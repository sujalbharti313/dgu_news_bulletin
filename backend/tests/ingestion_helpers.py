"""Shared helpers for ingestion tests: fixtures, a fake public DNS resolver and mock HTTP transports."""

from pathlib import Path

import httpx

from app.ingestion.http import SafeHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "ingestion"
PUBLIC_IP = "93.184.215.14"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


def make_http(handler, *, resolver=public_resolver, sleeps: list | None = None, **kwargs) -> SafeHttpClient:
    """SafeHttpClient over httpx.MockTransport: no sockets, no DNS, and recorded instead of real sleeps."""
    recorded = sleeps if sleeps is not None else []
    return SafeHttpClient(
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        sleep=recorded.append,
        jitter=lambda: 1.0,
        **kwargs,
    )
