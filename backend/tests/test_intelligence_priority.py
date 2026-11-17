"""Preliminary priority: fixed weights, deterministic levels, evidence caps."""

import pytest

from app.intelligence.priority import WEIGHTS, compute_priority, level_for


def scores(value=None, **overrides):
    base = dict.fromkeys(WEIGHTS, value if value is not None else 50)
    base.update(overrides)
    return base


def test_weights_match_the_specification():
    assert WEIGHTS == {
        "global_impact": 0.25,
        "technical_significance": 0.25,
        "student_relevance": 0.20,
        "timeliness": 0.15,
        "research_practical_importance": 0.15,
    }
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_weighted_score():
    s = scores(global_impact=80, technical_significance=60, student_relevance=70, timeliness=100,
               research_practical_importance=40)  # fmt: skip
    # 20 + 15 + 14 + 15 + 6 = 70
    result = compute_priority(s, sufficiency="sufficient", marketing_heavy=False, single_unofficial_report=False)
    assert (result.weighted_score, result.overall_score, result.priority_level) == (70, 70, "High")
    assert result.adjustments == []


@pytest.mark.parametrize("score,level", [(100, "Critical"), (85, "Critical"), (84, "High"), (70, "High"),
                                         (69, "Medium"), (45, "Medium"), (44, "Low"), (0, "Low")])  # fmt: skip
def test_levels(score, level):
    assert level_for(score) == level


def test_same_inputs_always_give_the_same_result():
    a = compute_priority(scores(77), sufficiency="partial", marketing_heavy=False, single_unofficial_report=True)
    b = compute_priority(scores(77), sufficiency="partial", marketing_heavy=False, single_unofficial_report=True)
    assert a == b


def test_insufficient_evidence_is_capped_at_low():
    result = compute_priority(
        scores(95), sufficiency="insufficient", marketing_heavy=False, single_unofficial_report=False
    )
    assert result.weighted_score == 95 and result.overall_score == 44 and result.priority_level == "Low"
    assert "insufficient evidence" in result.adjustments[0]


def test_marketing_heavy_announcements_cannot_rank_high():
    result = compute_priority(
        scores(90), sufficiency="sufficient", marketing_heavy=True, single_unofficial_report=False
    )
    assert result.priority_level == "Medium" and result.overall_score == 69


def test_single_unofficial_report_and_partial_evidence_are_never_critical():
    single = compute_priority(
        scores(95), sufficiency="sufficient", marketing_heavy=False, single_unofficial_report=True
    )
    partial = compute_priority(scores(95), sufficiency="partial", marketing_heavy=False, single_unofficial_report=False)
    assert single.priority_level == partial.priority_level == "High"


def test_a_small_research_breakthrough_can_outrank_a_big_company_announcement():
    research = scores(global_impact=55, technical_significance=95, student_relevance=85, timeliness=80,
                      research_practical_importance=90)  # fmt: skip
    announcement = scores(global_impact=85, technical_significance=30, student_relevance=40, timeliness=90,
                          research_practical_importance=30)  # fmt: skip
    r = compute_priority(research, sufficiency="sufficient", marketing_heavy=False, single_unofficial_report=False)
    a = compute_priority(announcement, sufficiency="sufficient", marketing_heavy=True, single_unofficial_report=False)
    assert r.overall_score > a.overall_score
