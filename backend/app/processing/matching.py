"""The three decision layers: exact duplicate, near-duplicate, same story. Each has its own rule.

Exact duplicate (deterministic): the same canonical URL (already one staged row in Stage 3.1, extra
sources are kept as sightings), the same arXiv id / DOI, or the same content hash (normalised title and
excerpt, only when the excerpt is long enough to be distinctive).

Near-duplicate (same article, lightly modified): similar excerpt text AND similar title, close in time;
or, when an excerpt is missing, a near-identical title on the same site within a short window.

Same story (different articles about one event): within the story window, compatible event types, a
shared entity (or, failing that, at least three shared headline words and a higher score), no conflicting
product versions, and a weighted title/text/entity score over the threshold.

Vetoes come first and always win, so a high similarity can never merge:
- different known event types (a launch vs a pricing change vs a safety report),
- the same product family with different versions ("GPT-5" vs "GPT-5.1"),
- titles that both name products but share no product family ("GPT-5" vs "Sora 2"),
- for near-duplicates, titles whose numbers differ ("raises $2B" vs "raises $5B").
"""

from dataclasses import dataclass, field

from app.processing.features import Features, conflicting_products, jaccard, product_family
from app.processing.models import Thresholds

MIN_SHARED_TITLE_TOKENS_WITHOUT_ENTITY = 3


@dataclass
class Match:
    score: float
    reasons: list[str] = field(default_factory=list)


def _hours_apart(a: Features, b: Features) -> float:
    return abs((a.event_time - b.event_time).total_seconds()) / 3600


def title_similarity(a: Features, b: Features) -> float:
    """Mean of word-level and character-trigram Jaccard: robust to small edits and reordered words."""
    return (jaccard(a.title_tokens, b.title_tokens) + jaccard(a.title_ngrams, b.title_ngrams)) / 2


def entity_similarity(a: Features, b: Features) -> float:
    """1.0 when both name the same product; otherwise half the Jaccard of all entities (an organisation in
    common is weak evidence on its own: one company has many stories)."""
    if a.products & b.products:
        return 1.0
    return 0.5 * jaccard(a.entity_keys, b.entity_keys)


def vetoes(a: Features, b: Features) -> list[str]:
    """Reasons two articles can never be the same story (or the same article), whatever their similarity."""
    reasons = []
    if a.event_type and b.event_type and a.event_type != b.event_type:
        reasons.append(f"different_event_type:{a.event_type}/{b.event_type}")
    conflicts = conflicting_products(a.title_products, b.title_products)
    if conflicts:
        reasons.append("different_product_versions:" + ",".join(conflicts))
    if a.title_products and b.title_products:
        if not ({product_family(p) for p in a.title_products} & {product_family(p) for p in b.title_products}):
            reasons.append("different_products")
    return reasons


def exact_match_keys(f: Features) -> list[str]:
    """Deterministic identity keys for exact duplicates beyond the canonical URL."""
    keys = [f"id:{i}" for i in f.identifiers]
    if f.content_hash:
        keys.append(f"hash:{f.content_hash}")
    return keys


def near_duplicate(a: Features, b: Features, t: Thresholds) -> Match | None:
    hours = _hours_apart(a, b)
    if hours > max(t.near_duplicate_window_hours, t.title_only_window_hours):
        return None
    if not (a.title_tokens & b.title_tokens) and not (a.body_shingles & b.body_shingles):
        return None  # cheap pre-filter: nothing in common
    if vetoes(a, b):
        return None
    if a.title_numbers and b.title_numbers and a.title_numbers != b.title_numbers:
        return None
    tsim = title_similarity(a, b)
    if a.body_shingles and b.body_shingles:
        if hours > t.near_duplicate_window_hours:
            return None
        text = jaccard(a.body_shingles, b.body_shingles)
        if text >= t.near_duplicate_text and tsim >= t.near_duplicate_title:
            return Match(round(text, 3), [f"text_shingles={text:.2f}", f"title={tsim:.2f}", f"hours_apart={hours:.0f}"])
        return None
    # At least one excerpt is missing or too short: title similarity alone is only trusted on the same
    # site, close in time (a re-post or an edited headline), never across publishers.
    if a.site and a.site == b.site and hours <= t.title_only_window_hours and tsim >= t.title_only:
        return Match(round(tsim, 3), [f"title={tsim:.2f}", "same_site", f"hours_apart={hours:.0f}"])
    return None


def story_match(a: Features, b: Features, t: Thresholds) -> Match | None:
    hours = _hours_apart(a, b)
    if hours > t.story_window_hours or vetoes(a, b):
        return None
    shared = a.entity_keys & b.entity_keys
    if a.entity_keys and b.entity_keys and not shared:
        return None
    if not shared and len(a.title_topic & b.title_topic) < MIN_SHARED_TITLE_TOKENS_WITHOUT_ENTITY:
        # Without a recognised entity in common, a story match needs real headline overlap; short generic
        # headlines ("This week in AI") on different sites are different articles.
        return None
    # Similarity of what the articles are about, with organisation names and event words removed: "Company X
    # launches A" and "Company X launches B" share an organisation and an event type, but no topic.
    tsim = jaccard(a.title_topic, b.title_topic)
    text = jaccard(a.text_topic, b.text_topic)
    ent = entity_similarity(a, b)
    score = t.weight_title * tsim + t.weight_text * text + t.weight_entities * ent
    required = t.story
    if not shared:
        required += t.no_entity_penalty
    if a.event_type is None or b.event_type is None:
        required += t.unknown_event_penalty
    if score < required:
        return None
    reasons = [f"score={score:.2f}>={required:.2f}", f"title={tsim:.2f}", f"text={text:.2f}", f"entities={ent:.2f}"]
    if shared:
        reasons.append("shared:" + ",".join(sorted(shared)[:5]))
    if a.event_type and a.event_type == b.event_type:
        reasons.append(f"event_type={a.event_type}")
    reasons.append(f"hours_apart={hours:.0f}")
    return Match(round(score, 3), reasons)
