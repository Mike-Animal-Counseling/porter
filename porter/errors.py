"""Unified API errors. Every error response is {error: {code, message}} (PRD §11)."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from .schemas import ErrorOut


class PorterError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "") -> None:
        self.message = message or self.code
        super().__init__(self.message)


class NotFound(PorterError):
    status_code = 404
    code = "not_found"


class InvalidInput(PorterError):
    status_code = 422
    code = "invalid_input"


class Conflict(PorterError):
    status_code = 409
    code = "conflict"


class SyncNotAvailable(PorterError):
    """P1 auto-sync stub (PRD §9.5): endpoint reserved, not implemented in v1."""

    status_code = 501
    code = "sync_not_available"


class InsufficientScope(PorterError):
    status_code = 403
    code = "insufficient_scope"


class InvalidScope(PorterError):
    """Unknown or malformed scope requested (PRD §11)."""

    status_code = 400
    code = "invalid_scope"


class InvalidGrant(PorterError):
    """OAuth token endpoint: bad code / refresh token / client credentials."""

    status_code = 400
    code = "invalid_grant"


class Unauthorized(PorterError):
    """Missing or invalid bearer token (no valid AuthContext)."""

    status_code = 401
    code = "invalid_token"


class TokenRevoked(PorterError):
    """Token was revoked (blacklist hit) or refresh reuse detected (T2)."""

    status_code = 401
    code = "token_revoked"


class RateLimited(PorterError):
    """T5: per-client rate limit exceeded."""

    status_code = 429
    code = "rate_limited"


class ServiceNotReady(PorterError):
    """Repository/pool not initialized yet (startup in progress)."""

    status_code = 503
    code = "not_ready"


async def porter_error_handler(_: Request, exc: PorterError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorOut(error={"code": exc.code, "message": exc.message}).model_dump(),
    )
