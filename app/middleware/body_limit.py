"""TC-D24 / EH-16: refuse oversized non-upload request bodies (413) before
they reach the app. File uploads (multipart/form-data) keep their own,
larger limit enforced by the upload endpoints (25 MB)."""

from starlette.responses import JSONResponse

MAX_JSON_BODY_BYTES = 1_000_000


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_JSON_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
        if content_type.startswith("multipart/form-data"):
            return await self.app(scope, receive, send)
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            response = JSONResponse(status_code=413, content={"detail": "Request body is too large."})
            return await response(scope, receive, send)
        # No/unknown Content-Length: count as the body streams in.
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            response = JSONResponse(status_code=413, content={"detail": "Request body is too large."})
            await response(scope, receive, send)


class _TooLarge(Exception):
    pass
