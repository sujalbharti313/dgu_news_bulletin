"""TLDR AI newsletter parsing from fixture emails (synthetic content, real layout conventions)."""

from app.ingestion.parsers.tldr import parse_tldr, parse_tldr_html, parse_tldr_text, resolve_link
from tests.ingestion_helpers import fixture


def test_html_extracts_articles_with_decoded_destinations():
    outcome = parse_tldr_html(fixture("tldr_ai.html").decode())
    titles = [a.title for a in outcome.articles]
    assert titles == ["Example Lab releases a test model", "test-agent", "Notes on scaling, without tracking"]
    first, repo, direct = outcome.articles
    assert first.url == "https://news.example.com/2026/11/model-launch?utm_source=tldrai"
    assert first.read_minutes == 4 and first.label == "4 minute read"
    assert first.description == (
        "Example Lab announced a fictional model for this test. It is said to do well on made-up benchmarks."
    )
    assert repo.url == "https://github.com/example-org/test-agent" and repo.read_minutes is None
    assert direct.url == "https://research.example.org/posts/scaling-notes"
    # Footer text never leaks into the last article.
    assert "referral" not in (direct.description or "") and "TLDR" not in (direct.description or "")
    assert outcome.sponsored == 1 and outcome.unresolved_links == 1


def test_scripts_and_styles_are_ignored():
    outcome = parse_tldr_html(fixture("tldr_ai.html").decode())
    assert all("evil" not in (a.description or "") and "color" not in (a.description or "") for a in outcome.articles)


def test_text_fallback():
    outcome = parse_tldr_text(fixture("tldr_ai.txt").decode())
    assert [a.url for a in outcome.articles] == [
        "https://news.example.com/2026/11/model-launch",
        "https://github.com/example-org/test-agent",
    ]
    assert outcome.articles[0].description == "Example Lab announced a fictional model for this test."
    assert outcome.sponsored == 1


def test_html_preferred_and_unknown_layout_warns():
    assert len(parse_tldr(fixture("tldr_ai.html").decode(), fixture("tldr_ai.txt").decode()).articles) == 3
    empty = parse_tldr("<p>Hello, no articles here</p>", "nothing")
    assert empty.articles == [] and empty.warnings


def test_resolve_link_rules():
    assert resolve_link("https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fa.example.com%2Fx/1/abc") == (
        "https://a.example.com/x"
    )
    assert resolve_link("https://tracking.tldrnewsletter.com/CL0/https:%2F%2Ftldr.tech%2Fai/1/abc") is None
    assert resolve_link("https://tracking.tldrnewsletter.com/other") is None
    assert resolve_link("https://tldr.tech/ai") is None
    assert resolve_link("mailto:dan@example.com") is None
    assert resolve_link("https://direct.example.com/a") == "https://direct.example.com/a"
