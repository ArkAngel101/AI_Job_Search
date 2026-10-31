"""Local-only web security: Host allowlist, Origin check, required custom header."""
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

ALLOWED_HOSTS = {"127.0.0.1:8000", "localhost:8000"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
CUSTOM_HEADER = "x-forge-request"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def local_security(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject foreign Host headers (400) and cross-site or header-less writes (403)."""
    if request.headers.get("host", "").lower() not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Bad host"}, status_code=400)
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin.lower() not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Bad origin"}, status_code=403)
        if CUSTOM_HEADER not in request.headers:
            return JSONResponse({"detail": "Missing X-Forge-Request header"}, status_code=403)
    return await call_next(request)
