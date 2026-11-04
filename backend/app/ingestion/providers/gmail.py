"""Gmail newsletter ingestion (TLDR AI) over the Gmail REST API with OAuth 2.0.

Scope: https://www.googleapis.com/auth/gmail.readonly only. The server holds a refresh token (from
`python -m scripts.gmail_oauth`) and exchanges it for a short-lived access token each run; neither is
logged, stored in the database, or sent anywhere except Google's token and Gmail endpoints.

Incremental and idempotent: the query is narrowed with `after:<epoch>` from the cursor, and the ids of
processed messages are kept in the cursor (bounded), so a message is parsed once. Article-level dedup in
the store covers any overlap. Email bodies are parsed in memory and discarded; only article titles,
links and short blurbs leave this module.
"""

import base64
import logging
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses

from app.ingestion.errors import FetchError, GmailAuthError, IngestionError
from app.ingestion.http import SafeHttpClient
from app.ingestion.models import CollectResult, RawItem
from app.ingestion.parsers.tldr import parse_tldr
from app.ingestion.sources import GmailSourceConfig

logger = logging.getLogger("app.ingestion.gmail")

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_LIST_PAGES = 5
MAX_PROCESSED_IDS = 300
MAX_BODY_BYTES = 2_000_000
OVERLAP = timedelta(days=1)


class GmailClient:
    def __init__(self, http: SafeHttpClient, *, client_id: str, client_secret: str, refresh_token: str):
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None

    def _token(self) -> str:
        if self._access_token is None:
            try:
                response = self._http.post_form(
                    TOKEN_URL,
                    {
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                    },
                )
            except FetchError as exc:
                if exc.status_code in (400, 401):
                    raise GmailAuthError(
                        "Google rejected the Gmail refresh token or client (revoked, expired or wrong client). "
                        "Run `python -m scripts.gmail_oauth` again."
                    ) from exc
                raise
            token = response.json().get("access_token")
            if not token:
                raise GmailAuthError("Google's token response did not contain an access token.")
            self._access_token = token
        return self._access_token

    def _get(self, path: str, params: dict) -> dict:
        try:
            return self._http.get(
                f"{GMAIL_API}{path}", params=params, headers={"Authorization": f"Bearer {self._token()}"}
            ).json()
        except FetchError as exc:
            if exc.status_code == 401:
                raise GmailAuthError("Gmail rejected the access token.") from exc
            if exc.status_code == 403:
                raise GmailAuthError(
                    "Gmail denied access (check the gmail.readonly scope and API enablement)."
                ) from exc
            raise

    def list_message_ids(self, query: str, page_size: int) -> tuple[list[str], bool]:
        """Message ids matching `query`, newest first. Returns (ids, truncated)."""
        ids: list[str] = []
        token = None
        for _ in range(MAX_LIST_PAGES):
            params = {"q": query, "maxResults": page_size}
            if token:
                params["pageToken"] = token
            page = self._get("/messages", params)
            ids.extend(m["id"] for m in page.get("messages") or [] if isinstance(m.get("id"), str))
            token = page.get("nextPageToken")
            if not token:
                return ids, False
        return ids, True

    def get_message(self, message_id: str) -> dict:
        if not message_id.isalnum() or len(message_id) > 64:
            raise IngestionError("Unexpected Gmail message id format.")
        return self._get(f"/messages/{message_id}", {"format": "full"})


def _header(message: dict, name: str) -> str | None:
    for header in (message.get("payload") or {}).get("headers") or []:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return None


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw[:MAX_BODY_BYTES].decode("utf-8", errors="replace")


def extract_bodies(message: dict) -> tuple[str | None, str | None]:
    """(html, text) of the message, ignoring attachments."""
    html_body = text_body = None
    stack = [message.get("payload") or {}]
    while stack:
        part = stack.pop()
        stack.extend(reversed(part.get("parts") or []))
        if part.get("filename"):
            continue
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/html" and html_body is None:
            html_body = _decode(data)
        elif mime == "text/plain" and text_body is None:
            text_body = _decode(data)
    return html_body, text_body


def sender_allowed(message: dict, allowed: list[str]) -> bool:
    addresses = [addr.lower() for _, addr in getaddresses([_header(message, "From") or ""]) if addr]
    return len(addresses) == 1 and addresses[0] in allowed


def collect_gmail(
    source: GmailSourceConfig, client: GmailClient, cursor: dict | None, *, now: datetime, max_items: int
) -> CollectResult:
    cursor = cursor or {}
    processed: list[str] = [i for i in cursor.get("processed_ids") or [] if isinstance(i, str)]
    processed_set = set(processed)
    after_epoch = cursor.get("after_epoch")
    if not isinstance(after_epoch, int):
        after_epoch = int((now - timedelta(days=source.initial_lookback_days)).timestamp())

    query = f"({source.query}) after:{after_epoch}"
    ids, truncated = client.list_message_ids(query, page_size=min(100, source.max_messages * 2))
    new_ids = [i for i in ids if i not in processed_set]
    # Gmail lists newest first; work oldest first so a backlog drains in order across runs.
    new_ids.reverse()
    batch, leftover = new_ids[: source.max_messages], new_ids[source.max_messages :]

    counts = {
        "messages_listed": len(ids),
        "messages_new": len(new_ids),
        "messages_processed": 0,
        "messages_ignored_sender": 0,
        "sponsored_skipped": 0,
        "unresolved_links": 0,
        "over_limit": 0,
    }
    warnings: list[str] = []
    items: list[RawItem] = []
    newest_internal: int | None = None

    for message_id in batch:
        message = client.get_message(message_id)
        internal_ms = int(message.get("internalDate") or 0)
        newest_internal = max(newest_internal or 0, internal_ms)
        processed.append(message_id)
        if not sender_allowed(message, source.allowed_senders):
            counts["messages_ignored_sender"] += 1
            continue
        html_body, text_body = extract_bodies(message)
        outcome = parse_tldr(html_body, text_body)
        del html_body, text_body  # never kept beyond parsing
        counts["messages_processed"] += 1
        counts["sponsored_skipped"] += outcome.sponsored
        counts["unresolved_links"] += outcome.unresolved_links
        warnings.extend(f"message {message_id}: {w}" for w in outcome.warnings)
        newsletter_date = datetime.fromtimestamp(internal_ms / 1000, UTC).isoformat() if internal_ms else None
        for article in outcome.articles:
            if len(items) >= max_items:
                counts["over_limit"] += 1
                continue
            items.append(
                RawItem(
                    title=article.title,
                    url=article.url,
                    feed_identifier=f"gmail:{message_id}",
                    # The article's publisher is not stated in the newsletter; it is left unknown, not guessed.
                    publisher=None,
                    # The newsletter's send date is not the article's publication date, so it goes in extra.
                    published_at=None,
                    description=article.description,
                    extra={
                        "newsletter": source.name,
                        "newsletter_date": newsletter_date,
                        "label": article.label,
                        "read_minutes": article.read_minutes,
                    },
                )
            )

    next_after = after_epoch
    if newest_internal and not leftover and not truncated:
        next_after = max(after_epoch, int(newest_internal / 1000 - OVERLAP.total_seconds()))
    if leftover or truncated:
        warnings.append("more newsletters are waiting than max_messages allows; the rest are collected next run")
    return CollectResult(
        items=items,
        cursor={"after_epoch": next_after, "processed_ids": processed[-MAX_PROCESSED_IDS:]},
        counts=counts,
        warnings=warnings,
    )
