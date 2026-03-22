import re
from datetime import datetime
from typing import List, Annotated, Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from uuid import UUID
from database import SessionLocal, engine, ensure_auth_columns, ensure_person_profile_columns
import models
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError
import asyncio
import json
import os
import sys
import random
import time
from collections import defaultdict, deque
from threading import Lock
from urllib import error, request

from auth_middleware import TaskAuthMiddleware
from auth_tokens import (
    attach_auth_cookies,
    auth_configured,
    auth_return_tokens_in_body,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    COOKIE_REFRESH_NAME,
    decode_token,
    get_access_token_from_request,
    hash_password,
    verify_password,
)


def _parse_cors_config() -> tuple[list[str], Optional[str], bool]:
    """
    Browsers send an exact `Origin` (scheme + host + port, no path, usually no trailing slash).
    `CORS_ORIGINS` must match that string exactly — we strip whitespace and trailing `/` on each entry.
    Optional `CORS_ORIGIN_REGEX` (full match) helps preview deploys, e.g. https://.*\\.vercel\\.app
    """
    raw = (os.getenv("CORS_ORIGINS") or "*").strip()
    regex = (os.getenv("CORS_ORIGIN_REGEX") or "").strip() or None

    if not raw or raw == "*":
        origins: list[str] = [] if regex else ["*"]
    else:
        origins = []
        for part in raw.split(","):
            o = part.strip().rstrip("/")
            if o:
                origins.append(o)

    # Credentials + cookie auth require explicit origins (not *) so Allow-Origin can echo the request Origin.
    allow_credentials = not (origins == ["*"] and regex is None)

    return origins, regex, allow_credentials


app = FastAPI(title="Nudge API")

_cors_origins, _cors_origin_regex, _allow_credentials = _parse_cors_config()

