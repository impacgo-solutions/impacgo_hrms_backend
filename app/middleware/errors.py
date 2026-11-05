"""ERR-02: turn unhandled exceptions into the JSON 500 body INSIDE the CORS
middleware.

FastAPI's `@app.exception_handler(Exception)` runs in Starlette's outermost
ServerErrorMiddleware -- outside CORSMiddleware -- so its 500 carried no
Access-Control-Allow-Origin header and the browser app saw a network error
instead of the body (and its request_id). This middleware is added just
inside CORSMiddleware (see main.py) and produces the same logged, generic
body via the shared `build_500` callable, so CORS headers get added on the
way out.
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp, build_500: Callable[[Request, Exception], Awaitable[Response]]) -> None:
        self.app = app
        self.build_500 = build_500

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001
            if response_started:
                raise
            response = await self.build_500(Request(scope), exc)
            await response(scope, receive, send)
