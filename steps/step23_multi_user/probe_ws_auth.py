#!/usr/bin/env python
"""Does a `BaseHTTPMiddleware` gate cover the WebSocket? Measured, not assumed.

    uv run python probe_ws_auth.py

`auth.py` says the console's gate is raw ASGI "because of the socket", and that
claim is worth exactly as much as the measurement behind it. So this builds two
apps with the same routes -- one HTTP route and one WebSocket route -- and puts
a different gate in front of each:

* `BaseHTTPMiddleware`, the way most FastAPI tutorials add authentication;
* the console's own `AuthMiddleware`, raw ASGI.

Then it knocks on both doors of both apps with no credential at all and prints
what happened. Offline: `TestClient` speaks ASGI in-process, no port is bound.

The interesting cell is the top right. If it says the socket opened, then a
console guarded that way authenticates every REST call and streams the model's
output, the commands it runs and their results to anybody who can name a
thread.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


def build(kind: str) -> FastAPI:
    app = FastAPI()

    @app.get("/api/secret")
    def secret() -> dict[str, str]:
        return {"secret": "the REST route ran"}

    @app.websocket("/ws/secret")
    async def socket(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("the socket route ran")
        await ws.close()

    if kind == "base":

        async def deny(request: Any, call_next: Any) -> Any:
            return JSONResponse({"detail": "not signed in"}, status_code=401)

        app.add_middleware(BaseHTTPMiddleware, dispatch=deny)
    else:

        class Deny:
            def __init__(self, app: Any) -> None:
                self.app = app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] not in ("http", "websocket"):
                    # `lifespan` goes past. The first version of this probe
                    # left it out and the app would not start -- which is the
                    # cost of raw ASGI stated honestly: you handle every scope
                    # type yourself, including the one that is not a request.
                    await self.app(scope, receive, send)
                    return
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": "no"})
                    return
                body = b'{"detail":"not signed in"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})

        app.add_middleware(Deny)
    return app


def knock(app: FastAPI) -> tuple[str, str]:
    with TestClient(app) as client:
        http = f"HTTP {client.get('/api/secret').status_code}"
        try:
            with client.websocket_connect("/ws/secret") as ws:
                got = ws.receive_text()
            socket = f"OPENED, received {got!r}"
        except Exception as exc:
            socket = f"refused ({type(exc).__name__})"
    return http, socket


def main() -> int:
    print(f"{'gate':<22} {'GET /api/secret':<18} ws /ws/secret")
    print("-" * 78)
    for kind, label in (("base", "BaseHTTPMiddleware"), ("asgi", "raw ASGI")):
        http, socket = knock(build(kind))
        print(f"{label:<22} {http:<18} {socket}")
    print()
    print("Both gates deny every HTTP request. Only one of them is a gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
