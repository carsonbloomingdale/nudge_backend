import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session, joinedload
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from pydantic import AliasChoices, BaseModel, Field, ConfigDict, computed_field, field_validator, model_validator
from uuid import UUID
from database import (
    DbSession,
    engine,
    ensure_auth_columns,
    ensure_journal_schema,
    ensure_journals_note_column,
    ensure_person_enrichment_summary_column,
    ensure_person_profile_columns,
)
from openai_client import OPENAI_MODEL, OPENAI_SUGGESTION_TEMPERATURE, openai_chat_completion
import models
from task_schemas import PersonalityTraitItem
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from threading import Lock

from auth_deps import CurrentUser
from auth_middleware import TaskAuthMiddleware
from auth_tokens import (
    attach_auth_cookies,
    auth_configured,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    COOKIE_REFRESH_NAME,
    decode_token,
    get_access_token_from_request,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from sms_checkin import start_sms_scheduler, stop_sms_scheduler

    start_sms_scheduler()
    yield
    stop_sms_scheduler()


app = FastAPI(title="Nudge API", lifespan=_lifespan)

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
    # Keep these optional in request payloads for backward compatibility with
    # older clients/tests that only send core task fields.
    time_of_day: str = "unspecified"
    amount_of_time: str = "unspecified"
    day_of_week: str = "unspecified"
    personality_traits: List[str] = Field(default_factory=list, max_length=5)

    @field_validator("personality_traits", mode="before")
    @classmethod
    def _cap_trait_strings(cls, v: object) -> object:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for item in v[:5]:
            s = str(item).strip()[:80]
            if s:
                out.append(s)
        return out


class TaskModel(TaskBase):
    task_id: int
    journal_id: Optional[int] = None
    personality_traits: List[PersonalityTraitItem] = Field(default_factory=list)

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


class SmsTestResponse(BaseModel):
    ok: bool = True


class PhoneOtpVerifyBody(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


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
    enrichment_summary: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def phone_verified(self) -> bool:
        return self.phone_verified_at is not None


class AuthSessionResponse(AuthMeResponse):
    """Same fields as GET /auth/me plus access_token and refresh_token for clients that use Authorization: Bearer (e.g. Safari / ITP where cookies are unreliable)."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[Literal["bearer"]] = None


class RefreshOkResponse(BaseModel):
    ok: bool = True
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[Literal["bearer"]] = None
    profile: Optional[AuthMeResponse] = None


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


class EnrichmentSummaryRefreshResponse(BaseModel):
    summary: str
    meta: OpenAIRequestMeta


class EnrichTaskRequest(BaseModel):
    task: str = Field(
        min_length=1,
        max_length=300,
        description="Short note to enrich. For long journal text use POST /api/tasks/split-from-journal first.",
    )
    taskHistory: List[dict[str, Any]] = Field(default_factory=list, max_length=50)


JOURNAL_SPLIT_MAX_CHARS = int(os.getenv("JOURNAL_SPLIT_MAX_CHARS", "12000"))
ENRICH_BATCH_MAX_TASKS = int(os.getenv("ENRICH_BATCH_MAX_TASKS", "15"))
ENRICH_SUMMARY_MAX_CHARS = int(os.getenv("ENRICH_SUMMARY_MAX_CHARS", "2000"))


class JournalSplitRequest(BaseModel):
    """Long journal / free-form entry; AI returns short lines suitable for enrich."""

    model_config = ConfigDict(populate_by_name=True)

    journal_text: str = Field(
        min_length=1,
        max_length=JOURNAL_SPLIT_MAX_CHARS,
        validation_alias=AliasChoices("journal_text", "journalText"),
    )
    taskHistory: List[dict[str, Any]] = Field(default_factory=list, max_length=50)


class SplitTaskItem(BaseModel):
    index: int
    text: str
    headline: str = ""


class JournalSplitResponse(BaseModel):
    items: List[SplitTaskItem]
    meta: OpenAIRequestMeta


class EnrichBatchRequest(BaseModel):
    """Short task strings only (e.g. output of split-from-journal); max 300 chars each."""

    tasks: List[str] = Field(min_length=1, max_length=ENRICH_BATCH_MAX_TASKS)
    taskHistory: List[dict[str, Any]] = Field(default_factory=list, max_length=50)

    @field_validator("tasks", mode="before")
    @classmethod
    def _validate_batch_tasks(cls, v: object) -> List[str]:
        if not isinstance(v, list) or not v:
            raise ValueError("tasks must be a non-empty list")
        out: List[str] = []
        for t in v[:ENRICH_BATCH_MAX_TASKS]:
            s = str(t).strip()
            if not s:
                raise ValueError("Each task must be non-empty")
            if len(s) > 300:
                raise ValueError("Each task must be at most 300 characters (use split-from-journal first)")
            out.append(s[:300])
        return out


class SuggestionRequest(BaseModel):
    """No body required. Server derives context from user tasks/traits."""


class EnrichedTask(BaseModel):
    sentiment: str
    category: str
    label: str
    context: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str
    personality_traits: List[str] = Field(default_factory=list)


class EnrichBatchResponse(BaseModel):
    tasks: List[EnrichedTask]
    meta: OpenAIRequestMeta


class SuggestionPayload(BaseModel):
    reccomendedTask: str
    context: str


class EnrichTaskResponse(BaseModel):
    task: EnrichedTask
    meta: OpenAIRequestMeta


class SuggestionResponse(BaseModel):
    suggestion: SuggestionPayload
    meta: OpenAIRequestMeta


db_dependency = DbSession

models.Base.metadata.create_all(bind=engine)
ensure_auth_columns(engine)
ensure_person_profile_columns(engine)
ensure_journal_schema(engine)
ensure_journals_note_column(engine)
ensure_person_enrichment_summary_column(engine)


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
        enrichment_summary=user.enrichment_summary,
    )


def _auth_session_response(person: models.Person, access: str, refresh: str) -> AuthSessionResponse:
    me = _auth_me_response(person)
    data = me.model_dump()
    return AuthSessionResponse(
        **data,
        access_token=access,
        refresh_token=refresh,
        token_type="bearer",
    )


RATE_LIMIT_REQUESTS = int(os.getenv("API_RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_limiter_lock = Lock()
_rate_limiter_store: defaultdict[str, deque[float]] = defaultdict(deque)


def _trim_task_history(
    task_history: List[dict[str, Any]],
    *,
    max_items: int = 5,
) -> List[dict[str, str]]:
    """Small cap so client-sent taskHistory cannot blow up OpenAI prompts."""
    trimmed: List[dict[str, str]] = []
    n = max(1, min(max_items, 20))
    for item in task_history[-n:]:
        if not isinstance(item, dict):
            continue
        trimmed.append(
            {
                "label": str(item.get("label", ""))[:100],
                "category": str(item.get("category", ""))[:50],
                "sentiment": str(item.get("sentiment", ""))[:24],
                "context": str(item.get("context", ""))[:140],
            }
        )
    return trimmed


def _llm_user_background(
    enrichment_summary: Optional[str],
    task_history: List[dict[str, Any]],
    pinned_traits: Optional[List[str]] = None,
) -> dict[str, Any]:
    """When a server-side summary exists, send it plus a tiny history tail; otherwise a slightly larger tail."""
    cleaned_pinned = [str(x).strip()[:80] for x in (pinned_traits or []) if str(x).strip()][:20]
    summary = (enrichment_summary or "").strip()
    if summary:
        hist = _trim_task_history(task_history, max_items=2)
        out: dict[str, Any] = {
            "user_profile_summary": summary[:ENRICH_SUMMARY_MAX_CHARS],
        }
        if cleaned_pinned:
            out["pinned_traits"] = cleaned_pinned
        if hist:
            out["recent_task_history"] = hist
        return out
    hist = _trim_task_history(task_history, max_items=5)
    out: dict[str, Any] = {}
    if cleaned_pinned:
        out["pinned_traits"] = cleaned_pinned
    if hist:
        out["recent_task_history"] = hist
    if out:
        return out
    return {}


def _enforce_rate_limit(client_key: str) -> None:
    now = time.time()
    with _rate_limiter_lock:
        bucket = _rate_limiter_store[client_key]
        while bucket and bucket[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_REQUESTS:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, try again soon.")
        bucket.append(now)


def _normalize_trait_label(raw: str) -> List[str]:
    """Split combined trait strings and remove redundant 'trait' suffix."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return []
    parts = re.split(r"\s*(?:,|/|&|\band\b|\bwith\b)\s*", text, flags=re.IGNORECASE)
    out: List[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = re.sub(r"\s+", " ", part).strip(" -_.,;:()[]{}")
        cleaned = re.sub(r"\btraits?\b$", "", cleaned, flags=re.IGNORECASE).strip(" -_.,;:()[]{}")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned[:80])
    return out


def _merge_required_pinned_traits(
    inferred_traits: List[str],
    pinned_traits: Optional[List[str]],
) -> List[str]:
    """Ensure every pinned trait appears in the output list at least once."""
    deduped: List[str] = []
    seen: set[str] = set()
    for trait in inferred_traits:
        key = trait.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trait)

    for pinned in pinned_traits or []:
        normalized_parts = _normalize_trait_label(str(pinned))
        if not normalized_parts:
            continue
        pinned_label = normalized_parts[0]
        key = pinned_label.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pinned_label)
    return deduped


