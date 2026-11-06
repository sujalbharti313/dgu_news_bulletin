"""Stage 3.2 exact and near-duplicate detection, false-positive prevention, idempotency, bad input."""

from datetime import timedelta

import pytest

from app.processing.features import article_identifiers, conflicting_products, extract_products
from app.processing.models import Thresholds
from app.processing.pipeline import process_items
from app.services.urls import canonicalize_url
from tests.processing_helpers import NOW, cluster_of, decisions, run, staged

LAUNCH = (
    "Example Labs today released Nimbus 2.5, a new language model that improves reasoning and coding. "
    "The model is available in the API starting today for all paid users, with free users getting access later."
)


# --- URL canonicalisation and identifiers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/p?utm_source=x&mkt_tok=y&_ga=1&gclid=2&hsa_cam=3", "https://example.com/p"),
        ("https://example.com/p?ref=newsletter&id=7", "https://example.com/p?id=7"),
        ("https://Example.com./p/#comments", "https://example.com/p"),
        ("http://www.example.com/p/", "https://example.com/p"),
    ],
)
def test_tracking_parameters_and_formatting_are_normalised(a, b):
    assert canonicalize_url(a) == canonicalize_url(b)


def test_meaningful_query_and_hashbang_routes_are_kept():
    assert canonicalize_url("https://example.com/watch?v=abc&t=10") == "https://example.com/watch?t=10&v=abc"
    assert canonicalize_url("https://example.com/#!/post/42") == "https://example.com/#!/post/42"
    assert canonicalize_url("https://example.com/a?page=2") != canonicalize_url("https://example.com/a?page=3")


def test_article_identifiers_from_urls():
    assert article_identifiers("https://arxiv.org/abs/2611.01234v3") == ["arxiv:2611.01234"]
    assert article_identifiers("https://arxiv.org/pdf/2611.01234") == ["arxiv:2611.01234"]
    assert article_identifiers("https://doi.org/10.1145/1234.5678") == ["doi:10.1145/1234.5678"]
    assert article_identifiers("https://example.com/2611.01234") == []
    assert article_identifiers(None) == []


# --- exact duplicates ---------------------------------------------------------------------------


def test_same_content_under_another_url_is_an_exact_duplicate():
    a = staged("Example Labs releases Nimbus 2.5", "https://labs.example.com/nimbus-2-5", LAUNCH, source="lab-blog")
    b = staged(
        "Example Labs releases Nimbus 2.5!", "https://mirror.example.net/x/123", f"<p>{LAUNCH}</p>", hours_ago=0.5
    )
    result, store, clusters = run([a, b])
    assert decisions(result)[b.id] == "exact_duplicate"
    assert store.items[b.id].duplicate_of_id == a.id
    cluster = cluster_of(clusters, a)
    assert cluster is cluster_of(clusters, b) and cluster.duplicate_count == 1 and cluster.article_count == 2


def test_same_arxiv_paper_under_abs_and_pdf_is_an_exact_duplicate():
    a = staged("Scaling laws for sparse models", "https://arxiv.org/abs/2611.01234v1", source="arxiv")
    b = staged("Scaling Laws for Sparse Models (v2)", "https://arxiv.org/pdf/2611.01234v2", source="news-a")
    result, store, _ = run([a, b])
    assert decisions(result)[b.id] == "exact_duplicate"
    assert store.items[b.id].processing["match"]["reasons"] == ["same_identifier:arxiv:2611.01234"]


def test_short_identical_text_is_not_hashed_into_an_exact_duplicate():
    # Two different weekly digests with the same generic title and a one-line blurb on different sites.
    a = staged("This week in AI", "https://a.example.com/weekly-45", "Our weekly roundup.", hours_ago=1)
    b = staged("This week in AI", "https://b.example.org/weekly", "Our weekly roundup.", hours_ago=2)
    result, _, clusters = run([a, b])
    assert set(decisions(result).values()) == {"primary"}
    assert cluster_of(clusters, a) is not cluster_of(clusters, b)


