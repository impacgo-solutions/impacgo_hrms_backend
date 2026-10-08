"""
Per-client API rate limiting (token bucket), as a pure ASGI middleware.

Who is limited
──────────────
Every HTTP request is charged against a bucket keyed by the caller:
  - a valid JWT  -> its user id + tenant ("u:<tenant>:<user_id>"), so users
    sharing one office NAT IP don't throttle each other;
  - otherwise    -> the client IP ("ip:<addr>").
Login, password and upload endpoints get their own, stricter buckets (see
_RULES) on top of that -- login is always keyed by IP, since the caller has
no token yet and this is what slows down password guessing (per-account
lockout is separate -- see auth_state.py / routers/auth.py).

What the client sees
────────────────────
Over the limit: 429 {"detail": ..., "retry_after": N} with a Retry-After
header. Every limited response also carries X-RateLimit-Limit /
X-RateLimit-Remaining / X-RateLimit-Reset (seconds until the tightest bucket
is full again; on a 429, seconds until the next request is allowed) so the
frontend can back off before hitting it.

Scope and limits
────────────────
Buckets live in process memory: fine for the single-process uvicorn this
app runs as today, but each worker keeps its own counts under
`--workers N` / multiple replicas -- move the store to Redis before
scaling out. WebSocket connections, CORS preflights (OPTIONS) and /health
are never limited. All numbers come from settings (RATE_LIMIT_* in .env).
"""

import json
import math
import re
import time
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import security
from ..config import settings

_EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


@dataclass(frozen=True)
class _Rule:
    name: str
    per_minute: int
    by_ip: bool  # True: key on client IP even when a token is present


def _rules() -> list[tuple[str, re.Pattern[str], _Rule]]:
    """(method, path pattern, rule) -- first match wins; built from settings
    at startup so .env overrides apply."""
    auth = _Rule("auth", settings.rate_limit_login_per_minute, by_ip=True)
    sensitive = _Rule("sensitive", settings.rate_limit_sensitive_per_minute, by_ip=False)
    upload = _Rule("upload", settings.rate_limit_upload_per_minute, by_ip=False)
    # Manual emails sent as the HR mailbox (routers/email.py) -- per user.
    email = _Rule("email", settings.rate_limit_email_per_minute, by_ip=False)
    return [
        ("POST", re.compile(r"^/api/admin/email/test$"), email),
        ("POST", re.compile(r"^/api/hr/documents/send-email$"), email),
        ("POST", re.compile(r"^/api/payroll/payslips/[^/]+/email$"), email),
        ("POST", re.compile(r"^/api/offers/[^/]+/email$"), email),
        ("POST", re.compile(r"^/api/auth/login$"), auth),
        ("POST", re.compile(r"^/api/super-admin/login$"), auth),
        ("POST", re.compile(r"^/api/super-admin/change-password$"), sensitive),
        ("POST", re.compile(r"^/api/(users|hrms-admins)/[^/]+/reset-password$"), sensitive),
        ("POST", re.compile(r"/(documents|attachments|photo)$"), upload),
        ("POST", re.compile(r"^/api/(policy-documents|document-templates)$"), upload),
    ]


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float, now: float):
        self.tokens = capacity
        self.updated = now


class RateLimiter:
    """Token buckets: `per_minute` capacity, refilled continuously at
    per_minute/60 tokens per second -- allows a full minute's burst (e.g. the
    Approvals screen's ~20-request fan-out) but caps sustained throughput."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._last_sweep = time.monotonic()

    def hit(self, key: str, per_minute: int) -> tuple[bool, int, int]:
        """Consumes one token. Returns (allowed, remaining, retry_after_s)
        -- on success the third value is the seconds until the bucket is
        full again (the X-RateLimit-Reset value)."""
        now = time.monotonic()
        capacity = float(per_minute)
        rate = capacity / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(capacity, now)
        else:
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
        self._maybe_sweep(now)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, int(bucket.tokens), math.ceil((capacity - bucket.tokens) / rate)
        return False, 0, max(1, math.ceil((1.0 - bucket.tokens) / rate))

    def _maybe_sweep(self, now: float) -> None:
        # Buckets idle for 2+ minutes are full again -- identical to a fresh
        # one -- so dropping them loses nothing and bounds memory.
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        stale = [k for k, b in self._buckets.items() if now - b.updated > 120]
        for k in stale:
            del self._buckets[k]

    def reset(self) -> None:
        self._buckets.clear()


limiter = RateLimiter()


def _client_ip(scope: Scope, headers: dict[bytes, bytes]) -> str:
    if settings.rate_limit_trust_proxy:
        forwarded = headers.get(b"x-forwarded-for")
        if forwarded:
            return forwarded.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _token_identity(headers: dict[bytes, bytes]) -> str | None:
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if not auth.lower().startswith("bearer "):
        return None
    token = security.decode_access_token(auth[7:])
    if token is None:
        return None
    return f"u:{token.tenant_slug or 'platform'}:{token.user_id}"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.rules = _rules()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not settings.rate_limit_enabled
            or scope["method"] == "OPTIONS"
            or scope["path"] in _EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        method, path = scope["method"], scope["path"]
        ip = _client_ip(scope, headers)
        identity = _token_identity(headers)

        # Every request pays into the caller's general bucket...
        if identity is not None:
            checks = [(f"default:{identity}", settings.rate_limit_per_minute)]
        else:
            checks = [(f"anon:ip:{ip}", settings.rate_limit_anon_per_minute)]
        # ...and into an endpoint-specific one when a stricter rule matches.
        for rule_method, pattern, rule in self.rules:
            if method == rule_method and pattern.search(path):
                who = f"ip:{ip}" if rule.by_ip or identity is None else identity
                checks.append((f"{rule.name}:{who}", rule.per_minute))
                break

        limit, remaining, reset = 0, 0, 0
        for key, per_minute in checks:
            allowed, left, retry_after = limiter.hit(key, per_minute)
            if not allowed:
                await self._reject(send, per_minute, retry_after)
                return
            # Report the tightest bucket this request touched.
            if limit == 0 or left < remaining:
                limit, remaining, reset = per_minute, left, retry_after

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"x-ratelimit-limit", str(limit).encode()),
                    (b"x-ratelimit-remaining", str(remaining).encode()),
                    (b"x-ratelimit-reset", str(reset).encode()),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(send: Send, limit: int, retry_after: int) -> None:
        body = json.dumps(
            {
                "detail": f"Too many requests. Please try again in {retry_after} second"
                + ("" if retry_after == 1 else "s") + ".",
                "retry_after": retry_after,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry_after).encode()),
                    (b"x-ratelimit-limit", str(limit).encode()),
                    (b"x-ratelimit-remaining", b"0"),
                    (b"x-ratelimit-reset", str(retry_after).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
