"""
JWT creation/verification and password hashing (bcrypt).
Used by main.py for /auth/* and Depends(get_current_user).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import bcrypt
from jose import JWTError, jwt
from starlette.requests import Request
from starlette.responses import Response


def _password_bytes(plain: str) -> bytes:
    """
    Bcrypt only accepts the first 72 bytes of the password. For longer UTF-8 passwords,
    pre-hash with SHA-256 so registration/login stay consistent (Passlib is incompatible
    with bcrypt>=4.1; we use the `bcrypt` package directly).
    """
    raw = plain.encode("utf-8")
    if len(raw) <= 72:
        return raw
    return hashlib.sha256(raw).digest()

JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    """Read on each use so .env is respected after load_dotenv (import order / cwd)."""
    return (os.getenv("JWT_SECRET_KEY") or "").strip()


def _access_expire_minutes() -> int:
    return int(os.getenv("JWT_ACCESS_EXPIRE_MINUTES", "15"))


def _refresh_expire_days() -> int:
    return int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))


COOKIE_ACCESS_NAME = os.getenv("AUTH_ACCESS_COOKIE_NAME", "access_token")
COOKIE_REFRESH_NAME = os.getenv("AUTH_REFRESH_COOKIE_NAME", "refresh_token")


def auth_configured() -> bool:
    s = _jwt_secret()
    return bool(s and len(s) >= 32)


def auth_return_tokens_in_body() -> bool:
    """
    When true, login/register/refresh responses also include access_token + refresh_token in JSON.
    Use when mobile Safari / ITP blocks cross-site httpOnly cookies; the SPA should then send
    Authorization: Bearer <access_token> (and keep refresh in memory/sessionStorage — XSS risk).
    """
    return os.getenv("AUTH_RETURN_TOKENS_IN_BODY", "").lower() in ("1", "true", "yes")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_password_bytes(plain), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        h = hashed.encode("ascii")
        return bcrypt.checkpw(_password_bytes(plain), h)
    except (ValueError, TypeError):
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(subject_user_id: UUID) -> str:
    if not auth_configured():
        raise RuntimeError("JWT_SECRET_KEY must be set (min 32 chars)")
    secret = _jwt_secret()
    expire = _now() + timedelta(minutes=_access_expire_minutes())
    payload: dict[str, Any] = {
        "sub": str(subject_user_id),
        "typ": "access",
        "exp": expire,
        "iat": _now(),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def create_refresh_token(subject_user_id: UUID) -> str:
    if not auth_configured():
        raise RuntimeError("JWT_SECRET_KEY must be set (min 32 chars)")
    secret = _jwt_secret()
    expire = _now() + timedelta(days=_refresh_expire_days())
    payload: dict[str, Any] = {
        "sub": str(subject_user_id),
        "typ": "refresh",
        "exp": expire,
        "iat": _now(),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_token(token: str, *, expected_type: str) -> dict[str, Any]:
    if not auth_configured():
        raise JWTError("JWT not configured")
    secret = _jwt_secret()
    payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    if payload.get("typ") != expected_type:
        raise JWTError("Wrong token type")
    return payload


# Browser cookies (httpOnly). For cross-site SPAs use COOKIE_SAMESITE=none + COOKIE_SECURE=true.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
_raw_samesite = (os.getenv("COOKIE_SAMESITE", "lax") or "lax").lower()
if _raw_samesite not in ("lax", "strict", "none"):
    _raw_samesite = "lax"
COOKIE_SAMESITE: str = _raw_samesite
# e.g. `.example.com` so cookies are sent to both `api.example.com` and `app.example.com` (use with care).
COOKIE_DOMAIN: Optional[str] = os.getenv("AUTH_COOKIE_DOMAIN") or None
if COOKIE_DOMAIN is not None:
    COOKIE_DOMAIN = COOKIE_DOMAIN.strip() or None


def get_access_token_from_request(request: Request) -> Optional[str]:
    """Read access JWT from httpOnly cookie or `Authorization: Bearer`."""
    token = request.cookies.get(COOKIE_ACCESS_NAME)
    if token:
        return token
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def auth_request_debug_context(request: Request) -> dict[str, Any]:
    """
    Non-sensitive fields for diagnosing missing/invalid JWTs.
    Never includes cookie values or bearer tokens.
    """
    auth = request.headers.get("authorization")
    if not auth:
        auth_scheme = "absent"
    elif auth.lower().startswith("bearer "):
        rest = auth[7:].strip()
        auth_scheme = "bearer" if rest else "bearer_empty"
    else:
        auth_scheme = "non_bearer"

    return {
        "cookie_header_present": bool(request.headers.get("cookie")),
        "access_cookie_present": COOKIE_ACCESS_NAME in request.cookies,
        "authorization_scheme": auth_scheme,
        "origin": request.headers.get("origin"),
        "user_agent": (request.headers.get("user-agent") or "")[:160],
    }


def attach_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    common = {
        "httponly": True,
        "secure": COOKIE_SECURE,
        "samesite": COOKIE_SAMESITE,
        "path": "/",
    }
    if COOKIE_DOMAIN:
        common["domain"] = COOKIE_DOMAIN
    response.set_cookie(
        key=COOKIE_ACCESS_NAME,
        value=access_token,
        max_age=_access_expire_minutes() * 60,
        **common,
    )
    response.set_cookie(
        key=COOKIE_REFRESH_NAME,
        value=refresh_token,
        max_age=_refresh_expire_days() * 86400,
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    # Must match attributes used in set_cookie or browsers may not clear the cookie.
    response.delete_cookie(
        COOKIE_ACCESS_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,  # type: ignore[arg-type]
    )
    response.delete_cookie(
        COOKIE_REFRESH_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,  # type: ignore[arg-type]
    )
