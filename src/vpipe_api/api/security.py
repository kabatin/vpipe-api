"""Request guards applied before routing: bearer token, body size, model and busy gates."""

from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from vpipe_api.api.errors import busy_response, error_response

if TYPE_CHECKING:
    from vpipe_api.jobs.queue import JobQueue
    from vpipe_api.workflows.base import WorkflowRegistry

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
_SUBMIT_PATH = re.compile(r"^/v1/workflows/([^/]+)/jobs$")
_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def host_name(host_header: str) -> str:
    """``[::1]:8765`` -> ``::1``, ``localhost:8765`` -> ``localhost``."""
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


class LoopbackOnlyMiddleware(BaseHTTPMiddleware):
    """Without a token the API trusts "this machine" — make sure that is what it gets.

    - A Host header that is not a loopback name means a browser was pointed at us through
      DNS rebinding (``attacker.example`` resolving to 127.0.0.1): refuse it.
    - Proxy headers mean a reverse proxy is republishing a token-less API: refuse it.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if host_name(request.headers.get("host", "")).lower() not in LOOPBACK_HOSTS:
            return error_response(403, "forbidden_host", "use 127.0.0.1 or localhost")
        if any(header in request.headers for header in PROXY_HEADERS):
            return error_response(
                403, "proxy_requires_token", "serving through a proxy requires VPIPE_API_TOKEN"
            )
        return await call_next(request)


class ModelGateMiddleware(BaseHTTPMiddleware):
    """Refuse a submit whose workflow's models are not on disk, before its body is read.

    A job would only fail later, or make vpipe look for weights that are not there; a client
    that polls for hours could not tell that from a slow run. The workflow stays listed.
    """

    def __init__(self, app: ASGIApp, registry: WorkflowRegistry, work_dir: Path) -> None:
        super().__init__(app)
        self._registry = registry
        self._work_dir = work_dir

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        match = _SUBMIT_PATH.match(request.url.path)
        workflow = self._registry.get(match.group(1)) if match is not None else None
        if request.method == "POST" and workflow is not None:
            missing = workflow.missing_models(self._work_dir)
            if missing:
                names = ", ".join(model.key for model in missing)
                return error_response(
                    409,
                    "model_not_installed",
                    f"model not downloaded on this server: {names} "
                    f"(run `vpipe-api setup models {workflow.id}` there)",
                    retryable=False,
                )
        return await call_next(request)


class BusyGateMiddleware(BaseHTTPMiddleware):
    """Answer ``429 busy`` for a submit before its body (up to ``max_body_mb``) is read.

    The queue re-checks capacity when the job is actually created; this only saves the
    upload and JSON parsing when the answer is already known. A resubmission whose
    Idempotency-Key already has a job passes through (it gets that job back, not a 429).
    """

    def __init__(self, app: ASGIApp, queue: JobQueue) -> None:
        super().__init__(app)
        self._queue = queue

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        match = _SUBMIT_PATH.match(request.url.path)
        if request.method == "POST" and match is not None:
            key = request.headers.get(IDEMPOTENCY_HEADER)
            if key is not None and _KEY.match(key) is None:
                key = None  # the route rejects it with a 422 after this
            retry_after = self._queue.refusal(match.group(1), key)
            if retry_after is not None:
                return busy_response(retry_after)
        return await call_next(request)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self._expected = token.encode()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        valid = scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.strip().encode("latin-1", "replace"), self._expected
        )
        if not valid:
            return error_response(
                401,
                "unauthorized",
                "missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies up front; chunked uploads without a length are refused."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length")
            if length is None:
                if request.headers.get("transfer-encoding", "").lower() == "chunked":
                    return error_response(411, "length_required", "send a Content-Length")
            elif not length.isdigit() or int(length) > self._max:
                return error_response(
                    413,
                    "payload_too_large",
                    f"request body over {self._max // (1024 * 1024)} MB",
                )
        return await call_next(request)
