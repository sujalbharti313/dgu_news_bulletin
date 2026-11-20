"""Editorial admin authentication and authorisation.

- Admins sign in on the frontend with Supabase Auth (email + password). The browser sends the Supabase access
  token as `Authorization: Bearer <token>`. No cookies are used, so cross-site request forgery does not apply,
  and CORS only allows the configured frontend origins.
- The backend verifies every token with Supabase Auth itself (`GET {SUPABASE_URL}/auth/v1/user` with the
  project's publishable key), which validates signature, expiry and revocation whatever signing keys the project
  uses. A verified token is cached for at most ADMIN_AUTH_CACHE_SECONDS, keyed by a hash of the token.
- Being signed in is not enough: the user id must be an active row in `editorial_admins`, re-read from the
  database on every request. Rows are added only from the command line.
- Without SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY the admin editions API does not exist (404). The ingestion
  admin token (INGESTION_ADMIN_TOKEN) never grants editorial rights.
"""

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger("app.editions.auth")
MAX_TOKEN_LENGTH = 8192
MAX_CACHE = 1000


@dataclass(frozen=True)
class VerifiedUser:
    user_id: uuid.UUID
    email: str | None


class AuthUnavailable(Exception):
    """Supabase Auth could not be reached (not the user's fault)."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedUser | None: ...


class SupabaseTokenVerifier:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None, clock=time.monotonic):
        self.url = f"{settings.supabase_url}/auth/v1/user"
        self.key = settings.supabase_publishable_key.get_secret_value() if settings.supabase_publishable_key else ""
        self.ttl = settings.admin_auth_cache_seconds
        self.client = client or httpx.Client(timeout=settings.admin_auth_timeout_seconds, follow_redirects=False)
        self.clock = clock
        self._cache: dict[str, tuple[float, VerifiedUser]] = {}
        self._lock = threading.Lock()

    def verify(self, token: str) -> VerifiedUser | None:
        if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = self.clock()
        with self._lock:
            hit = self._cache.get(digest)
            if hit and hit[0] > now:
                return hit[1]
        try:
            response = self.client.get(self.url, headers={"apikey": self.key, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.warning("Supabase Auth unreachable (%s)", type(exc).__name__)
            raise AuthUnavailable() from None
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            logger.warning("Supabase Auth answered HTTP %s", response.status_code)
            raise AuthUnavailable()
        try:
            data = response.json()
            user = VerifiedUser(uuid.UUID(str(data["id"])), data.get("email"))
        except (ValueError, KeyError, TypeError):
            logger.warning("Supabase Auth returned an unreadable user")
            return None
        if self.ttl:
            with self._lock:
                if len(self._cache) >= MAX_CACHE:
                    self._cache.clear()
                self._cache[digest] = (now + self.ttl, user)
        return user


class WriteLimiter:
    """Per-admin fixed-window limit on write actions (in process memory)."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self._lock = threading.Lock()
        self._windows: dict[uuid.UUID, tuple[float, int]] = {}

    def allow(self, user_id: uuid.UUID) -> bool:
        now = self.clock()
        with self._lock:
            start, count = self._windows.get(user_id, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            count += 1
            self._windows[user_id] = (start, count)
            if len(self._windows) > MAX_CACHE:
                self._windows.clear()
            return count <= self.per_minute
