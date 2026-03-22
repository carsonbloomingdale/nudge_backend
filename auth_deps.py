"""Shared FastAPI dependencies (JWT → Person row). Used by main and feature routers."""

from __future__ import annotations

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
