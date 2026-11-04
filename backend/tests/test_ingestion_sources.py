"""Source configuration validation, including the shipped config file."""

import pytest

from app.ingestion.errors import SourceConfigError
from app.ingestion.sources import GmailSourceConfig, RssSourceConfig, load_sources, parse_sources

RSS = """
[[sources]]
key = "{key}"
type = "rss"
name = "Feed"
category = "news"
url = "{url}"
"""


def test_shipped_configuration_is_valid():
    sources = load_sources("config/ingestion_sources.toml")
    keys = [s.key for s in sources]
    assert len(keys) == len(set(keys)) and sources
    assert any(isinstance(s, GmailSourceConfig) for s in sources)
    for source in sources:
        if isinstance(source, RssSourceConfig):
            assert source.url.startswith("https://")


def test_valid_rss_and_gmail():
    text = (
        RSS.format(key="a-feed", url="https://feeds.example.com/rss")
        + """
[[sources]]
key = "tldr"
type = "gmail"
name = "TLDR AI"
category = "newsletter"
query = "from:news@example.com"
allowed_senders = ["News@Example.com"]
"""
    )
    rss, gmail = parse_sources(text)
    assert rss.type == "rss" and rss.enabled
    assert gmail.allowed_senders == ["news@example.com"] and gmail.max_messages == 10


@pytest.mark.parametrize(
    "url",
    [
        "http://feeds.example.com/rss",
        "https://127.0.0.1/rss",
        "https://localhost/rss",
        "file:///etc/passwd",
        "https://u:p@feeds.example.com/",
        "https://feeds.example.com:8443/rss",
    ],
)
def test_unsafe_feed_urls_rejected(url):
    with pytest.raises(SourceConfigError, match="url"):
        parse_sources(RSS.format(key="bad", url=url))


def test_http_feed_needs_explicit_opt_in():
    text = RSS.format(key="plain", url="http://feeds.example.com/rss").replace(
        'category = "news"', 'category = "news"\nallow_http = true'
    )
    assert parse_sources(text)[0].allow_http


@pytest.mark.parametrize(
    "text, match",
    [
        (
            RSS.format(key="dup", url="https://a.example.com/") + RSS.format(key="dup", url="https://b.example.com/"),
            "duplicate source key",
        ),
        (
            RSS.format(key="one", url="https://a.example.com/") + RSS.format(key="two", url="https://a.example.com/"),
            "duplicate feed url",
        ),
        (RSS.format(key="Bad Key", url="https://a.example.com/"), "key"),
        (RSS.format(key="x", url="https://a.example.com/") + 'surprise = "field"\n', "surprise"),
        ('[[sources]]\nkey = "x"\ntype = "ftp"\nname = "n"\ncategory = "news"\n', "type"),
        ("not = [valid toml", "TOML"),
        (
            '[[sources]]\nkey = "g"\ntype = "gmail"\nname = "n"\ncategory = "newsletter"\nquery = "q"\n'
            'allowed_senders = ["not-an-email"]\n',
            "email",
        ),
    ],
)
def test_invalid_configurations(text, match):
    with pytest.raises(SourceConfigError, match=match):
        parse_sources(text)


def test_missing_file():
    with pytest.raises(SourceConfigError, match="not found"):
        load_sources("config/does-not-exist.toml")
