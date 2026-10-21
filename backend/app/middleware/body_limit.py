from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.schemas.trace import MAX_REQUEST_BYTES


def _too_large() -> dict:
    return {"error": "request_too_large", "max_bytes": MAX_REQUEST_BYTES,
            "message": f"a request body is at most {MAX_REQUEST_BYTES} bytes"}


class BodyLimit:
    """413 for a body over MAX_REQUEST_BYTES: by its Content-Length before any of it is read, else once what was
    read passes it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"")
        if length.isdigit() and int(length) > MAX_REQUEST_BYTES:
            detail = _too_large()
            response = JSONResponse(status_code=413, content={"error": {
                "code": detail["error"], "message": detail["message"], "details": detail}})
            return await response(scope, receive, send)
        read = 0

        async def counted():
            nonlocal read
            message = await receive()
            read += len(message.get("body", b""))
            if read > MAX_REQUEST_BYTES:
                raise HTTPException(413, detail=_too_large())
            return message

        await self.app(scope, counted, send)
