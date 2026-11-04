"""SSRF protection for every URL the ingestion system fetches or stores.

Two layers:
1. `validate_url` (no network): scheme, credentials, port, length, obviously internal host names, and IP
   literals in any notation (including legacy forms such as `0x7f.1` or `2130706433`).
2. `resolve_public_addresses` (DNS): every address a host resolves to must be publicly routable.
   `http.py` also checks the address actually connected to, which narrows DNS-rebinding windows.

Article URLs found inside feeds and newsletters get layer 1 only, because Stage 3.1 never fetches them.
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from app.ingestion.errors import UnsafeUrlError

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = {"http": {80}, "https": {443}}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp", ".localdomain")
BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal"}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    # NAT64 well-known prefix 64:ff9b::/96 carries an IPv4 address in the low 32 bits.
    if ip in ipaddress.IPv6Network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_ip(embedded):
            return False
    # is_global excludes private, loopback, link-local, CGNAT (100.64/10), documentation, benchmarking,
    # reserved and unspecified ranges. Multicast is checked separately because it can be "global".
    return ip.is_global and not ip.is_multicast


def _parse_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Legacy IPv4 forms that resolvers accept: 2130706433, 0x7f000001, 0177.0.0.1, 127.1
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def validate_url(url: str, *, allow_http: bool = False) -> str:
    """Return the URL unchanged if it is acceptable to fetch or store, else raise UnsafeUrlError."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL is empty.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is too long.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise UnsafeUrlError("URL contains whitespace or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL is malformed.") from exc
    scheme = parts.scheme.lower()
    allowed = {"https", "http"} if allow_http else {"https"}
    if scheme not in allowed:
        raise UnsafeUrlError(f"URL scheme {scheme or '(none)'!r} is not allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("URL has no host.")
    if port is not None and port not in ALLOWED_PORTS[scheme]:
        raise UnsafeUrlError("Non-standard ports are not allowed.")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError("Internal host names are not allowed.")
    ip = _parse_ip_literal(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise UnsafeUrlError("Private, loopback, link-local and internal addresses are not allowed.")
    elif "." not in host:
        # Single-label names (e.g. "intranet", "db") only resolve inside private networks.
        raise UnsafeUrlError("Single-label host names are not allowed.")
    return url


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def resolve_public_addresses(host: str, port: int, resolver: Resolver = system_resolver) -> list[IPAddress]:
    """Resolve `host` and require every address to be public (one private answer fails the whole host)."""
    try:
        raw = resolver(host, port)
    except OSError as exc:
        raise UnsafeUrlError("Host name could not be resolved.") from exc
    if not raw:
        raise UnsafeUrlError("Host name could not be resolved.")
    addresses = []
    for value in raw:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        if not is_public_ip(ip):
            raise UnsafeUrlError("Host resolves to a private, loopback, link-local or internal address.")
        addresses.append(ip)
    return addresses


def check_peer_address(value: object) -> None:
    """Validate the peer address of an open connection (`server_addr` from httpcore), if known."""
    if not value:
        return
    host = value[0] if isinstance(value, tuple) else value
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return
    if not is_public_ip(ip):
        raise UnsafeUrlError("Connection was made to a private, loopback, link-local or internal address.")
