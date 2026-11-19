"""Stage 3.4 scoring: weighted total, rounding, bounds, missing data, timeliness (Asia/Kolkata weeks), classes,
and strict configuration validation."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.ranking.config import RankingConfigError, load_ranking_config
from app.ranking.scoring import clamp, final_scores, timeliness, weighted_total
from app.ranking.verification import verify
from app.ranking.window import EditionWindow, edition_for
from tests.intelligence_helpers import PAPER
from tests.processing_helpers import NOW, staged
from tests.ranking_helpers import CONFIG, REGISTRY, analysed, analysis_of, draft_for

WEIGHTS = CONFIG.weights.as_dict()
WINDOW = EditionWindow(date(2026, 11, 2))  # Monday 2 Nov to Sunday 8 Nov 2026, Asia/Kolkata


def scores(**values):
    base = {d: 0 for d in WEIGHTS}
    base.update(values)
    return base


def test_weights_match_the_specification():
    assert WEIGHTS == {
        "global_impact": 0.20,
        "technical_significance": 0.20,
        "student_relevance": 0.20,
        "evidence_quality": 0.15,
        "source_credibility": 0.15,
        "timeliness": 0.10,
    }


def test_weighted_score_calculation():
    s = scores(
        global_impact=80,
        technical_significance=70,
        student_relevance=90,
        evidence_quality=60,
        source_credibility=85,
        timeliness=100,
    )
    # 16 + 14 + 18 + 9 + 12.75 + 10
    assert weighted_total(s, WEIGHTS) == 79.8


def test_rounding_is_half_up_to_one_decimal():
    # 0.15 * 1 = 0.15 -> 0.2 (half-up; binary floats would give 0.1 with round()).
    assert weighted_total(scores(evidence_quality=1), WEIGHTS) == 0.2
    assert weighted_total(scores(evidence_quality=3), WEIGHTS) == 0.5  # 0.45 -> 0.5
    assert clamp(84.5) == 85 and clamp(84.49) == 84


def test_minimum_and_maximum_bounds():
    assert weighted_total(scores(), WEIGHTS) == 0.0
    assert weighted_total({d: 100 for d in WEIGHTS}, WEIGHTS) == 100.0
    assert clamp(-20) == 0 and clamp(140) == 100


def test_missing_dimension_counts_as_zero_and_is_listed():
    clusters, records = analysed([PAPER])
    cluster = clusters[0]
    undated = cluster.model_copy(
        update={
            "earliest_published_at": None,
            "sources": [s.model_copy(update={"published_at": None}) for s in cluster.sources],
        }
    )
    analysis = analysis_of(records, cluster)
    ev = draft_for(undated, analysis)
    v = verify(undated, analysis, ev, CONFIG, REGISTRY).verification
    t = timeliness(undated, WINDOW, CONFIG)
    assert t.score is None and "publication_date_missing" in t.flags and t.timing.date_basis == "none"
    fs = final_scores(analysis, v, ev, t, CONFIG)
    assert fs.timeliness is None and fs.unavailable_dimensions == ["timeliness"]
    expected = weighted_total({**{d: getattr(fs, d) for d in WEIGHTS}, "timeliness": 0}, WEIGHTS)
    assert fs.final_priority_score == expected


def _dated(moment: datetime, *, newsletter: bool = False):
    item = staged("A dated story about model evaluation", "https://news-a.example.com/x", "Some text " * 5)
    clusters, _ = analysed([item])
    c = clusters[0]
    if newsletter:
        return c.model_copy(
            update={
                "earliest_published_at": None,
                "sources": [s.model_copy(update={"published_at": None, "newsletter_date": moment}) for s in c.sources],
            }
        )
    return c.model_copy(update={"earliest_published_at": moment})


IST = timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    ("local", "expected", "flag"),
    [
        (datetime(2026, 11, 8, 23, 0), 100, None),  # Sunday 23:00 IST: last day of the week
        (datetime(2026, 11, 2, 0, 30), 88, None),  # Monday 00:30 IST: first day, 6 days before the end
        (datetime(2026, 11, 1, 12, 0), 70, "late_arrival"),  # Sunday before: 1 day before the week
        (datetime(2026, 10, 25, 12, 0), 35, "out_of_window"),  # 8 days before
        (datetime(2026, 10, 12, 12, 0), 10, "out_of_window"),  # 21 days before
        (datetime(2026, 9, 1, 12, 0), 0, "resurfaced"),
        (datetime(2026, 11, 9, 0, 30), 0, "after_window"),  # Monday 00:30 IST: next week's edition
    ],
)
def test_timeliness_uses_asia_kolkata_weeks(local, expected, flag):
    moment = (local - IST).replace(tzinfo=UTC)
    t = timeliness(_dated(moment), WINDOW, CONFIG)
    assert t.score == expected
    if flag:
        assert flag in t.flags
    else:
        assert t.timing.in_window and not t.flags


def test_newsletter_date_only_is_capped_and_flagged():
    moment = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
    t = timeliness(_dated(moment, newsletter=True), WINDOW, CONFIG)
    assert t.timing.date_basis == "newsletter" and t.score == CONFIG.timeliness.newsletter_date_cap
    assert "publication_date_from_newsletter" in t.flags


def test_edition_window_boundaries():
    w = edition_for(datetime(2026, 11, 9, 1, 0, tzinfo=UTC), week="previous")  # Monday 06:30 IST
    assert w.start_date == date(2026, 11, 2) and w.end_date == date(2026, 11, 8)
    assert w.start == datetime(2026, 11, 1, 18, 30, tzinfo=UTC) and w.end == datetime(2026, 11, 8, 18, 30, tzinfo=UTC)
    # 23:00 UTC on Sunday 8 Nov is already Monday 9 Nov in India.
    late = edition_for(datetime(2026, 11, 8, 23, 0, tzinfo=UTC), week="current")
    assert late.start_date == date(2026, 11, 9)
    assert edition_for(NOW, week="2026-11-05").start_date == date(2026, 11, 2)
    with pytest.raises(ValueError):
        edition_for(NOW, week="last tuesday")


@pytest.mark.parametrize(
    ("score", "label"),
    [(100, "Exceptional"), (85, "Exceptional"), (84.9, "High"), (70, "High"), (69.9, "Medium"), (55, "Medium"),
     (54.9, "Low"), (40, "Low"), (39.9, "Very Low"), (0, "Very Low")],
)  # fmt: skip
def test_priority_classification_thresholds(score, label):
    assert CONFIG.classification.classify(score) == label


def test_thresholds_are_configurable(tmp_path):
    raw = open("config/ranking.toml", encoding="utf-8").read().replace("exceptional = 85", "exceptional = 90")
    path = tmp_path / "ranking.toml"
    path.write_text(raw, encoding="utf-8")
    custom = load_ranking_config(path)
    assert custom.classification.classify(88) == "High"
    assert custom.fingerprint != CONFIG.fingerprint  # an edit always changes the stored version


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("timeliness = 0.10", "timeliness = 0.20", "sum to 1.0"),
        ("high = 70", "high = 90", "strictly decreasing"),
        ("target = 10", "target = 0", "target"),
        ('action = "review_required"', 'action = "delete"', "action"),
        ("[weights]", "[weights]\nbonus = 0.1", "bonus"),
    ],
)
def test_invalid_configuration_is_rejected(tmp_path, old, new, message):
    raw = open("config/ranking.toml", encoding="utf-8").read().replace(old, new, 1)
    path = tmp_path / "ranking.toml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(RankingConfigError, match=message):
        load_ranking_config(path)


def _scored(**changes):
    clusters, records = analysed([PAPER])
    cluster = clusters[0]
    analysis = analysis_of(records, cluster)
    ev = draft_for(cluster, analysis, **changes)
    v = verify(cluster, analysis, ev, CONFIG, REGISTRY).verification
    return final_scores(analysis, v, ev, timeliness(cluster, WINDOW, CONFIG), CONFIG), v


def test_final_score_never_exceeds_100_even_with_perfect_inputs():
    fs, _ = _scored(
        **{"industry_impact.score": 100, "technical_novelty.score": 100, "student_relevance.score": 100,
           "evidence.score": 100}
    )  # fmt: skip
    assert 0 <= fs.final_priority_score <= 100
    assert all(getattr(fs, d) is None or 0 <= getattr(fs, d) <= 100 for d in WEIGHTS)


def test_dimension_scores_come_from_the_semantic_evaluation_and_the_total_from_code():
    fs, v = _scored(**{"industry_impact.score": 72, "technical_novelty.score": 64, "student_relevance.score": 81})
    assert (fs.global_impact, fs.technical_significance, fs.student_relevance) == (72, 64, 81)
    assert fs.evidence_quality == v.evidence_quality_score and fs.source_credibility == v.source_credibility_score
    expected = weighted_total({d: getattr(fs, d) for d in WEIGHTS}, WEIGHTS)
    assert fs.final_priority_score == expected  # weighted in code; the model never supplies a final score
    assert fs.weights == WEIGHTS
    assert set(WEIGHTS) <= set(fs.explanations)


def test_the_model_cannot_supply_a_final_score_or_extra_fields():
    from app.ranking.semantic import EvaluationDraft

    with pytest.raises(ValueError):
        EvaluationDraft.model_validate({"final_priority_score": 100})


@pytest.mark.parametrize(
    ("changes", "dimension", "cap"),
    [
        ({"hype.level": "significant", "industry_impact.score": 95}, "global_impact", "significant_hype_impact"),
        ({"hype.level": "significant", "technical_novelty.score": 95}, "technical_significance",
         "significant_hype_technical"),
        ({"hype.level": "mild", "technical_novelty.score": 95}, "technical_significance", "mild_hype_technical"),
    ],
)  # fmt: skip
def test_hype_caps_scores(changes, dimension, cap):
    fs, _ = _scored(**changes)
    assert getattr(fs, dimension) == getattr(CONFIG.caps, cap)
    assert any("capped" in a for a in fs.adjustments)


def test_weak_evidence_caps_impact():
    fs, v = _scored(**{"evidence.score": 25, "industry_impact.score": 95})
    assert v.evidence_quality_score < CONFIG.caps.weak_evidence_below
    assert fs.global_impact <= CONFIG.caps.weak_evidence_impact
