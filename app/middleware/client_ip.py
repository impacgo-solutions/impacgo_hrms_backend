"""API-02/C20: request-scoped client IP for the audit log.

A tiny pure-ASGI middleware stores the caller's IP in a ContextVar so
crud.create_audit_log can default its `ip_address` to it without touching
its hundreds of call sites. The IP is resolved exactly like the rate
limiter does (rate_limit._client_ip): X-Forwarded-For's first hop only when
RATE_LIMIT_TRUST_PROXY is set, else the socket peer. Context variables set
here propagate into sync endpoints/dependencies run in the threadpool.
"""

import ipaddress
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from .rate_limit import _client_ip

_current_client_ip: ContextVar[str | None] = ContextVar("current_client_ip", default=None)


def get_current_client_ip() -> str | None:
    """The current request's client IP, or None outside a request / when it
    isn't a valid IP address (core_audit_logs.ip_address is INET, so an
    invalid value like "unknown"/"testclient" must never be written)."""
    return _current_client_ip.get()


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


class ClientIPMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        token = _current_client_ip.set(_valid_ip(_client_ip(scope, headers)))
        try:
            await self.app(scope, receive, send)
        finally:
            _current_client_ip.reset(token)
