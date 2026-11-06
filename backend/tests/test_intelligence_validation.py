"""Structured output validation: schema, lengths, categories, project-idea limits, source references,
invented URLs and numbers, and missing evidence. Pure; no provider."""

import json

import pytest

from app.intelligence.categories import CategorySet, load_categories
from app.intelligence.errors import IntelligenceConfigError
from app.intelligence.evidence import build_evidence, normalise_number, numbers_in
from app.intelligence.validation import validate_response
from tests.intelligence_helpers import CATEGORIES, THIN_ITEM, clusters, good_draft

PACK = build_evidence(clusters()[0])


def check(content, max_ideas=3):
    text = content if isinstance(content, str) or content is None else json.dumps(content)
    return validate_response(text, PACK, CATEGORIES, max_project_ideas=max_ideas)


def codes(result):
    return sorted({i.code for i in result.errors})


def test_a_well_formed_grounded_response_is_valid():
    result = check(good_draft())
    assert result.ok, [str(i) for i in result.issues]


@pytest.mark.parametrize(
    ("content", "code"),
    [(None, "empty_response"), ("  ", "empty_response"), ("{not json", "malformed_json"), ("[1, 2]", "malformed_json")],
)
def test_empty_or_malformed_responses_are_rejected(content, code):
    result = check(content)
    assert not result.ok and result.draft is None and codes(result) == [code]


def test_missing_fields_wrong_types_and_extra_fields_fail_the_schema():
    missing = good_draft()
    del missing["claims"]
    wrong_type = good_draft(**{"project_ideas": "build something"})
    extra = good_draft(**{"source_url": "https://example.com"})
    bad_enum = good_draft(**{"evidence_assessment.sufficiency": "certain"})
    for draft in (missing, wrong_type, extra, bad_enum):
        result = check(draft)
        assert not result.ok and codes(result) == ["schema"]


def test_field_lengths_are_enforced():
    assert codes(check(good_draft(headline="x" * 141))) == ["schema"]
    assert codes(check(good_draft(summary="Too short to be a summary."))) == ["length"]
    long_summary = " ".join(["word"] * 140)
    assert codes(check(good_draft(summary=long_summary))) == ["length"]
    near = check(good_draft(summary=" ".join(["word"] * 50)))
    assert near.ok and [w.code for w in near.warnings] == ["length"]  # outside 60-100: review warning only


def test_required_content_when_evidence_is_claimed_sufficient():
    result = check(good_draft(summary=None, technical_explanation=None, claims=[]))
    assert codes(result) == ["missing_field"]
    paths = {i.path for i in result.errors}
    assert {"summary", "technical_explanation", "claims"} <= paths
    assert codes(check(good_draft(**{"student_relevance.summary": None}))) == ["missing_field"]
    assert codes(
        check(good_draft(**{"why_it_matters.confirmed_impact": None, "why_it_matters.interpretation": None}))
    ) == ["missing_field"]  # noqa: E501


def test_insufficient_evidence_may_leave_analysis_empty_instead_of_inventing_it():
    draft = good_draft(
        summary=None,
        technical_explanation=None,
        claims=[],
        technical_details=[],
        project_ideas=[],
        **{
            "evidence_assessment.sufficiency": "insufficient",
            "student_relevance.summary": None,
            "why_it_matters.confirmed_impact": None,
            "why_it_matters.interpretation": None,
        },
    )
    assert check(draft).ok


def test_category_values_are_validated():
    assert codes(check(good_draft(**{"classification.primary": "crypto"}))) == ["category"]
    assert codes(check(good_draft(**{"classification.secondary": ["sports"]}))) == ["category"]
    assert codes(check(good_draft(**{"classification.secondary": ["model-releases"]}))) == ["category"]  # = primary
    too_many = ["ai-research", "ai-tools-development", "industry-business"]
    assert codes(check(good_draft(**{"classification.secondary": too_many}))) == ["category"]
    assert codes(check(good_draft(**{"classification.secondary": ["ai-research", "ai-research"]}))) == ["category"]


def test_project_idea_limits():
    idea = good_draft()["project_ideas"][0]
    assert check(good_draft(project_ideas=[])).ok  # zero is allowed: never forced
    assert check(good_draft(project_ideas=[idea] * 3)).ok
    assert codes(check(good_draft(project_ideas=[idea] * 4))) == ["schema"]
    assert codes(check(good_draft(project_ideas=[idea] * 2), max_ideas=1)) == ["limit"]


def test_source_reference_integrity():
    assert codes(check(good_draft(**{"claims.0.source_ids": ["S9"]}))) == ["source_reference"]
    assert codes(check(good_draft(**{"claims.0.source_ids": []}))) == ["source_reference"]  # stated, uncited
    assert codes(check(good_draft(**{"technical_details.0.source_ids": ["https://x.example.com"]}))) == [
        "source_reference"
    ]
    inferred = good_draft(**{"claims.1.basis": "inferred", "claims.1.source_ids": []})
    assert check(inferred).ok  # an inference need not cite


def test_urls_not_from_the_sources_are_rejected():
    bad = good_draft(summary=good_draft()["summary"] + " Details at https://made-up.example.org/post.")
    assert "unsupported_url" in codes(check(bad))
    idea = dict(good_draft()["project_ideas"][0], technical_direction="Fork www.random-repo.example and adapt it.")
    assert "unsupported_url" in codes(check(good_draft(project_ideas=[idea])))
    real = good_draft(**{"evidence_assessment.notes": "See https://labs.example.com/blog/nimbus-2-5 for the post."})
    assert check(real).ok  # a source's own URL is not invented


def test_numbers_missing_from_the_evidence_are_rejected():
    invented = good_draft(**{"technical_details.1.detail": "71.2% and 92.5% on HumanEval"})
    result = check(invented)
    assert codes(result) == ["unsupported_number"] and "92.5" in result.errors[0].message
    assert codes(check(good_draft(headline="Example Labs releases Nimbus 3 with a 1M-token window"))) == [
        "unsupported_number"
    ]
    # Interpretation and project ideas are not factual claims, so they are not number-checked.
    assert check(good_draft(**{"why_it_matters.interpretation": "Within 2 to 3 years this may be common."})).ok


def test_number_normalisation():
    assert numbers_in("71.2%, 1,000 users, 400K tokens, v2.50, 007") == {"71.2", "1000", "400", "2.5", "7"}
    assert normalise_number("3.0") == "3" and normalise_number("0") == "0"
    assert numbers_in("as reported (S1, S12)") == set()


def test_thin_clusters_have_a_low_word_count():
    assert build_evidence(clusters([THIN_ITEM])[0]).word_count < 25


def test_category_file_is_validated(tmp_path):
    bad = tmp_path / "cats.toml"
    bad.write_text('[[categories]]\nkey = "Bad Key"\nlabel = "x"\ndescription = "y"\ntopic = "models"\n')
    with pytest.raises(IntelligenceConfigError):
        load_categories(bad)
    with pytest.raises(IntelligenceConfigError):
        load_categories(tmp_path / "missing.toml")
    dup = tmp_path / "dup.toml"
    entry = '[[categories]]\nkey = "a"\nlabel = "A"\ndescription = "d"\ntopic = "models"\n'
    dup.write_text(entry * 2)
    with pytest.raises(IntelligenceConfigError):
        load_categories(dup)
    assert isinstance(CATEGORIES, CategorySet) and len(CATEGORIES.keys) == 8 and "india-ai" in CATEGORIES.keys