_cors_kwargs: dict = {
    "allow_credentials": _allow_credentials,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
if _cors_origin_regex:
    _cors_kwargs["allow_origin_regex"] = _cors_origin_regex
_cors_kwargs["allow_origins"] = _cors_origins if _cors_origins else []

app.add_middleware(CORSMiddleware, **_cors_kwargs)
app.add_middleware(TaskAuthMiddleware)


class PersonalityTrait(BaseModel):
    label: str
    task_id: int
    trait_id: int


class TaskBase(BaseModel):
    sentiment: str
    category: str
    label: str
    context: str
    user_id: UUID
    time_of_day: str
    amount_of_time: str
    day_of_week: str


class TaskCreateBody(BaseModel):
    """POST /tasks/ body — user comes from JWT only (do not trust client user_id)."""

    sentiment: str
    category: str
    label: str
    context: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str


class TaskModel(TaskBase):
    task_id: int

    model_config = ConfigDict(from_attributes=True)


class PersonBase(BaseModel):
    user_name: str
    email: str
    person_tasks: List[TaskModel]


class PersonModel(PersonBase):
    user_id: UUID

    model_config = ConfigDict(from_attributes=True)


class CreateUserRequest(BaseModel):
    """Legacy username-only signup (no password). Prefer POST /auth/register."""

    username: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("username cannot be blank")
        return s


class UserPublic(BaseModel):
    user_id: UUID
    user_name: str
    email: str

    model_config = ConfigDict(from_attributes=True)


class AuthSessionResponse(UserPublic):
    """Same as UserPublic; optional token fields when AUTH_RETURN_TOKENS_IN_BODY is enabled."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[Literal["bearer"]] = None


class RefreshOkResponse(BaseModel):
    ok: bool = True
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[Literal["bearer"]] = None


def _auth_session_response(person: models.Person, access: str, refresh: str) -> AuthSessionResponse:
    base = UserPublic.model_validate(person).model_dump()
    if auth_return_tokens_in_body():
        return AuthSessionResponse(
            **base,
            access_token=access,
            refresh_token=refresh,
            token_type="bearer",
        )
    return AuthSessionResponse(**base)


class AuthMeResponse(BaseModel):
    """Profile for SPA — map from `id` / `user_id` / `sub`; `username` mirrors `user_name`."""

    id: UUID
    user_id: UUID
    sub: str
    username: str
    user_name: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_e164: Optional[str] = None
    timezone: Optional[str] = None
    sms_opt_in: bool = False
    phone_verified_at: Optional[datetime] = None


_PHONE_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _normalize_optional_e164(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    s = str(v).strip()
    if not s:
        return None
    if not _PHONE_E164_RE.fullmatch(s):
        raise ValueError(
            "phone_e164 must be E.164 (+ and digits, max 15 digits after +). "
            "Numbers are unverified until SMS verification is implemented."
        )
    return s


def _normalize_optional_timezone(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        ZoneInfo(s)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone name (e.g. America/New_York)") from exc
    return s


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Minimum 8 characters (align with FE validation).",
    )
    first_name: Optional[str] = Field(None, max_length=128)
    last_name: Optional[str] = Field(None, max_length=128)
    phone_e164: Optional[str] = None
    timezone: Optional[str] = Field(None, max_length=64)
    sms_opt_in: bool = False

    @field_validator("username")
    @classmethod
    def username_strip(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("username cannot be blank")
        return s

    @field_validator("email")
    @classmethod
    def email_norm(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("first_name", "last_name")
    @classmethod
    def register_names(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s if s else None

    @field_validator("phone_e164")
    @classmethod
    def register_phone(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_e164(v)

    @field_validator("timezone")
    @classmethod
    def register_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_timezone(v)


class PatchMeRequest(BaseModel):
    """PATCH /auth/me — only fields present in the body are updated (partial update)."""

    first_name: Optional[str] = Field(None, max_length=128)
    last_name: Optional[str] = Field(None, max_length=128)
    phone_e164: Optional[str] = None
    timezone: Optional[str] = Field(None, max_length=64)
    sms_opt_in: Optional[bool] = None
    email: Optional[str] = Field(None, min_length=3, max_length=254)
    username: Optional[str] = Field(None, min_length=1, max_length=128)

    @field_validator("first_name", "last_name")
    @classmethod
    def patch_names(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s if s else None

    @field_validator("phone_e164")
    @classmethod
    def patch_phone(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_e164(v)

    @field_validator("timezone")
    @classmethod
    def patch_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_timezone(v)

    @field_validator("email")
    @classmethod
    def patch_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().lower()

    @field_validator("username")
    @classmethod
    def patch_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("username cannot be blank")
        return s


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    email: Optional[str] = None
    username: Optional[str] = None

    @model_validator(mode="after")
    def need_identifier(self) -> "LoginRequest":
        if not (self.email or self.username):
            raise ValueError("Provide email or username")
        return self

    @field_validator("email")
    @classmethod
    def email_opt_norm(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        return v.strip().lower()

    @field_validator("username")
    @classmethod
    def username_opt_strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        s = v.strip()
        return s or None


class OpenAIRequestMeta(BaseModel):
    model: str
    retries_used: int


class EnrichTaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=300)
    taskHistory: List[dict[str, Any]] = Field(default_factory=list, max_length=50)


class SuggestionRequest(BaseModel):
    taskHistory: List[dict[str, Any]] = Field(default_factory=list, max_length=50)


class EnrichedTask(BaseModel):
    sentiment: str
    category: str
    label: str
    context: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str
    personality_traits: List[str] = Field(default_factory=list)


class SuggestionPayload(BaseModel):
    reccomendedTask: str
    context: str


class EnrichTaskResponse(BaseModel):
    task: EnrichedTask
    meta: OpenAIRequestMeta


class SuggestionResponse(BaseModel):
    suggestion: SuggestionPayload
    meta: OpenAIRequestMeta


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Annotated[Session, Depends(get_db)]

models.Base.metadata.create_all(bind=engine)
ensure_auth_columns(engine)
ensure_person_profile_columns(engine)


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


def _auth_me_response(user: models.Person) -> AuthMeResponse:
    uid = user.user_id
    sid = str(uid)
    return AuthMeResponse(
        id=uid,
        user_id=uid,
        sub=sid,
        username=user.user_name,
        user_name=user.user_name,
        email=user.email or "",
        first_name=user.first_name,
        last_name=user.last_name,
        phone_e164=user.phone_e164,
        timezone=user.timezone,
        sms_opt_in=bool(user.sms_opt_in),
        phone_verified_at=user.phone_verified_at,
    )

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_SUGGESTION_TEMPERATURE = float(os.getenv("OPENAI_SUGGESTION_TEMPERATURE", "0.85"))
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "15"))
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "2"))

RATE_LIMIT_REQUESTS = int(os.getenv("API_RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_limiter_lock = Lock()
_rate_limiter_store: defaultdict[str, deque[float]] = defaultdict(deque)


def _trim_task_history(task_history: List[dict[str, Any]]) -> List[dict[str, str]]:
    trimmed: List[dict[str, str]] = []
    for item in task_history[-20:]:
        if not isinstance(item, dict):
            continue
        trimmed.append(
            {
                "label": str(item.get("label", ""))[:160],
                "category": str(item.get("category", ""))[:80],
                "sentiment": str(item.get("sentiment", ""))[:40],
                "context": str(item.get("context", ""))[:220],
            }
        )
    return trimmed


def _enforce_rate_limit(client_key: str) -> None:
    now = time.time()
    with _rate_limiter_lock:
        bucket = _rate_limiter_store[client_key]
        while bucket and bucket[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_REQUESTS:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, try again soon.")
        bucket.append(now)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    stripped = (raw_text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(stripped[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _normalize_enriched_task(raw_payload: dict[str, Any], original_task: str) -> EnrichedTask:
    payload = raw_payload.get("task", raw_payload) if isinstance(raw_payload, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    personality_traits = payload.get("personality_traits", payload.get("personalityTraits", []))
    normalized_traits: List[str] = []
    if isinstance(personality_traits, list):
        for trait in personality_traits:
            if isinstance(trait, str):
                normalized_traits.append(trait[:80])
            elif isinstance(trait, dict) and trait.get("label"):
                normalized_traits.append(str(trait.get("label"))[:80])

    sentiment = str(payload.get("sentiment", "neutral")).lower().strip()
    if sentiment not in {"positive", "neutral", "negative"}:
        sentiment = "neutral"

    label = str(payload.get("label", "")).strip() or original_task[:200]

    return EnrichedTask(
        sentiment=sentiment,
        category=str(payload.get("category", "other")).strip()[:80] or "other",
        label=label[:200],
        context=str(payload.get("context", "")).strip()[:300],
        time_of_day=str(payload.get("time_of_day", payload.get("timeOfDay", "unspecified"))).strip()[:40]
        or "unspecified",
        amount_of_time=str(payload.get("amount_of_time", payload.get("amountOfTime", "unspecified"))).strip()[:40]
        or "unspecified",
        day_of_week=str(payload.get("day_of_week", payload.get("dayOfWeek", "unspecified"))).strip()[:40]
        or "unspecified",
        personality_traits=normalized_traits[:5],
    )


def _normalize_suggestion(raw_payload: dict[str, Any]) -> SuggestionPayload:
    payload = raw_payload.get("suggestion", raw_payload) if isinstance(raw_payload, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    recommendation = (
        payload.get("reccomendedTask")
        or payload.get("recommendedTask")
        or payload.get("task")
        or "Take a short break and choose one focused task."
    )
    context = payload.get("context") or "Built from your recent activity."

    return SuggestionPayload(
        reccomendedTask=str(recommendation).strip()[:220],
        context=str(context).strip()[:350],
    )


async def _openai_chat_completion(
    system_prompt: str, user_prompt: str, *, temperature: float = 0.2
) -> tuple[dict[str, Any], int]:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI service is not configured.")

    body = {
        "model": OPENAI_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error: Optional[Exception] = None
    for attempt in range(OPENAI_RETRIES + 1):
        req = request.Request(OPENAI_URL, data=payload, headers=headers, method="POST")
        try:
            response_bytes = await asyncio.to_thread(
                request.urlopen,
                req,
                timeout=OPENAI_TIMEOUT_SECONDS,
            )
            raw = response_bytes.read().decode("utf-8")
            parsed = json.loads(raw)
            content = parsed["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(chunk.get("text", "")) for chunk in content if isinstance(chunk, dict))
            return _extract_json_object(str(content)), attempt
        except error.HTTPError as exc:
            err_text = ""
            try:
                err_text = exc.read().decode("utf-8", errors="replace")[:4000]
            except Exception:
                pass
            openai_msg: Optional[str] = None
            openai_type: Optional[str] = None
            try:
                ej = json.loads(err_text)
                e = ej.get("error")
                if isinstance(e, dict):
                    openai_msg = e.get("message")
                    openai_type = e.get("type") or e.get("code")
            except json.JSONDecodeError:
                pass

            if exc.code == 429:
                last_error = exc
                if attempt < OPENAI_RETRIES:
                    retry_after: Optional[float] = None
                    if exc.headers:
                        try:
                            retry_after = float(exc.headers.get("Retry-After", ""))
                        except (TypeError, ValueError):
                            retry_after = None
                    base = (2**attempt) * 1.0 + random.uniform(0, 0.5)
                    wait = min(retry_after if retry_after is not None else base, 45.0)
                    await asyncio.sleep(wait)
                    continue
                if openai_type == "insufficient_quota" or (
                    openai_msg and "quota" in openai_msg.lower()
                ):
                    detail = (
                        "OpenAI quota exceeded (billing). Add credits or check your plan at "
                        "https://platform.openai.com/account/billing"
                    )
                else:
                    detail = openai_msg or (
                        "OpenAI rate limit — wait a minute and try again, or reduce request frequency."
                    )
                raise HTTPException(status_code=429, detail=detail)

            if 500 <= exc.code < 600:
                last_error = exc
            else:
                detail = openai_msg or f"OpenAI request failed with status {exc.code}."
                raise HTTPException(status_code=502, detail=detail)
        except (error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc

        if attempt < OPENAI_RETRIES:
            await asyncio.sleep((2**attempt) * 0.5 + random.uniform(0, 0.2))

    raise HTTPException(status_code=502, detail=f"OpenAI request failed after retries: {last_error}")


def _build_enrich_prompts(task: str, task_history: List[dict[str, Any]]) -> tuple[str, str]:
    system_prompt = (
        "You extract task metadata from short user notes. Return strict JSON only with this exact shape: "
        '{"task":{"sentiment":"positive|neutral|negative","category":"string","label":"string","context":"string",'
        '"time_of_day":"string","amount_of_time":"string","day_of_week":"string","personality_traits":["string"]}}.'
    )
    user_prompt = json.dumps(
        {
            "task": task,
            "recent_task_history": _trim_task_history(task_history),
            "rules": [
                "Use short concise text.",
                "If unknown, use 'unspecified'.",
                "personality_traits should have up to 5 items.",
            ],
        }
    )
    return system_prompt, user_prompt


def _build_suggestion_prompts(task_history: List[dict[str, Any]]) -> tuple[str, str]:
    trimmed = _trim_task_history(task_history)
    has_signal = any(
        (row.get("label") or "").strip() or (row.get("context") or "").strip() for row in trimmed
    )
    system_prompt = (
        "You recommend one concrete next task for a productivity app. "
        "Ground it in recent_task_history when possible. "
        "Return strict JSON only with this exact shape: "
        '{"suggestion":{"reccomendedTask":"string","context":"string"}}.'
    )
    user_prompt = json.dumps(
        {
            "recent_task_history": trimmed,
            "history_has_specific_tasks": has_signal,
            "rules": [
                "reccomendedTask: one short imperative line (about 12 words or fewer).",
                "context: one sentence that ties the suggestion to patterns in recent_task_history.",
                "Prefer a logical follow-up, unblocker, or natural break given their categories and sentiments.",
                "Do not assume facts not supported by recent_task_history (pets, children, commute, health) "
                "unless labels or context clearly mention them.",
                "Avoid clichéd generic wellness filler (e.g. walk your dog, drink more water) unless history relates.",
                "Do not merely repeat the latest task label; choose a distinct next step unless repeating is clearly useful.",
                "If history_has_specific_tasks is false, suggest a neutral productive micro-task that needs no private details.",
                "Avoid unsafe or medical/financial advice.",
            ],
        }
    )
    return system_prompt, user_prompt


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Auth (password + JWT in httpOnly cookies; Bearer header also supported) ---


@app.post("/auth/register", response_model=AuthSessionResponse, response_model_exclude_none=True)
async def auth_register(body: RegisterRequest, db: db_dependency, response: Response):
    if not auth_configured():
        raise HTTPException(
            status_code=503,
            detail="Set JWT_SECRET_KEY (min 32 chars) to enable authentication.",
        )
    if db.query(models.Person).filter(models.Person.user_name == body.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")
    if db.query(models.Person).filter(models.Person.email == body.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    person = models.Person(
        user_name=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        phone_e164=body.phone_e164,
        timezone=body.timezone,
        sms_opt_in=body.sms_opt_in,
    )
    db.add(person)
    db.commit()
    db.refresh(person)
    access = create_access_token(person.user_id)
    refresh = create_refresh_token(person.user_id)
    attach_auth_cookies(response, access, refresh)
    return _auth_session_response(person, access, refresh)


@app.post("/auth/login", response_model=AuthSessionResponse, response_model_exclude_none=True)
async def auth_login(body: LoginRequest, db: db_dependency, response: Response):
    if not auth_configured():
        raise HTTPException(
            status_code=503,
            detail="Set JWT_SECRET_KEY (min 32 chars) to enable authentication.",
        )
    user: Optional[models.Person] = None
    if body.email:
        user = db.query(models.Person).filter(models.Person.email == body.email).first()
    elif body.username:
        user = db.query(models.Person).filter(models.Person.user_name == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access = create_access_token(user.user_id)
    refresh = create_refresh_token(user.user_id)
    attach_auth_cookies(response, access, refresh)
    return _auth_session_response(user, access, refresh)


@app.post("/auth/logout")
async def auth_logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@app.get("/auth/me", response_model=AuthMeResponse)
async def auth_me(user: CurrentUser):
    """Current session profile (requires access cookie or Bearer)."""
    return _auth_me_response(user)


@app.patch("/auth/me", response_model=AuthMeResponse)
async def auth_patch_me(user: CurrentUser, body: PatchMeRequest, db: db_dependency):
    """Partial profile update (allowlisted fields only)."""
    updates = body.model_dump(exclude_unset=True)
    if "username" in updates:
        updates["user_name"] = updates.pop("username")
    if "user_name" in updates:
        # Load by value first, then compare ids in Python — SQLite + postgresql.UUID
        # can mis-handle `column != uuid` in SQL while equality works.
        other = (
            db.query(models.Person)
            .filter(models.Person.user_name == updates["user_name"])
            .first()
        )
        if other is not None and other.user_id != user.user_id:
            raise HTTPException(status_code=409, detail="Username already exists")
    if "email" in updates:
        other = (
            db.query(models.Person)
            .filter(models.Person.email == updates["email"])
            .first()
        )
        if other is not None and other.user_id != user.user_id:
            raise HTTPException(status_code=409, detail="Email already registered")
    if "phone_e164" in updates:
        new_phone = updates["phone_e164"]
        if new_phone != user.phone_e164:
            updates["phone_verified_at"] = None
    for key, val in updates.items():
        setattr(user, key, val)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _auth_me_response(user)


@app.post("/auth/refresh", response_model=RefreshOkResponse, response_model_exclude_none=True)
async def auth_refresh(request: Request, response: Response, db: db_dependency):
    if not auth_configured():
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    rt: Optional[str] = request.cookies.get(COOKIE_REFRESH_NAME)
    if not rt:
        ct = request.headers.get("content-type") or ""
        if "json" in ct.lower():
            try:
                body_json: Any = await request.json()
            except Exception:
                body_json = None
            if isinstance(body_json, dict):
                raw = body_json.get("refresh_token") or body_json.get("refreshToken")
                if isinstance(raw, str):
                    rt = raw.strip() or None
    if not rt:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    try:
        payload = decode_token(rt, expected_type="refresh")
        uid = UUID(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = db.query(models.Person).filter(models.Person.user_id == uid).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    new_access = create_access_token(user.user_id)
    new_refresh = create_refresh_token(user.user_id)
    attach_auth_cookies(response, new_access, new_refresh)
    if auth_return_tokens_in_body():
        return RefreshOkResponse(
            ok=True,
            access_token=new_access,
            refresh_token=new_refresh,
            token_type="bearer",
        )
    return RefreshOkResponse(ok=True)


# --- Tasks (protected by TaskAuthMiddleware + Depends(get_current_user)) ---


@app.post("/tasks/", response_model=TaskModel)
async def create_task(body: TaskCreateBody, db: db_dependency, user: CurrentUser):
    data = body.model_dump()
    data["user_id"] = user.user_id
    db_transaction = models.Task(**data)
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction


@app.get("/tasks", response_model=List[TaskModel])
@app.get("/tasks/", response_model=List[TaskModel])
async def read_tasks(db: db_dependency, user: CurrentUser, skip: int = 0, limit: int = 100):
    tasks = (
        db.query(models.Task)
        .filter(models.Task.user_id == user.user_id)
        .offset(skip)
        .limit(limit)
        .all()
    )
    return tasks


@app.post("/api/tasks/enrich", response_model=EnrichTaskResponse)
async def enrich_task(body: EnrichTaskRequest, request: Request, user: CurrentUser):
    _ = user
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/tasks/enrich"
    _enforce_rate_limit(client_key)

    system_prompt, user_prompt = _build_enrich_prompts(body.task, body.taskHistory)
    raw_payload, retries_used = await _openai_chat_completion(system_prompt, user_prompt)
    normalized = _normalize_enriched_task(raw_payload, body.task)
    return EnrichTaskResponse(task=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


@app.post("/api/suggestions", response_model=SuggestionResponse)
async def create_suggestion(body: SuggestionRequest, request: Request, user: CurrentUser):
    _ = user
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/suggestions"
    _enforce_rate_limit(client_key)

    system_prompt, user_prompt = _build_suggestion_prompts(body.taskHistory)
    raw_payload, retries_used = await _openai_chat_completion(
        system_prompt, user_prompt, temperature=OPENAI_SUGGESTION_TEMPERATURE
    )
    normalized = _normalize_suggestion(raw_payload)
    return SuggestionResponse(suggestion=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


# --- Users (public lookups / legacy signup) ---


@app.post("/users/", response_model=PersonModel)
async def create_user(body: CreateUserRequest, db: db_dependency):
    username = body.username
    existing = db.query(models.Person).filter(models.Person.user_name == username).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username already exists")
    person = models.Person(user_name=username, email="", password_hash=None)
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


@app.get("/users", response_model=List[PersonModel])
async def read_users(db: db_dependency, skip: int = 0, limit: int = 100):
    users = db.query(models.Person).offset(skip).limit(limit).all()
    return users


@app.get("/user_by_id/{user_id}", response_model=PersonModel)
async def user_by_id(user_id: str, db: db_dependency):
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid user id format") from exc
    user = db.query(models.Person).filter(models.Person.user_id == uid).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.get("/user_by_username/{username}", response_model=PersonModel)
async def user_by_user_name(username: str, db: db_dependency):
    key = username.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Username required")
    user = db.query(models.Person).filter(models.Person.user_name == key).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            "Missing dependency: uvicorn. Activate your project venv and run:\n"
            "  pip install -r requirements.txt\n"
            "Or run: ./scripts/dev.sh",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
