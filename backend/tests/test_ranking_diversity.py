"""Stage 3.4 diversity-aware shortlist: category, organisation, development-type and semantic-topic caps, exceptional
stories, coverage swaps, one event once, and never filling slots with weak stories."""

import uuid
from types import SimpleNamespace

from app.ranking.shortlist import rank, select_shortlist
from tests.intelligence_helpers import FUNDING, INDIA, LAB_POST, NEWS_REPORT, PAPER, POLICY, SYNDICATED_COPY
from tests.ranking_helpers import CONFIG, WEEK, analysed, make_ranker


def story(score, category="ai-research", org=None, groups=None, eligibility="eligible", evidence=60, dev=None):
    return SimpleNamespace(
        development_type=dev or f"type-{uuid.uuid4().hex[:6]}",
        relations=[],
        cluster_id=uuid.uuid4(),
        final=score,
        eligibility=eligibility,
        organisation=org,
        coverage_groups=groups if groups is not None else [category.split("-")[1] if "-" in category else category],
        analysis=SimpleNamespace(primary_category_key=category, primary_category=category.replace("-", " ").title()),
        verification=SimpleNamespace(evidence_quality_score=evidence, source_credibility_score=60),
        selection_note=None,
    )


def shortlist(stories, config=CONFIG):
    selected, notes = select_shortlist(rank(stories), config)
    return selected, notes


def test_same_category_is_capped():
    research = [story(80 - i, "ai-research", groups=["research"]) for i in range(CONFIG.shortlist.max_per_category + 3)]
    tools = [story(60, "ai-tools-development", groups=["tools"])]
    selected, _ = shortlist(research + tools)
    assert (
        sum(1 for s in selected if s.analysis.primary_category_key == "ai-research")
        == CONFIG.shortlist.max_per_category
    )
    assert tools[0] in selected
    capped = [s for s in research if s not in selected]
    assert capped and all(f"already {CONFIG.shortlist.max_per_category}" in s.selection_note for s in capped)


def test_same_company_is_capped():
    lab = [story(78 - i, f"cat-{i}", org="Example Labs", groups=[f"g{i}"]) for i in range(4)]
    other = story(58, "industry-business", org="Other Co", groups=["industry"])
    selected, _ = shortlist(lab + [other])
    assert sum(1 for s in selected if s.organisation == "Example Labs") == CONFIG.shortlist.max_per_organisation
    assert other in selected
    assert any("about Example Labs" in (s.selection_note or "") for s in lab)


def test_exceptional_stories_ignore_diversity_caps():
    exceptional = [story(95 - i, "ai-model-releases", org="Example Labs", groups=["model_releases"]) for i in range(4)]
    selected, _ = shortlist(exceptional)
    assert len(selected) == 4  # all >= 85: never sacrificed for a quota
    assert all("exceptional" in s.selection_note for s in selected)


def test_high_scoring_stories_across_categories_are_all_kept():
    cats = ["ai-research", "ai-model-releases", "industry-business", "india-ai", "ai-safety-governance"]
    stories = [story(80 - i, c, groups=[c]) for i, c in enumerate(cats)]
    selected, _ = shortlist(stories)
    assert [s.final for s in selected] == [80, 79, 78, 77, 76]


def test_weak_stories_never_fill_the_shortlist():
    stories = [story(70, "ai-research"), story(54.9, "india-ai"), story(30, "ai-tools-development")]
    stories.append(story(90, "industry-business", eligibility="review_required"))
    selected, notes = shortlist(stories)
    assert [s.final for s in selected] == [70]
    assert any("only 1 story met the quality bar" in n for n in notes)
    assert "below the shortlist minimum" in stories[1].selection_note
    assert stories[3].selection_note == "not shortlisted: review required"


def test_target_is_respected():
    stories = [story(80 - i, f"c{i}", groups=[f"g{i}"]) for i in range(15)]
    selected, _ = shortlist(stories)
    assert len(selected) == CONFIG.shortlist.target
    assert "shortlist is full" in stories[-1].selection_note


