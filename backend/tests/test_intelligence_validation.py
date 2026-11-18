"""Output validation: malformed or unsupported model output is rejected, never patched. No network, no DB."""

import json

import pytest

from app.intelligence.draft import response_json_schema
from app.intelligence.evidence import build_evidence
from app.intelligence.validation import validate_response
from tests.intelligence_helpers import CATEGORIES, clusters, good_draft


@pytest.fixture(scope="module")
def pack():
    return build_evidence(clusters()[0])


def check(pack, draft, ideas=3):
    content = draft if isinstance(draft, str) else json.dumps(draft)
    return validate_response(content, pack, CATEGORIES, max_project_ideas=ideas)


def codes(result):
    return {i.code for i in result.errors}


def test_a_grounded_response_is_accepted(pack):
    result = check(pack, good_draft())
    assert result.ok, result.issues


@pytest.mark.parametrize("content", [None, "", "not json", "[1, 2]", '{"headline": "x"}'])
def test_malformed_or_incomplete_json_is_rejected(pack, content):
    result = validate_response(content, pack, CATEGORIES, max_project_ideas=3)
    assert not result.ok and result.draft is None


def test_extra_fields_and_out_of_range_scores_are_rejected(pack):
    assert not check(pack, {**good_draft(), "verified": True}).ok
    assert not check(pack, good_draft(**{"priority.global_impact": 140})).ok
    assert not check(pack, good_draft(**{"priority.timeliness": -1})).ok


def test_invented_numbers_are_rejected(pack):
    result = check(pack, good_draft(summary=good_draft()["summary"].replace("71.2%", "88.4%")))
    assert "unsupported_number" in codes(result)


def test_invented_urls_are_rejected_but_source_urls_are_not_needed(pack):
    draft = good_draft(**{"why_it_matters.interpretation": "See https://made-up.example.org/report for more."})
    assert "unsupported_url" in codes(check(pack, draft))


def test_unknown_source_ids_and_uncited_stated_claims_are_rejected(pack):
    assert "source_reference" in codes(check(pack, good_draft(**{"claims.0.source_ids": ["S9"]})))
    assert "source_reference" in codes(check(pack, good_draft(**{"claims.0.source_ids": []})))


def test_categories_must_come_from_the_list(pack):
    assert "category" in codes(check(pack, good_draft(**{"classification.primary": "crypto"})))
    assert "category" in codes(
        check(pack, good_draft(**{"classification.secondary": ["ai-model-releases"]}))
    )  # repeats primary
    too_many = ["ai-research", "ai-tools-development", "industry-business"]
    assert "category" in codes(check(pack, good_draft(**{"classification.secondary": too_many})))


def test_summary_length_is_enforced(pack):
    assert "length" in codes(check(pack, good_draft(summary="Example Labs released a model.")))
    long = " ".join(["word"] * 140)
    assert "length" in codes(check(pack, good_draft(summary=long)))


def test_promotional_language_is_rejected(pack):
    summary = good_draft()["summary"].replace("a language model", "a groundbreaking language model")
    assert "promotional_language" in codes(check(pack, good_draft(summary=summary)))


def test_technical_explanation_status_must_match_its_text(pack):
    assert "missing_field" in codes(
        check(pack, good_draft(technical_explanation={"status": "available", "text": None}))
    )
    assert "consistency" in codes(
        check(pack, good_draft(technical_explanation={"status": "not_applicable", "text": "Some explanation here."}))
    )
    ok = check(pack, good_draft(technical_explanation={"status": "insufficient_evidence", "text": None}))
    assert ok.ok, ok.issues


def test_insufficient_evidence_needs_no_summary_but_cannot_explain_technology(pack):
    draft = good_draft(**{"evidence_assessment.sufficiency": "insufficient", "summary": None})
    draft["technical_explanation"] = {"status": "insufficient_evidence", "text": None}
    assert check(pack, draft).ok
    draft["technical_explanation"] = {"status": "available", "text": good_draft()["technical_explanation"]["text"]}
    assert "consistency" in codes(check(pack, draft))


def test_too_many_project_ideas_are_rejected(pack):
    ideas = good_draft()["project_ideas"] * 3
    assert "limit" in codes(check(pack, good_draft(project_ideas=ideas), ideas=2))


def test_response_schema_is_strict_and_limits_categories():
    schema = response_json_schema(CATEGORIES.keys, 3)
    assert "$defs" not in json.dumps(schema)  # flat for Gemini
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    primary = schema["properties"]["classification"]["properties"]["primary"]
    assert primary["enum"] == CATEGORIES.keys
    # The model is never asked for URLs, the cluster id, the model id or the overall score.
    text = json.dumps(schema)
    for forbidden in ("original_url", "story_cluster_id", "model_id", "overall_score"):
        assert forbidden not in text


def test_the_nine_specified_categories_are_configured():
    assert [c.label for c in CATEGORIES.categories] == [
        "AI Research",
        "AI Model Releases",
        "AI Tools and Development",
        "Industry and Business",
        "India × AI",
        "AI Safety and Governance",
        "AI Education and Careers",
        "AI Infrastructure and Hardware",
        "Robotics and Automation",
    ]


def test_response_schema_states_limits_as_instructions():
    schema = response_json_schema(CATEGORIES.keys, 3)
    text = json.dumps(schema)
    for keyword in ("maxLength", "minLength", "maxItems", "minItems"):
        assert keyword not in text
    assert "At most 6 items." in schema["properties"]["understanding"]["properties"]["key_points"]["description"]
    assert "At most 112 characters." in schema["properties"]["headline"]["description"]
    assert "characters" in schema["properties"]["summary"]["description"]


def test_fields_named_like_schema_keywords_are_kept():
    """`title` is a keyword we strip, but also the name of a project idea field."""
    schema = response_json_schema(CATEGORIES.keys, 3)
    idea = schema["properties"]["project_ideas"]["items"]
    assert set(idea["properties"]) == {"title", "description", "technologies"}
    assert set(idea["required"]) == set(idea["properties"])

    def no_keyword_titles(node, in_properties=False):
        if isinstance(node, dict):
            if not in_properties:
                assert "title" not in node or isinstance(node["title"], dict)
            for k, v in node.items():
                if k == "properties":
                    for sub in v.values():
                        no_keyword_titles(sub)
                else:
                    no_keyword_titles(v)
        elif isinstance(node, list):
            for v in node:
                no_keyword_titles(v)

    no_keyword_titles(schema)
