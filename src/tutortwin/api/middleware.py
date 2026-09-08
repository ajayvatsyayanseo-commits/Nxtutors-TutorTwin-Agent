"""Request context and body-size middleware."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tutortwin.domain.errors import ErrorCode
from tutortwin.observability.logging import (
    correlation_id_var,
    get_logger,
    request_id_var,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"
CORRELATION_ID_HEADER = "x-correlation-id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds request/correlation IDs for the life of the request.

    An inbound ID is honoured so a trace survives across Lead Intake -> TutorTwin;
    otherwise one is generated. Both are echoed back on the response.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex}"
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or f"corr_{uuid.uuid4().hex}"
        request_token = request_id_var.set(request_id)
        correlation_token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            request_id_var.reset(request_token)
            correlation_id_var.reset(correlation_token)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies before they are parsed.

    Checks Content-Length first (cheap), then enforces the same ceiling while
    streaming so a chunked body cannot bypass the header check.
    """

    def __init__(self, app: object, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            return self._too_large()

        body = await request.body()
        if len(body) > self._max:
            return self._too_large()
        return await call_next(request)

    def _too_large(self) -> JSONResponse:
        logger.warning("request_too_large", max_bytes=self._max)
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": ErrorCode.PAYLOAD_TOO_LARGE,
                    "message": "Request body exceeds the configured limit.",
                }
            },
        )
