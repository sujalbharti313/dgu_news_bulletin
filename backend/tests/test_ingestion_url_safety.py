"""SSRF guard: schemes, credentials, ports, internal names and every private/reserved address form."""

import pytest

from app.ingestion.errors import UnsafeUrlError
from app.ingestion.url_safety import check_peer_address, is_public_ip, resolve_public_addresses, validate_url

ALLOWED = [
    "https://rss.arxiv.org/rss/cs.AI",
    "https://example.com:443/feed.xml",
    "https://93.184.215.14/feed",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/feed",
]

REJECTED = [
    "",
    "ftp://example.com/feed",
    "file:///etc/passwd",
    "gopher://example.com",
    "javascript:alert(1)",
    "http://example.com/feed",  # plain http is opt-in
    "https://user:pass@example.com/feed",
    "https://example.com@169.254.169.254/",
    "https://example.com:8080/feed",
    "https://localhost/feed",
    "https://api.localhost/feed",
    "https://printer.local/",
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://intranet/",
    "https://127.0.0.1/",
    "https://127.1/",
    "https://2130706433/",  # 127.0.0.1 as an integer
    "https://0x7f000001/",
    "https://0177.0.0.1/",
    "https://0.0.0.0/",
    "https://10.0.0.5/",
    "https://172.16.3.4/",
    "https://192.168.1.1/",
    "https://169.254.169.254/latest/meta-data/",
    "https://100.64.0.1/",  # carrier-grade NAT
    "https://198.18.0.1/",  # benchmarking
    "https://224.0.0.1/",  # multicast
    "https://[::1]/",
    "https://[fe80::1]/",
    "https://[fc00::1]/",
    "https://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    "https://[::ffff:169.254.169.254]/",
    "https://[64:ff9b::a00:1]/",  # NAT64 of 10.0.0.1
    "https://[2002:a00:1::]/",  # 6to4 of 10.0.0.1
    "https://example.com/a b",
    "https://example.com/\x00",
    "https://" + "a" * 2100 + ".com/",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url", REJECTED)
def test_rejected_urls(url):
    with pytest.raises(UnsafeUrlError):
        validate_url(url)


def test_http_only_when_allowed():
    assert validate_url("http://example.com/feed", allow_http=True)
    with pytest.raises(UnsafeUrlError):
        validate_url("http://10.0.0.1/feed", allow_http=True)


@pytest.mark.parametrize(
    "answers",
    [["10.0.0.1"], ["93.184.215.14", "127.0.0.1"], ["::1"], ["fd00::5"], ["169.254.169.254"], []],
)
def test_dns_answers_must_all_be_public(answers):
    with pytest.raises(UnsafeUrlError):
        resolve_public_addresses("feeds.example.com", 443, lambda h, p: answers)


def test_dns_failure_is_unsafe():
    def boom(host, port):
        raise OSError("no such host")

    with pytest.raises(UnsafeUrlError):
        resolve_public_addresses("feeds.example.com", 443, boom)


def test_public_dns_answers_pass():
    assert resolve_public_addresses("feeds.example.com", 443, lambda h, p: ["93.184.215.14", "2606:2800::1"])


def test_peer_address_check():
    check_peer_address(("93.184.215.14", 443))
    check_peer_address(None)
    with pytest.raises(UnsafeUrlError):
        check_peer_address(("10.1.2.3", 443))


def test_is_public_ip_examples():
    import ipaddress

    assert is_public_ip(ipaddress.ip_address("8.8.8.8"))
    assert not is_public_ip(ipaddress.ip_address("192.0.2.1"))  # documentation range
