"""Gmail newsletter ingestion against a mocked Gmail API and OAuth token endpoint. No real credentials."""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from app.ingestion.errors import GmailAuthError
from app.ingestion.providers.gmail import GmailClient, collect_gmail, extract_bodies
from app.ingestion.sources import GmailSourceConfig
from tests.ingestion_helpers import fixture, make_http

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
SOURCE = GmailSourceConfig(
    key="tldr-ai",
    type="gmail",
    name="TLDR AI",
    category="newsletter",
    query='from:dan@tldrnewsletter.com subject:"TLDR AI"',
    allowed_senders=["dan@tldrnewsletter.com"],
    max_messages=10,
)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def message(mid: str, sender: str, internal_ms: int) -> dict:
    return {
        "id": mid,
        "internalDate": str(internal_ms),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": "TLDR AI 2026-11-03"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(fixture("tldr_ai.txt"))}},
                {"mimeType": "text/html", "body": {"data": b64(fixture("tldr_ai.html"))}},
                {"mimeType": "application/pdf", "filename": "x.pdf", "body": {"attachmentId": "a1"}},
            ],
        },
    }


class FakeGoogle:
    def __init__(self, messages: dict[str, dict], page_size: int = 2, token_status: int = 200):
        self.messages = messages
        self.page_size = page_size
        self.token_status = token_status
        self.queries: list[str] = []
        self.fetched: list[str] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["test-refresh"]
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "test-access", "expires_in": 3599})
        assert request.headers["authorization"] == "Bearer test-access"
        path = request.url.path
        if path.endswith("/messages"):
            self.queries.append(request.url.params["q"])
            ids = sorted(self.messages, reverse=True)  # newest first, like Gmail
            start = int(request.url.params.get("pageToken", 0))
            page = ids[start : start + self.page_size]
            body = {"messages": [{"id": i, "threadId": i} for i in page]}
            if start + self.page_size < len(ids):
                body["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, content=json.dumps(body))
        mid = path.rsplit("/", 1)[1]
        self.fetched.append(mid)
        return httpx.Response(200, json=self.messages[mid])


def client_for(fake: FakeGoogle) -> GmailClient:
    return GmailClient(make_http(fake), client_id="cid", client_secret="csecret", refresh_token="test-refresh")


def test_collects_newsletter_articles_with_pagination_and_sender_check():
    fake = FakeGoogle(
        {
            "18a1": message("18a1", "TLDR AI <dan@tldrnewsletter.com>", 1_793_650_000_000),
            "18a2": message("18a2", "Spoofer <dan@tldrnewsletter.com.evil.example>", 1_793_660_000_000),
            "18a3": message("18a3", "TLDR AI <dan@tldrnewsletter.com>", 1_793_670_000_000),
        }
    )
    result = collect_gmail(SOURCE, client_for(fake), None, now=NOW, max_items=100)
    assert fake.token_requests == 1  # one access token per run
    assert fake.queries[0].startswith('(from:dan@tldrnewsletter.com subject:"TLDR AI") after:')
    assert fake.fetched == ["18a1", "18a2", "18a3"]  # oldest first
    assert result.counts["messages_processed"] == 2 and result.counts["messages_ignored_sender"] == 1
    assert len(result.items) == 6 and result.counts["sponsored_skipped"] == 2
    item = result.items[0]
    assert item.feed_identifier == "gmail:18a1" and item.publisher is None and item.published_at is None
    assert item.extra["newsletter"] == "TLDR AI" and item.extra["newsletter_date"] == "2026-11-02T20:06:40+00:00"
    assert result.cursor["processed_ids"] == ["18a1", "18a2", "18a3"]
    assert result.cursor["after_epoch"] == 1_793_670_000 - 86_400


def test_second_run_skips_processed_messages():
    fake = FakeGoogle({"18a1": message("18a1", "dan@tldrnewsletter.com", 1_793_650_000_000)})
    first = collect_gmail(SOURCE, client_for(fake), None, now=NOW, max_items=100)
    second = collect_gmail(SOURCE, client_for(fake), first.cursor, now=NOW, max_items=100)
    assert fake.fetched == ["18a1"] and second.items == [] and second.counts["messages_new"] == 0
    assert f"after:{first.cursor['after_epoch']}" in fake.queries[1]


def test_backlog_beyond_max_messages_keeps_cursor_for_next_run():
    msgs = {f"18b{i}": message(f"18b{i}", "dan@tldrnewsletter.com", 1_762_100_000_000 + i) for i in range(5)}
    fake = FakeGoogle(msgs, page_size=10)
    source = SOURCE.model_copy(update={"max_messages": 2})
    cursor = {"after_epoch": 1_700_000_000, "processed_ids": []}
    result = collect_gmail(source, client_for(fake), cursor, now=NOW, max_items=100)
    assert fake.fetched == ["18b0", "18b1"]
    assert result.cursor["after_epoch"] == 1_700_000_000 and result.warnings


def test_revoked_refresh_token_is_an_auth_error_without_leaking_it():
    fake = FakeGoogle({}, token_status=400)
    with pytest.raises(GmailAuthError) as info:
        collect_gmail(SOURCE, client_for(fake), None, now=NOW, max_items=10)
    assert "test-refresh" not in info.value.safe_message and "csecret" not in info.value.safe_message


def test_extract_bodies_ignores_attachments():
    html_body, text_body = extract_bodies(message("1", "x@example.com", 0))
    assert "<html>" in html_body and text_body.startswith("TLDR AI")
