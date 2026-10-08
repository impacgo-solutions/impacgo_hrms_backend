"""L-23: every Reports & Analytics endpoint (/api/reports/*, including the
compliance / overtime / leave-withdrawal reports owned by other routers)
answers an inverted period (from after to) with 422 instead of an empty
200. Runs before routing/auth -- it only looks at the query string."""

from __future__ import annotations

import datetime
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

_PAIRS = (("from_date", "to_date"), ("date_from", "date_to"), ("start_date", "end_date"))
_DETAIL = "from_date must be on or before to_date."


def inverted_range(query_string: str) -> bool:
    params = parse_qs(query_string, keep_blank_values=False)
    for a, b in _PAIRS:
        va, vb = params.get(a), params.get(b)
        if not va or not vb:
            continue
        try:
            if datetime.date.fromisoformat(va[0][:10]) > datetime.date.fromisoformat(vb[0][:10]):
                return True
        except ValueError:
            continue  # the endpoint's own validation reports bad dates
    return False


class ReportRangeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and (scope.get("path") or "").startswith("/api/reports/")
            and inverted_range((scope.get("query_string") or b"").decode("latin-1"))
        ):
            return await JSONResponse(status_code=422, content={"detail": _DETAIL})(scope, receive, send)
        return await self.app(scope, receive, send)
