"""
Structured request logging middleware.
Logs every request with timing and user context.
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("api.requests")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs all requests with:
      - Request ID (unique per request)
      - Method + path
      - Status code
      - Duration
      - User ID (if available from header)

    Skip logging for health checks and static assets.
    """

    SKIP_PATHS = {"/health", "/docs", "/openapi.json",
                  "/redoc", "/favicon.svg"}

    async def dispatch(
        self, request: Request, call_next
    ) -> Response:
        # Skip noisy paths
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)
        if request.url.path.startswith("/assets/"):
            return await call_next(request)

        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()

        # Attach request_id for use in handlers
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(
                "req_id=%s method=%s path=%s "
                "status=500 duration_ms=%.1f ERROR",
                request_id,
                request.method,
                request.url.path,
                elapsed,
            )
            raise

        elapsed = (time.perf_counter() - start) * 1000
        user_id = request.headers.get("X-User-Id", "-")

        logger.info(
            "req_id=%s method=%s path=%s "
            "status=%d duration_ms=%.1f user=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            user_id,
        )

        # Attach request ID to response for tracing
        response.headers["X-Request-Id"] = request_id
        return response