def test_coverage_swap_brings_in_an_unrepresented_group_within_the_margin():
    config = CONFIG.model_copy(update={"shortlist": CONFIG.shortlist.model_copy(update={"target": 3})})
    picks = [
        story(80, "a", groups=["research"]),
        story(75, "b", groups=["industry"]),
        story(70, "c", groups=["research"]),
    ]
    india = story(64, "india-ai", groups=["india"])
    selected, notes = shortlist(picks + [india], config)
    assert india in selected and picks[2] not in selected
    assert "coverage of 'india'" in india.selection_note and notes
    assert picks[1] in selected  # the only 'industry' story is never the one replaced
    # Outside the margin: no swap.
    far = story(50 + 5, "india-ai", groups=["india"])
    picks2 = [
        story(80, "a", groups=["research"]),
        story(79, "b", groups=["tools"]),
        story(75, "c", groups=["industry"]),
    ]
    selected2, _ = shortlist(picks2 + [far], config)
    assert far not in selected2


def test_end_to_end_multiple_stories_about_one_event_take_one_slot():
    items = [LAB_POST, NEWS_REPORT, SYNDICATED_COPY, PAPER, INDIA, FUNDING, POLICY]
    clusters, records = analysed(items)
    runner, _ = make_ranker(clusters, records)
    result = runner.run(week=WEEK).result
    ids = result.ranked_candidate_ids
    assert len(ids) == len(set(ids))
    nimbus = [c for c in result.candidates if "Nimbus" in c.headline]
    assert len(nimbus) == 1  # Stage 3.2 merged the three reports; the shortlist cannot hold the event twice
    for c in result.shortlist:
        assert c.eligibility_status == "eligible" and c.final_scores.final_priority_score >= CONFIG.shortlist.min_score
        assert c.ranking_explanation.startswith(c.priority_classification)
    assert [c.shortlist_rank for c in result.shortlist] == list(range(1, len(result.shortlist) + 1))
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))


def test_same_development_type_is_capped():
    cap = CONFIG.shortlist.max_per_development_type
    funding = [story(80 - i, f"c{i}", groups=[f"g{i}"], dev="funding") for i in range(cap + 2)]
    other = story(56, "x", groups=["x"], dev="research_paper")
    selected, _ = shortlist(funding + [other])
    assert sum(1 for s in selected if s.development_type == "funding") == cap
    assert other in selected
    assert any("of type 'funding'" in (s.selection_note or "") for s in funding)


def test_same_semantic_topic_is_capped_by_embedding_similarity():
    cap = CONFIG.shortlist.max_per_topic
    topic = [story(80 - i, f"c{i}", groups=[f"g{i}"]) for i in range(cap + 1)]
    for a in topic:  # every pair is very close in embedding space: one topic
        a.relations = [
            SimpleNamespace(story_cluster_id=b.cluster_id, similarity=CONFIG.semantic.topic_similarity + 0.01)
            for b in topic
            if b is not a
        ]
    distinct = story(57, "z", groups=["z"])
    selected, _ = shortlist(topic + [distinct])
    assert sum(1 for s in selected if s in topic) == cap
    assert distinct in selected


def test_a_preprint_repository_is_not_the_organisation_behind_a_paper():
    """Papers from different labs on arXiv are not 'stories about arXiv' for the organisation cap."""
    from app.ranking.evaluate import organisation_of
    from tests.ranking_helpers import analysis_of

    clusters, records = analysed([PAPER])
    cluster = clusters[0]
    analysis = analysis_of(records, cluster)
    tagged = cluster.model_copy(
        update={"entities": cluster.entities.model_copy(update={"organisations": ["arXiv", "Example University"]})}
    )
    assert organisation_of(tagged, analysis) == "Example University"
    only_platform = cluster.model_copy(
        update={"entities": cluster.entities.model_copy(update={"organisations": ["arXiv"]})}
    )
    no_party = analysis.model_copy(update={"understanding": analysis.understanding.model_copy(update={"involved": []})})
    assert organisation_of(only_platform, no_party) is None  # unknown: no organisation cap applies
