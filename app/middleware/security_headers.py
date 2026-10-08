"""L-36: baseline security headers on every HTTP response, /media included.

- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY (+ CSP frame-ancestors 'none')
- Referrer-Policy: no-referrer (signed /media?t= URLs must never leak via Referer)
- Content-Security-Policy:
    * JSON / files: ``default-src 'none'`` -- nothing an API response or an
      uploaded file (HTML/SVG served from /media) could execute or load.
      Images and PDFs still render when opened directly.
    * Server-rendered HTML pages (candidate portal, email-action
      confirmation): inline styles, same-origin/https images and fonts, form
      posts; still no scripts and no framing.
    * /docs, /redoc (only mounted in dev, see main.py) are left alone --
      Swagger UI needs its CDN scripts.
- Strict-Transport-Security when the request arrived over HTTPS (directly,
  or X-Forwarded-Proto=https behind a trusted proxy) or APP_ENV is a real
  deployment.

A header the endpoint already set is never overwritten.
"""

from __future__ import annotations

from ..config import settings

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
MEDIA_CSP = (
    "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
    "object-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline' https:; img-src 'self' data: https:; "
    "font-src 'self' data: https:; form-action 'self' https:; frame-ancestors 'none'; base-uri 'none'"
)
HSTS = "max-age=31536000; includeSubDomains"
_DOCS_PREFIXES = ("/docs", "/redoc")


def _is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    if settings.rate_limit_trust_proxy:
        for k, v in scope.get("headers") or []:
            if k == b"x-forwarded-proto":
                return v.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path") or ""
        is_docs = path.startswith(_DOCS_PREFIXES) or path == "/openapi.json"
        is_media = path.startswith("/media/")
        hsts = _is_https(scope) or not settings.is_dev

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}

                def add(name: bytes, value: str):
                    if name not in present:
                        headers.append((name, value.encode("latin-1")))

                add(b"x-content-type-options", "nosniff")
                add(b"referrer-policy", "no-referrer")
                if not is_docs:
                    add(b"x-frame-options", "DENY")
                    content_type = ""
                    for k, v in headers:
                        if k.lower() == b"content-type":
                            content_type = v.decode("latin-1").lower()
                            break
                    if is_media:
                        csp = MEDIA_CSP
                    elif content_type.startswith("text/html"):
                        csp = HTML_CSP
                    else:
                        csp = API_CSP
                    add(b"content-security-policy", csp)
                add(b"cross-origin-resource-policy", "cross-origin")
                if hsts:
                    add(b"strict-transport-security", HSTS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
