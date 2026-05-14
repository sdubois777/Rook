"""
Security headers middleware.
Applied to every response automatically.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security headers to every HTTP response.
    Protects against clickjacking, MIME sniffing,
    XSS, and information leakage.
    """

    async def dispatch(
        self, request: Request, call_next
    ) -> Response:
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Referrer policy — don't leak full URL
        response.headers["Referrer-Policy"] = (
            "strict-origin-when-cross-origin"
        )

        # Remove server identification
        if "server" in response.headers:
            del response.headers["server"]
        if "x-powered-by" in response.headers:
            del response.headers["x-powered-by"]

        # Content Security Policy
        # Allows: same origin, Clerk (auth), Railway CDN
        if not request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' "
                "https://clerk.accounts.dev "
                "https://challenges.cloudflare.com; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self' https://api.clerk.dev "
                "wss:; "
                "frame-src https://clerk.accounts.dev;"
            )

        return response