def _normalize_enriched_task(
    raw_payload: dict[str, Any],
    original_task: str,
    pinned_traits: Optional[List[str]] = None,
) -> EnrichedTask:
    payload = raw_payload.get("task", raw_payload) if isinstance(raw_payload, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    personality_traits = payload.get("personality_traits", payload.get("personalityTraits", []))
    normalized_traits: List[str] = []
    if isinstance(personality_traits, list):
        for trait in personality_traits:
            if isinstance(trait, str):
                normalized_traits.extend(_normalize_trait_label(trait))
            elif isinstance(trait, dict) and trait.get("label"):
                normalized_traits.extend(_normalize_trait_label(str(trait.get("label"))))
    deduped_traits = _merge_required_pinned_traits(normalized_traits, pinned_traits)

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
        personality_traits=deduped_traits[:20],
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


def _build_enrich_prompts(
    task: str,
    task_history: List[dict[str, Any]],
    enrichment_summary: Optional[str] = None,
    pinned_traits: Optional[List[str]] = None,
) -> tuple[str, str]:
    system_prompt = (
        "You extract task metadata from short user notes. Return strict JSON only with this exact shape: "
        '{"task":{"sentiment":"positive|neutral|negative","category":"string","label":"string","context":"string",'
        '"time_of_day":"string","amount_of_time":"string","day_of_week":"string","personality_traits":["string"]}}. '
        "If user_profile_summary is present, use it for tone and recurring themes; recent_task_history is optional extra signal."
    )
    user_prompt = json.dumps(
        {
            "task": task,
            **_llm_user_background(enrichment_summary, task_history, pinned_traits),
            "rules": [
                "Use short concise text.",
                "If unknown, use 'unspecified'.",
                "personality_traits should have up to 5 items.",
                "Each personality_traits item must be a single trait only (no commas, '&', '/', or 'and').",
                "Do not include the words 'trait' or 'traits' in personality_traits values.",
                "If pinned_traits are present, personality_traits must include ALL pinned_traits values at least once.",
                "When pinned_traits are present, additional non-pinned traits may also be generated.",
            ],
        }
    )
    return system_prompt, user_prompt


def _build_journal_split_prompts(
    journal_text: str,
    task_history: List[dict[str, Any]],
    enrichment_summary: Optional[str] = None,
    pinned_traits: Optional[List[str]] = None,
) -> tuple[str, str]:
    system_prompt = (
        "You read a long journal or reflective entry. Extract discrete task-sized items the user could log "
        "in a productivity app (concrete actions, habits, or meaningful standalone beats). "
        "Return strict JSON only with this exact shape: "
        '{"items":[{"text":"string under 280 characters","headline":"optional very short title"}, ...]}. '
        "Rules: (1) Each text is self-contained and at most 280 characters. "
        "(2) Produce between 1 and 15 items depending on content; merge fluff; skip empty filler. "
        "(3) Preserve distinct emotionally or practically important beats as separate items when appropriate. "
        "(4) No markdown or unescaped newlines inside JSON string values. "
        "If user_profile_summary is present, align tone with it when helpful."
    )
    user_prompt = json.dumps(
        {
            "journal_text": journal_text,
            **_llm_user_background(enrichment_summary, task_history, pinned_traits),
        }
    )
    return system_prompt, user_prompt


def _normalize_journal_split(raw_payload: dict[str, Any]) -> List[SplitTaskItem]:
    if not isinstance(raw_payload, dict):
        return []
    items = raw_payload.get("items")
    if items is None:
        items = raw_payload.get("tasks")
    if not isinstance(items, list):
        return []
    out: List[SplitTaskItem] = []
    for it in items[:20]:
        headline = ""
        if isinstance(it, str):
            text = it.strip()
        elif isinstance(it, dict):
            text = str(it.get("text") or it.get("label") or "").strip()
            headline = str(it.get("headline") or it.get("title") or "").strip()[:120]
        else:
            continue
        if not text:
            continue
        text = text[:300]
        out.append(SplitTaskItem(index=len(out), text=text, headline=headline))
    return out


def _build_batch_enrich_prompts(
    tasks: List[str],
    task_history: List[dict[str, Any]],
    enrichment_summary: Optional[str] = None,
    pinned_traits: Optional[List[str]] = None,
) -> tuple[str, str]:
    system_prompt = (
        "You enrich multiple short user notes for a productivity app. "
        "Return strict JSON only with this exact shape: "
        '{"tasks":['
        '{"sentiment":"positive|neutral|negative","category":"string","label":"string","context":"string",'
        '"time_of_day":"string","amount_of_time":"string","day_of_week":"string","personality_traits":["string"]}'
        "]}. "
        "The tasks array must have the SAME LENGTH and SAME ORDER as the input tasks array. "
        "Each object corresponds to the input string at the same index. "
        "If user_profile_summary is present, use it for consistent tone and trait patterns; "
        "recent_task_history is optional extra signal."
    )
    user_prompt = json.dumps(
        {
            "tasks": tasks,
            **_llm_user_background(enrichment_summary, task_history, pinned_traits),
            "rules": [
                "Use short concise text per field.",
                "If unknown, use 'unspecified'.",
                "Up to 5 personality_traits per item.",
                "Each personality_traits item must be a single trait only (no commas, '&', '/', or 'and').",
                "Do not include the words 'trait' or 'traits' in personality_traits values.",
                "If pinned_traits are present, each task's personality_traits must include ALL pinned_traits values at least once.",
                "When pinned_traits are present, additional non-pinned traits may also be generated.",
            ],
        }
    )
    return system_prompt, user_prompt


def _normalize_batch_enrich(
    raw_payload: dict[str, Any],
    originals: List[str],
    pinned_traits: Optional[List[str]] = None,
) -> List[EnrichedTask]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    arr = raw_payload.get("tasks")
    if not isinstance(arr, list):
        arr = []
    out: List[EnrichedTask] = []
    for i, orig in enumerate(originals):
        sub: dict[str, Any] = {}
        if i < len(arr) and isinstance(arr[i], dict):
            sub = {"task": arr[i]}
        out.append(_normalize_enriched_task(sub, orig, pinned_traits))
    return out


def _build_suggestion_prompts(
    task_history: List[dict[str, Any]],
    enrichment_summary: Optional[str] = None,
    pinned_traits: Optional[List[str]] = None,
    smart_signals: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    bg = _llm_user_background(enrichment_summary, task_history, pinned_traits)
    trimmed = bg.get("recent_task_history") or []
    has_signal = bool(bg.get("user_profile_summary")) or any(
        (row.get("label") or "").strip() or (row.get("context") or "").strip() for row in trimmed
    )
    system_prompt = (
        "You recommend one concrete next task for a productivity app. "
        "Ground the suggestion in user_profile_summary and/or recent_task_history when present. "
        "Return strict JSON only with this exact shape: "
        '{"suggestion":{"reccomendedTask":"string","context":"string"}}.'
    )
    user_prompt = json.dumps(
        {
            **bg,
            "smart_signals": smart_signals or {},
            "history_has_specific_tasks": has_signal,
            "rules": [
                "reccomendedTask: one short imperative line (about 12 words or fewer).",
                "context: one sentence that ties the suggestion to user_profile_summary and/or recent_task_history.",
                "Prefer a logical follow-up, unblocker, or natural break given their categories and sentiments.",
                "Do not assume facts not supported by user_profile_summary or recent_task_history "
                "(pets, children, commute, health) unless labels or context clearly mention them.",
                "Avoid clichéd generic wellness filler (e.g. walk your dog, drink more water) unless history relates.",
                "Do not merely repeat the latest task label; choose a distinct next step unless repeating is clearly useful.",
                "If history_has_specific_tasks is false, suggest a neutral productive micro-task that needs no private details.",
                "Avoid unsafe or medical/financial advice.",
                "If pinned_traits are present, prefer suggestions that reinforce those focus traits when appropriate.",
                "If smart_signals.low_traits includes candidates, prioritize a suggestion that exercises one of those lower-recent traits.",
                "When smart_signals.actions_by_trait exists for a low trait, reuse those action patterns in the suggestion.",
            ],
        }
    )
    return system_prompt, user_prompt


def _get_pinned_traits_for_user(db: Session, user_id: UUID) -> List[str]:
    from personality_analytics import pinned_trait_labels_for_user

    return pinned_trait_labels_for_user(db, user_id, limit=20)


def _server_task_history_for_suggestions(db: Session, user_id: UUID, *, limit: int = 18) -> List[dict[str, Any]]:
    rows = (
        db.query(models.Task)
        .filter(models.Task.user_id == user_id)
        .order_by(models.Task.task_id.desc())
        .limit(max(1, min(limit, 50)))
        .all()
    )
    out: List[dict[str, Any]] = []
    for t in rows:
        out.append(
            {
                "label": str(t.label or "")[:120],
                "category": str(t.category or "")[:60],
                "sentiment": str(t.sentiment or "")[:24],
                "context": str(t.context or "")[:180],
            }
        )
    return out


def _build_suggestion_smart_signals(db: Session, user_id: UUID) -> dict[str, Any]:
    """Detect lower-recent traits and map them to historically related actions."""
    rows = (
        db.query(models.Task)
        .options(joinedload(models.Task.personality_traits))
        .filter(models.Task.user_id == user_id)
        .order_by(models.Task.task_id.desc())
        .limit(80)
        .all()
    )
    if not rows:
        return {}

    recent_tasks = rows[:16]
    baseline_tasks = rows[16:64]
    if not baseline_tasks:
        return {}

    def _task_traits(task: models.Task) -> List[str]:
        labels: List[str] = []
        for tr in task.personality_traits or []:
            for item in _normalize_trait_label(str(tr.label or "")):
                if item:
                    labels.append(item)
        dedup: List[str] = []
        seen: set[str] = set()
        for label in labels:
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            dedup.append(label)
        return dedup

    recent_count: dict[str, int] = {}
    baseline_count: dict[str, int] = {}
    actions_by_trait: dict[str, List[dict[str, str]]] = {}
    for task in baseline_tasks:
        traits = _task_traits(task)
        action = {
            "label": str(task.label or "")[:120],
            "category": str(task.category or "")[:60],
            "context": str(task.context or "")[:140],
        }
        for trait in traits:
            baseline_count[trait] = baseline_count.get(trait, 0) + 1
            actions_by_trait.setdefault(trait, [])
            if action not in actions_by_trait[trait]:
                actions_by_trait[trait].append(action)
    for task in recent_tasks:
        for trait in _task_traits(task):
            recent_count[trait] = recent_count.get(trait, 0) + 1

    rn = max(len(recent_tasks), 1)
    bn = max(len(baseline_tasks), 1)
    low_traits: List[dict[str, Any]] = []
    for trait, base_c in baseline_count.items():
        if base_c < 2:
            continue
        rec_c = recent_count.get(trait, 0)
        recent_ratio = (rec_c + 0.25) / (rn + 1.0)
        baseline_ratio = (base_c + 0.25) / (bn + 1.0)
        delta = recent_ratio - baseline_ratio
        if delta < -0.02:
            low_traits.append(
                {
                    "trait": trait,
                    "recent_count": rec_c,
                    "baseline_count": base_c,
                    "delta_ratio": round(delta, 4),
                }
            )
    low_traits.sort(key=lambda x: x["delta_ratio"])
    if not low_traits:
        return {}

    chosen = low_traits[:3]
    actions: dict[str, List[dict[str, str]]] = {}
    for item in chosen:
        trait = item["trait"]
        actions[trait] = actions_by_trait.get(trait, [])[:3]
    return {"low_traits": chosen, "actions_by_trait": actions}


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


@app.post("/auth/me/enrichment-summary/refresh", response_model=EnrichmentSummaryRefreshResponse)
async def refresh_enrichment_summary_endpoint(
    db: db_dependency,
    user: CurrentUser,
    request: Request,
):
    """One OpenAI call to build a short cached profile for enrich/split/suggestions (reduces huge taskHistory in prompts)."""
    from personality_analytics import trait_snapshot_for_user

    client_key = f"{request.client.host if request.client else 'unknown'}:/auth/me/enrichment-summary/refresh"
    _enforce_rate_limit(client_key)

    recent = (
        db.query(models.Task)
        .filter(models.Task.user_id == user.user_id)
        .order_by(models.Task.task_id.desc())
        .limit(15)
        .all()
    )
    task_lines: List[dict[str, str]] = []
    for t in recent:
        task_lines.append(
            {
                "label": str(t.label or "")[:200],
                "category": str(t.category or "")[:80],
                "sentiment": str(t.sentiment or "")[:40],
                "context": str(t.context or "")[:200],
            }
        )
    traits = trait_snapshot_for_user(db, user.user_id, limit=15)
    pinned_traits = _get_pinned_traits_for_user(db, user.user_id)
    name_hint = " ".join(
        s for s in [(user.first_name or "").strip(), (user.last_name or "").strip()] if s
    )
    payload: dict[str, Any] = {
        "recent_tasks_sample": task_lines,
        "trait_and_category_counts": traits,
        "pinned_traits": pinned_traits,
    }
    if name_hint:
        payload["profile"] = {"display_name_hint": name_hint[:120]}

    system_prompt = (
        "You write a compact user profile for a productivity app's AI enrichment. "
        'Return strict JSON only: {"summary":"string"}. '
        f"The summary must be at most {ENRICH_SUMMARY_MAX_CHARS} characters. "
        "Describe recurring themes, work style, emotional tone, and personality-relevant patterns "
        "that would help label tasks consistently. "
        "Do not invent private facts; only generalize from the data given. Plain text, no markdown."
    )
    user_prompt = json.dumps(payload)
    raw_payload, retries_used = await openai_chat_completion(system_prompt, user_prompt, temperature=0.25)
    summary = str(raw_payload.get("summary", "")).strip()[:ENRICH_SUMMARY_MAX_CHARS]
    if not summary:
        raise HTTPException(
            status_code=422,
            detail="Could not build enrichment summary from your data. Add a few tasks and try again.",
        )
    user.enrichment_summary = summary
    db.add(user)
    db.commit()
    return EnrichmentSummaryRefreshResponse(
        summary=summary,
        meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used),
    )


@app.post("/auth/me/phone/send-verification-code", response_model=AuthMeResponse)
async def auth_me_phone_send_verification_code(user: CurrentUser, db: db_dependency):
    """Send or start SMS verification for profile phone_e164.

    With `TWILIO_VERIFY_SERVICE_SID`, uses Twilio Verify (recommended). Otherwise sends a custom
    Programmable SMS body (may require toll-free verification for that sender).
    """
    from phone_sms_verify import send_phone_verification
    from sms_checkin import SmsUpstreamError

    try:
        send_phone_verification(db, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="SMS is not configured on this server.",
        )
    except SmsUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        logger.exception("Phone OTP send failed user_id=%s", user.user_id)
        raise HTTPException(
            status_code=502,
            detail="Could not send verification text. Try again later.",
        )
    db.refresh(user)
    return _auth_me_response(user)


@app.post("/auth/me/phone/verify", response_model=AuthMeResponse)
async def auth_me_phone_verify(user: CurrentUser, db: db_dependency, body: PhoneOtpVerifyBody):
    """Submit the 6-digit code from SMS; sets phone_verified_at and may send welcome SMS if sms_opt_in."""
    if user.phone_verified_at is not None:
        raise HTTPException(status_code=400, detail="Phone number is already verified")
    from phone_sms_verify import verify_phone_code
    from sms_checkin import send_welcome_sms_if_opted_in

    ok = verify_phone_code(db, user, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    db.refresh(user)
    await asyncio.to_thread(
        send_welcome_sms_if_opted_in,
        user.phone_e164,
        bool(user.sms_opt_in),
        user.phone_verified_at,
    )
    return _auth_me_response(user)


@app.post("/auth/me/sms/test", response_model=SmsTestResponse)
async def auth_me_sms_test(user: CurrentUser):
    """Send a test SMS to the signed-in user's phone (profile must have SMS enabled + E.164 phone)."""
    if not bool(user.sms_opt_in):
        raise HTTPException(
            status_code=400,
            detail="Turn on SMS notifications in your profile to send a test message.",
        )
    if user.phone_verified_at is None:
        raise HTTPException(
            status_code=400,
            detail="Verify your phone number first (POST /auth/me/phone/send-verification-code, then /verify).",
        )
    phone = (user.phone_e164 or "").strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Add a phone number (E.164) to your profile first.",
        )
    from sms_checkin import SmsUpstreamError, send_account_test_sms

    try:
        await asyncio.to_thread(send_account_test_sms, phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="SMS is not configured on this server.",
        )
    except SmsUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        logger.exception("Test SMS failed user_id=%s", user.user_id)
        raise HTTPException(
            status_code=502,
            detail="Could not send test SMS. Try again later.",
        )
    return SmsTestResponse()


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
    return RefreshOkResponse(
        ok=True,
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
        profile=_auth_me_response(user),
    )


# --- Tasks (protected by TaskAuthMiddleware + Depends(get_current_user)) ---


@app.post("/tasks/", response_model=TaskModel)
async def create_task(body: TaskCreateBody, db: db_dependency, user: CurrentUser):
    from journal_service import insert_journal_with_tasks, replace_personality_traits_for_task

    data = body.model_dump()
    trait_labels = data.pop("personality_traits", []) or []
    journal = insert_journal_with_tasks(
        db,
        user_id=user.user_id,
        task_field_dicts=[data],
        source="app",
        note=None,
    )
    db.flush()
    task_row = (
        db.query(models.Task)
        .filter(models.Task.journal_id == journal.journal_id)
        .order_by(models.Task.task_id.asc())
        .first()
    )
    if task_row is None:
        raise HTTPException(status_code=500, detail="Task row missing after journal create")
    replace_personality_traits_for_task(db, task_row.task_id, trait_labels)
    db.commit()
    from personality_analytics import invalidate_personality_chart_cache

    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    task_row = (
        db.query(models.Task)
        .options(joinedload(models.Task.personality_traits))
        .filter(models.Task.task_id == task_row.task_id)
        .first()
    )
    if task_row is None:
        raise HTTPException(status_code=500, detail="Task row missing after journal create")
    return TaskModel.model_validate(task_row)


@app.get("/tasks", response_model=List[TaskModel])
@app.get("/tasks/", response_model=List[TaskModel])
async def read_tasks(db: db_dependency, user: CurrentUser, skip: int = 0, limit: int = 100):
    tasks = (
        db.query(models.Task)
        .options(joinedload(models.Task.personality_traits))
        .filter(models.Task.user_id == user.user_id)
        .offset(skip)
        .limit(limit)
        .all()
    )
    return tasks


@app.delete("/tasks/{task_id}", status_code=204)
@app.delete("/tasks/{task_id}/", status_code=204)
async def delete_task(task_id: int, db: db_dependency, user: CurrentUser):
    row = (
        db.query(models.Task)
        .filter(models.Task.task_id == task_id, models.Task.user_id == user.user_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    db.query(models.PersonalityTrait).filter(models.PersonalityTrait.task_id == task_id).delete()
    db.delete(row)
    db.commit()
    from personality_analytics import invalidate_personality_chart_cache

    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    return Response(status_code=204)


@app.post("/api/tasks/enrich", response_model=EnrichTaskResponse)
async def enrich_task(body: EnrichTaskRequest, request: Request, user: CurrentUser, db: db_dependency):
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/tasks/enrich"
    _enforce_rate_limit(client_key)

    pinned_traits = _get_pinned_traits_for_user(db, user.user_id)
    system_prompt, user_prompt = _build_enrich_prompts(
        body.task,
        body.taskHistory,
        user.enrichment_summary,
        pinned_traits,
    )
    raw_payload, retries_used = await openai_chat_completion(system_prompt, user_prompt)
    normalized = _normalize_enriched_task(raw_payload, body.task, pinned_traits)
    return EnrichTaskResponse(task=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


@app.post("/api/tasks/split-from-journal", response_model=JournalSplitResponse)
async def split_tasks_from_journal(body: JournalSplitRequest, request: Request, user: CurrentUser, db: db_dependency):
    """Step 1: long journal text → short task lines (then call /api/tasks/enrich or /api/tasks/enrich-batch)."""
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/tasks/split-from-journal"
    _enforce_rate_limit(client_key)

    pinned_traits = _get_pinned_traits_for_user(db, user.user_id)
    system_prompt, user_prompt = _build_journal_split_prompts(
        body.journal_text, body.taskHistory, user.enrichment_summary, pinned_traits
    )
    raw_payload, retries_used = await openai_chat_completion(system_prompt, user_prompt, temperature=0.3)
    items = _normalize_journal_split(raw_payload)
    if not items:
        raise HTTPException(
            status_code=422,
            detail="Could not extract tasks from journal. Try again or shorten the entry.",
        )
    return JournalSplitResponse(items=items, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


@app.post("/api/tasks/enrich-batch", response_model=EnrichBatchResponse)
async def enrich_tasks_batch(body: EnrichBatchRequest, request: Request, user: CurrentUser, db: db_dependency):
    """Step 2: enrich many short strings in one model call (output of split-from-journal)."""
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/tasks/enrich-batch"
    _enforce_rate_limit(client_key)

    pinned_traits = _get_pinned_traits_for_user(db, user.user_id)
    system_prompt, user_prompt = _build_batch_enrich_prompts(
        body.tasks, body.taskHistory, user.enrichment_summary, pinned_traits
    )
    raw_payload, retries_used = await openai_chat_completion(system_prompt, user_prompt, temperature=0.2)
    normalized = _normalize_batch_enrich(raw_payload, body.tasks, pinned_traits)
    return EnrichBatchResponse(tasks=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


@app.post("/api/suggestions", response_model=SuggestionResponse)
async def create_suggestion(
    request: Request,
    user: CurrentUser,
    db: db_dependency,
    body: Optional[SuggestionRequest] = None,
):
    _ = body  # Deprecated input; suggestions now use backend history/signals.
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/suggestions"
    _enforce_rate_limit(client_key)

    pinned_traits = _get_pinned_traits_for_user(db, user.user_id)
    server_history = _server_task_history_for_suggestions(db, user.user_id, limit=18)
    smart_signals = _build_suggestion_smart_signals(db, user.user_id)
    system_prompt, user_prompt = _build_suggestion_prompts(
        server_history,
        user.enrichment_summary,
        pinned_traits,
        smart_signals,
    )
    raw_payload, retries_used = await openai_chat_completion(
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


from journal_api import router as journal_router
from personality_analytics import router as analytics_router
from sms_checkin import sms_router

app.include_router(journal_router)
app.include_router(analytics_router)
app.include_router(sms_router)


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