# --- near duplicates -----------------------------------------------------------------------------


def test_lightly_edited_copy_is_a_near_duplicate():
    edited = LAUNCH.replace("today released", "on Friday released").replace("paid users", "paying customers")
    a = staged("Example Labs releases Nimbus 2.5", "https://labs.example.com/nimbus", LAUNCH, source="lab-blog")
    b = staged("Example Labs releases Nimbus 2.5 model", "https://syndicate.example.org/nimbus", edited, hours_ago=0.5)
    result, store, clusters = run([a, b])
    assert decisions(result)[b.id] == "near_duplicate"
    assert store.items[b.id].duplicate_of_id == a.id
    ref = next(r for r in cluster_of(clusters, b).sources if r.item_id == b.id)
    assert ref.relation == "near_duplicate" and ref.match_score >= 0.5


def test_near_identical_title_on_same_site_without_excerpt_is_a_near_duplicate():
    a = staged("Example Labs opens a research office in Bengaluru", "https://news.example.com/a/1")
    b = staged("Example Labs opens research office in Bengaluru", "https://news.example.com/a/1-updated")
    result, _, clusters = run([a, b])
    assert sorted(decisions(result).values()) == ["near_duplicate", "primary"]
    assert len(clusters) == 1


def test_similar_title_alone_never_merges_across_sites_as_a_duplicate():
    a = staged("Example Labs opens a research office in Bengaluru", "https://news.example.com/a/1")
    b = staged("Example Labs opens a research office in Bengaluru", "https://other.example.org/b/2")
    result, store, _ = run([a, b])
    assert decisions(result)[b.id] != "near_duplicate"
    assert store.items[b.id].duplicate_of_id is None


def test_same_title_on_same_site_far_apart_is_not_a_duplicate():
    a = staged("Monthly model release notes", "https://news.example.com/notes/oct", hours_ago=24 * 30)
    b = staged("Monthly model release notes", "https://news.example.com/notes/nov", hours_ago=1)
    result, _, clusters = run([a, b])
    assert set(decisions(result).values()) == {"primary"}
    assert len(clusters) == 2


def test_different_numbers_in_similar_titles_are_not_duplicates():
    a = staged("Example Labs raises $2 billion in new funding", "https://news.example.com/f/1")
    b = staged("Example Labs raises $5 billion in new funding", "https://news.example.com/f/2")
    result, _, _ = run([a, b])
    assert decisions(result)[b.id] != "near_duplicate"


def test_different_model_versions_are_never_duplicates_or_one_story():
    a = staged("OpenAI launches GPT-5 for all users", "https://news.example.com/gpt-5", LAUNCH)
    b = staged("OpenAI launches GPT-5.1 for all users", "https://news.example.com/gpt-5-1", LAUNCH + " Updated.")
    result, _, clusters = run([a, b])
    assert decisions(result)[b.id] == "primary"
    assert cluster_of(clusters, a) is not cluster_of(clusters, b)


def test_product_version_parsing_and_conflicts():
    assert extract_products("Claude Opus 4.5 and Claude 3.5 Sonnet") == {"claude 4.5 opus", "claude 3.5 sonnet"}
    assert extract_products("Llama 3.1 405B") == {"llama 3.1"}
    assert extract_products("o3-mini") == {"openai-o 3 mini"}
    assert extract_products("Copilot") == set()
    assert conflicting_products({"gpt 5"}, {"gpt 5.1"}) == ["gpt"]
    assert conflicting_products({"gpt 5 mini"}, {"gpt 5"}) == []
    assert conflicting_products({"gemini 3 pro"}, {"llama 4"}) == []


# --- idempotency and bad input -------------------------------------------------------------------


