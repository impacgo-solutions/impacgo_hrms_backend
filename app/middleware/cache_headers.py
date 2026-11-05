"""
HTTP caching middleware for the FastAPI backend.

Adds ETag, Cache-Control, Last-Modified, and Vary headers to successful GET
responses, and returns HTTP 304 Not Modified when the client sends a matching
If-None-Match header.

Why this matters
────────────────
Without cache headers the browser treats every API response as non-cacheable
and re-downloads unchanged data on every navigation. With ETags the browser
can revalidate cheaply (304 with an empty body instead of a full response),
and for slow-changing reference endpoints (departments, branches, roles) a
5-minute max-age lets the Flutter app skip the network entirely.
"""

import hashlib
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Endpoints whose data changes at most a few times per day.
# These receive a short max-age so the browser can skip the network entirely.
_REFERENCE_ENDPOINTS: set[str] = {
    "/api/organization/departments",
    "/api/organization/branches",
    "/api/organization/profile",
    "/api/roles",
    "/api/permissions",
    "/api/designations",
}

_LAST_MODIFIED = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())


class CacheHeaderMiddleware(BaseHTTPMiddleware):
    """Inject ETag + Cache-Control on GET 200 responses; honour If-None-Match."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Only annotate successful GET responses.
        if request.method != "GET" or response.status_code != 200:
            return response

        # Buffer the body so we can hash it and potentially suppress it.
        body = b"".join([chunk async for chunk in response.body_iterator])
        etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'

        cc = self._cache_control(str(request.url.path))

        # Return 304 when the client already holds the current version.
        client_etag = request.headers.get("if-none-match")
        if client_etag and client_etag == etag:
            return Response(
                status_code=304,
                headers={
                    "etag": etag,
                    "cache-control": cc,
                    "vary": "Authorization, Accept-Encoding",
                },
            )

        # Re-emit the full response with caching headers added.
        headers = dict(response.headers)
        headers["etag"] = etag
        headers["last-modified"] = _LAST_MODIFIED
        headers["cache-control"] = cc
        headers["vary"] = "Authorization, Accept-Encoding"

        return Response(
            content=body,
            status_code=200,
            headers=headers,
            media_type=response.media_type,
        )

    @staticmethod
    def _cache_control(path: str) -> str:
        if path in _REFERENCE_ENDPOINTS:
            return "private, max-age=300, must-revalidate"
        return "private, no-cache, must-revalidate"
