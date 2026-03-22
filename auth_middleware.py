"""
Path-based auth gate for task-related routes (JWT in httpOnly cookie or Authorization: Bearer).
Complements Depends(get_current_user), which loads the Person row and enforces ownership.
"""

from __future__ import annotations

import logging
import os

from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth_tokens import (
    COOKIE_ACCESS_NAME,
    auth_configured,
    decode_token,
    get_access_token_from_request,
)

logger = logging.getLogger("nudge.auth")


def _auth_denials_logged() -> bool:
    return os.getenv("AUTH_LOG_DENIALS", "").lower() in ("1", "true", "yes")


def _log_task_auth_denial(request: Request, path: str, reason: str) -> None:
    if not _auth_denials_logged():
        return
    auth_h = request.headers.get("authorization") or ""
    bearer_hint = auth_h.lower().startswith("bearer ") and len(auth_h) > 7
    cookie_hint = COOKIE_ACCESS_NAME in request.cookies
    logger.warning(
        "task_auth_denied method=%s path=%s reason=%s access_cookie_present=%s bearer_header_present=%s",
        request.method,
        path,
        reason,
        cookie_hint,
        bearer_hint,
    )


def _path_requires_task_auth(path: str) -> bool:
    p = path.split("?", 1)[0]
    if p in ("/tasks", "/tasks/"):
        return True
    if p == "/api/tasks/enrich":
        return True
    if p == "/api/suggestions":
        return True
    return False


class TaskAuthMiddleware(BaseHTTPMiddleware):
    """Return 401 before handler if task routes are called without a valid access JWT."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not _path_requires_task_auth(path):
            return await call_next(request)

        if not auth_configured():
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication is not configured (set JWT_SECRET_KEY, min 32 chars)."},
            )

        token = get_access_token_from_request(request)
        if not token:
            _log_task_auth_denial(request, path, "missing_token")
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            decode_token(token, expected_type="access")
        except JWTError:
            _log_task_auth_denial(request, path, "invalid_or_expired_token")
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
