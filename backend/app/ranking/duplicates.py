"""Gate 4: two clusters about the same event. Stage 3.2 clusters reports of one event together, but a later
report can land in a separate cluster (different wording, outside its time window). Here such clusters are
grouped, the most complete and credible one is kept, and the others become ineligible duplicates pointing at it.
Nothing is merged or deleted; every cluster keeps its own sources and provenance.

Same event when any of these holds:
- they share a paper identifier (arXiv id or DOI) or an article URL;
- they name the same product (Stage 3.2 entity) and are dated within `product_window_days`;
- their headlines are very similar (`title_similarity`) about the same organisation within `title_window_days`.
"""

from datetime import timedelta

from app.ranking.config import RankingConfig
from app.ranking.contract import GateResult
from app.ranking.evaluate import Evaluated
from app.ranking.text import jaccard

_ELIGIBILITY_ORDER = {"eligible": 0, "review_required": 1, "ineligible": 2}


def _days_apart(a: Evaluated, b: Evaluated) -> float | None:
    da, db = a.timely.timing.event_date, b.timely.timing.event_date
    if da is None or db is None:
        return None
    return abs((da - db) / timedelta(days=1))


def same_event(a: Evaluated, b: Evaluated, config: RankingConfig) -> str | None:
    g = config.gates.duplicate_event
    ids_a = {i for s in a.cluster.sources for i in s.identifiers}
    ids_b = {i for s in b.cluster.sources for i in s.identifiers}
    if ids_a & ids_b:
        return "same paper identifier"
    urls_a = {s.canonical_url for s in a.cluster.sources}
    if urls_a & {s.canonical_url for s in b.cluster.sources}:
        return "same article URL"
    apart = _days_apart(a, b)
    products = set(a.cluster.entities.products) & set(b.cluster.entities.products)
    if products and apart is not None and apart <= g.product_window_days:
        return f"same product ({sorted(products)[0]}) within {g.product_window_days} days"
    same_org = a.organisation and b.organisation and a.organisation.casefold() == b.organisation.casefold()
    similarity = max(jaccard(a.analysis.headline, b.analysis.headline), jaccard(a.cluster.title, b.cluster.title))
    if same_org and similarity >= g.title_similarity and apart is not None and apart <= g.title_window_days:
        return f"very similar headlines about {a.organisation}"
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


def apply_duplicate_gate(stories: list[Evaluated], config: RankingConfig) -> int:
    """Marks duplicates in place. Returns how many stories were marked."""
    g = config.gates.duplicate_event
    if not g.enabled or len(stories) < 2:
        return 0
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
    marked = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = min(members, key=lambda i: preference(stories[i]))
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
