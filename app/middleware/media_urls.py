"""SEC-04: signs /media/... URLs in API responses so browsers can load them.

The Flutter app opens files with `Image.network(url)` and `launchUrl(url)`,
neither of which can send an Authorization header, while /media now requires
authentication. So for every JSON response to a request carrying a valid
bearer token, each string value that is a relative "/media/<path>" URL gets
`?t=<signed token>` appended (security.sign_media_path: bound to that path,
the caller's tenant and user, short expiry). The URL shape the client uses
is unchanged -- no frontend change needed.

The reverse also happens: `?t=...` is stripped from "/media/..." strings in
JSON request bodies, so a client that echoes a URL back (e.g. saving a form
that contains a file_url) never persists a token into the database.

Pure ASGI; must sit INSIDE GZipMiddleware (sees plain JSON) and inside
CacheHeaderMiddleware (so the ETag covers the signed body -- tokens are
time-bucketed, see sign_media_path, so ETags stay stable within a bucket).
"""

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import security

_RESPONSE_URL = re.compile(rb'"/media/([^"?\\]+)"')
_REQUEST_TOKEN = re.compile(rb'(/media/[^"?\\]+)\?t=[A-Za-z0-9_\-.]+')


class MediaUrlSigningMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        receive = await self._strip_request_tokens(scope, headers, receive)

        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = security.decode_access_token(auth[7:]) if auth.lower().startswith("bearer ") else None
        if token is None or not token.tenant_slug:
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                content_type = dict(message.get("headers", [])).get(b"content-type", b"")
                if not content_type.startswith(b"application/json"):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or passthrough or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            if b'"/media/' in body:
                def _sign(match: re.Match) -> bytes:
                    path = match.group(1).decode("utf-8", "replace")
                    signed = security.sign_media_path(path, token.tenant_slug, token.user_id)
                    return b'"/media/' + match.group(1) + b"?t=" + signed.encode() + b'"'

                body = _RESPONSE_URL.sub(_sign, body)
            new_headers = [
                (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
            ]
            new_headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": new_headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _strip_request_tokens(scope: Scope, headers: dict, receive: Receive) -> Receive:
        if scope["method"] not in ("POST", "PUT", "PATCH"):
            return receive
        if not headers.get(b"content-type", b"").startswith(b"application/json"):
            return receive
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # client disconnected mid-body -- hand the message on untouched
                async def replay_disconnect(msg=message) -> Message:
                    return msg
                return replay_disconnect
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if b"?t=" in body:
            body = _REQUEST_TOKEN.sub(rb"\1", body)
            scope["headers"] = [
                (k, v) for k, v in scope["headers"] if k.lower() != b"content-length"
            ] + [(b"content-length", str(len(body)).encode())]
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay
