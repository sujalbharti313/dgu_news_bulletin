"""Same event in several clusters. Stage 3.2 clusters reports of one event together by wording and rules, but a
report written very differently can land in a separate cluster. Here such clusters are grouped, the most
complete and credible one is kept, and the others become ineligible duplicates pointing at it. Nothing is merged
or deleted; every cluster keeps its own sources and provenance.

Two clusters are the same event when:
- they share a paper identifier (arXiv id or DOI) or an article URL (facts in the source metadata), or
- semantically: the Gemini evaluation of either story judged the other `same_event` AND their embedding
  similarity is at least `semantic.same_event_min_similarity`. Similarity alone never decides it, so two
  different releases on the same topic stay separate stories.

Also here: `previously_shortlisted` (the same event as a story shortlisted in an earlier edition) and the extra
publishers a kept story gains from its duplicates (counted as corroboration, since they are separate clusters).
"""

import uuid

from app.ranking.config import RankingConfig
from app.ranking.contract import GateResult
from app.ranking.evaluate import Evaluated

_ELIGIBILITY_ORDER = {"eligible": 0, "review_required": 1, "ineligible": 2}


def semantic_same_event(a: Evaluated, b: Evaluated, config: RankingConfig) -> float | None:
    """The similarity when either evaluation says same_event above the threshold, else None."""
    floor = config.semantic.same_event_min_similarity
    for x, y in ((a, b), (b, a)):
        for rel in x.relations:
            if rel.story_cluster_id == y.cluster_id and rel.relation == "same_event" and rel.similarity >= floor:
                return rel.similarity
    return None


def same_event(a: Evaluated, b: Evaluated, config: RankingConfig) -> str | None:
    ids_a = {i for s in a.cluster.sources for i in s.identifiers}
    ids_b = {i for s in b.cluster.sources for i in s.identifiers}
    if ids_a & ids_b:
        return "same paper identifier"
    urls_a = {s.canonical_url for s in a.cluster.sources}
    if urls_a & {s.canonical_url for s in b.cluster.sources}:
        return "same article URL"
    similarity = semantic_same_event(a, b, config)
    if similarity is not None:
        return f"judged the same event, similarity {similarity:.2f}"
    return None


def preference(e: Evaluated) -> tuple:
    """Lower sorts first: eligible, best evidence and credibility, most sources, highest score, newest."""
    date = e.timely.timing.event_date
    return (
        _ELIGIBILITY_ORDER[e.eligibility],
        -(e.verification.evidence_quality_score + e.verification.source_credibility_score),
        -(e.verification.independent_source_count or 0),
        -e.verification.source_count,
        -e.final,
        -(date.timestamp() if date else 0),
        str(e.cluster_id),
    )


def duplicate_groups(stories: list[Evaluated], config: RankingConfig) -> list[tuple[int, list[int], dict[int, str]]]:
    """(kept index, member indexes, reasons) for every group of two or more clusters about one event."""
    parent = list(range(len(stories)))
    reasons: dict[int, str] = {}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(stories)):
        for j in range(i + 1, len(stories)):
            why = same_event(stories[i], stories[j], config)
            if why:
                reasons.setdefault(i, why)
                reasons.setdefault(j, why)
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(stories)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        if len(members) >= 2:
            keep = min(members, key=lambda i: preference(stories[i]))
            out.append((keep, members, reasons))
    return out


def extra_publishers(stories: list[Evaluated], config: RankingConfig) -> dict[uuid.UUID, set[str]]:
    """Publishers a kept story gains from the duplicate clusters of its event."""
    out: dict[uuid.UUID, set[str]] = {}
    for keep, members, _ in duplicate_groups(stories, config):
        kept = stories[keep]
        own = {s.publisher_key for s in kept.outcome.pack.sources} if kept.outcome.pack else set()
        extra = {
            s.publisher_key
            for i in members
            if i != keep and stories[i].outcome.pack
            for s in stories[i].outcome.pack.sources
            if s.report_group == s.ref_id and (s.ref.publisher or s.ref.site)
        } - own
        if extra:
            out[kept.cluster_id] = extra
    return out


def apply_duplicate_gate(stories: list[Evaluated], config: RankingConfig) -> int:
    """Marks duplicates in place. Returns how many stories were marked."""
    g = config.gates.duplicate_event
    if not g.enabled or len(stories) < 2:
        return 0
    marked = 0
    for keep, members, reasons in duplicate_groups(stories, config):
        kept = stories[keep]
        for i in members:
            if i == keep:
                continue
            e = stories[i]
            e.duplicate_of = kept.cluster_id
            newer = (
                kept.timely.timing.event_date
                and e.timely.timing.event_date
                and kept.timely.timing.event_date > e.timely.timing.event_date
            )
            e.extra_flags.add("duplicate_event")
            if newer:
                e.extra_flags.add("superseded_by_newer_cluster")
            e.add_gate(
                GateResult(
                    gate="duplicate_event",
                    action=g.action,
                    reason=f"Same event as {kept.cluster_id} ({reasons.get(i, 'same event')}), which is "
                    + ("newer and " if newer else "")
                    + "better evidenced; that cluster is preferred.",
                )
            )
            marked += 1
    return marked


def apply_previously_shortlisted(
    stories: list[Evaluated], shortlisted_before: set[uuid.UUID], config: RankingConfig
) -> int:
    """Review gate for a story that is the same event as one shortlisted in an earlier edition."""
    g = config.gates.previously_shortlisted
    if not g.enabled or not shortlisted_before:
        return 0
    floor = config.semantic.same_event_min_similarity
    marked = 0
    for e in stories:
        for rel in e.relations:
            if (
                rel.story_cluster_id in shortlisted_before
                and rel.story_cluster_id != e.cluster_id
                and rel.relation == "same_event"
                and rel.similarity >= floor
            ):
                e.extra_flags.add("previously_shortlisted_event")
                e.add_gate(
                    GateResult(
                        gate="previously_shortlisted",
                        action=g.action,
                        reason=f"The same event as {rel.story_cluster_id}, shortlisted in an earlier edition; "
                        "include only if it adds a real update.",
                    )
                )
                marked += 1
                break
    return marked
