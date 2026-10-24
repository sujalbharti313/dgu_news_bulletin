import pytest

from app.services.urls import canonicalize_url


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://Example.TEST/post/", "https://example.test/post"),
        ("http://www.example.test/post", "https://example.test/post"),
        ("https://example.test:443/post#section", "https://example.test/post"),
        ("https://example.test/post?utm_source=x&utm_medium=y&fbclid=z", "https://example.test/post"),
        ("https://example.test/p?b=2&a=1", "https://example.test/p?a=1&b=2"),
    ],
)
def test_equivalent_urls_canonicalise_identically(a, b):
    assert canonicalize_url(a) == canonicalize_url(b)


def test_content_query_parameters_are_kept():
    assert canonicalize_url("https://example.test/watch?v=abc") != canonicalize_url("https://example.test/watch?v=def")


def test_non_default_ports_are_kept():
    assert canonicalize_url("https://example.test:8443/a") == "https://example.test:8443/a"


@pytest.mark.parametrize("bad", ["ftp://example.test/a", "/relative/path", "example.test"])
def test_non_http_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        canonicalize_url(bad)
