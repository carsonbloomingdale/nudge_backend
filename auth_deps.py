"""Shared FastAPI dependencies (JWT → Person row). Used by main and feature routers."""

from __future__ import annotations

import os
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from jose import JWTError
import models
from auth_tokens import auth_configured, decode_token, get_access_token_from_request
from database import DbSession

db_dependency = DbSession


def get_current_user(request: Request, db: db_dependency) -> models.Person:
    if not auth_configured():
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured (set JWT_SECRET_KEY, min 32 chars).",
        )
    token = get_access_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token, expected_type="access")
        uid = UUID(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.query(models.Person).filter(models.Person.user_id == uid).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


CurrentUser = Annotated[models.Person, Depends(get_current_user)]


def require_admin_user(request: Request, db: db_dependency) -> models.Person:
    user = get_current_user(request, db)
    if bool(user.account_locked):
        raise HTTPException(status_code=403, detail="Account is locked")
    role = (user.role or "user").strip().lower()
    if role not in {"admin", "support_agent"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    # Optional low-friction MFA hook for admin endpoints.
    if bool(user.mfa_enabled):
        expected = (os.getenv("ADMIN_MFA_BYPASS_CODE") or "").strip()
        code = (request.headers.get("x-admin-mfa-code") or "").strip()
        if expected and code != expected:
            raise HTTPException(status_code=403, detail="Admin MFA check failed")
    return user


AdminUser = Annotated[models.Person, Depends(require_admin_user)]