def test_reprocessing_is_idempotent_and_cluster_ids_are_stable():
    items = [
        staged("Example Labs releases Nimbus 2.5", "https://labs.example.com/n", LAUNCH, source="lab-blog"),
        staged("Example Labs releases Nimbus 2.5", "https://mirror.example.net/n", LAUNCH, hours_ago=0.5),
    ]
    result, store, clusters = run(items)
    again = process_items(store.load_items(NOW, 100), thresholds=Thresholds(), now=NOW + timedelta(hours=1))
    assert again.updates == [] and again.counts["already_processed"] == 2
    _, _, clusters_again = run(list(store.items.values()))
    assert [c.cluster_id for c in clusters_again] == [c.cluster_id for c in clusters]
    # Same input in a fresh store gives the same cluster id (derived from the first item's dedup key).
    _, _, fresh = run([staged(i.title, i.original_url, i.excerpt, source=i.source_key) for i in items])
    assert fresh[0].cluster_id == clusters[0].cluster_id


def test_requeued_item_keeps_its_assignment():
    items = [staged("Example Labs releases Nimbus 2.5", "https://labs.example.com/n", LAUNCH)]
    _, store, clusters = run(items)
    item = store.items[items[0].id]
    item.processing_status = "pending"
    result = process_items(store.load_items(NOW, 10), thresholds=Thresholds(), now=NOW)
    assert result.counts["kept_assignment"] == 1 and result.updates[0].cluster_id == clusters[0].cluster_id


def test_empty_input():
    result, _, clusters = run([])
    assert result.updates == [] and clusters == [] and result.counts["loaded"] == 0


@pytest.mark.parametrize(
    ("title", "url", "reason"),
    [
        ("Untitled", "https://news.example.com/x", "generic_title"),
        ("", "https://news.example.com/x", "missing_title"),
        ("https://news.example.com/x", "https://news.example.com/x", "title_is_url"),
        ("A real title here", "http://169.254.169.254/latest/meta-data/", "unsafe_or_malformed_url"),
        ("A real title here", "https://localhost/admin", "unsafe_or_malformed_url"),
        ("A real title here", "https://user:pw@news.example.com/x", "unsafe_or_malformed_url"),
    ],
)
def test_invalid_items_are_discarded_with_a_reason_not_repaired(title, url, reason):
    item = staged("placeholder", "https://news.example.com/ok")
    item.title, item.original_url = title, url
    result, store, clusters = run([item])
    assert store.items[item.id].processing_status == "discarded"
    assert store.items[item.id].processing["reason"] == reason
    assert store.items[item.id].cluster_id is None and clusters == []
    # Discarded items are not loaded again.
    assert store.load_items(NOW, 10) == []


def test_malformed_metadata_is_tolerated():
    item = staged("Example Labs opens an office", "https://news.example.com/office", None, hours_ago=None)
    item.extra = {"newsletter_date": "not a date", "author": None}
    item.publisher = "   "
    _, _, clusters = run([item])
    (cluster,) = clusters
    ref = cluster.sources[0]
    assert ref.publisher is None and ref.published_at is None and ref.newsletter_date is None
    assert cluster.earliest_published_at is None and cluster.processing.time_basis == ["collected"]


def test_one_failing_item_does_not_stop_the_run(monkeypatch):
    import app.processing.pipeline as pipeline

    good = staged("Example Labs opens an office", "https://news.example.com/office")
    bad = staged("Example Labs closes an office", "https://news.example.com/close")
    real = pipeline.build_features

    def flaky(**kwargs):
        if kwargs["title"].startswith("Example Labs closes"):
            raise RuntimeError("boom")
        return real(**kwargs)

    monkeypatch.setattr(pipeline, "build_features", flaky)
    result, store, _ = run([good, bad])
    assert store.items[bad.id].processing_status == "failed"
    assert store.items[bad.id].processing == {"version": "3.2.0", "decision": "failed", "error_type": "RuntimeError"}
    assert store.items[good.id].processing_status == "processed"
    assert result.counts["failed"] == 1
