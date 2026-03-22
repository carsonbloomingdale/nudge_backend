"""
Path-based auth gate for task-related routes (JWT in httpOnly cookie or Authorization: Bearer).
Complements Depends(get_current_user), which loads the Person row and enforces ownership.
"""

from __future__ import annotations

import logging

from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth_tokens import (
    auth_configured,
    auth_request_debug_context,
    decode_token,
    get_access_token_from_request,
)

logger = logging.getLogger(__name__)


def _path_requires_task_auth(path: str) -> bool:
    p = path.split("?", 1)[0]
    if p in ("/tasks", "/tasks/"):
        return True
    if p == "/api/tasks/enrich":
        return True
    if p == "/api/suggestions":
        return True
    if p.startswith("/api/journals"):
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
        client_host = request.client.host if request.client else None
        debug_ctx = auth_request_debug_context(request)
        if not token:
            logger.info(
                "Task auth 401 not_authenticated reason=missing_token method=%s path=%s client=%s detail=%s",
                request.method,
                path,
                client_host,
                debug_ctx,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            decode_token(token, expected_type="access")
        except JWTError as exc:
            logger.info(
                "Task auth 401 invalid_token reason=invalid_or_expired_token method=%s path=%s client=%s jwt_error=%s detail=%s",
                request.method,
                path,
                client_host,
                str(exc),
                debug_ctx,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)