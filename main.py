from typing import List, Annotated, Any, Optional

from sqlalchemy.orm import Session
from fastapi import FastAPI, HTTPException, Depends, Request
from pydantic import BaseModel, Field, ConfigDict, field_validator
from uuid import UUID, uuid4
from database import SessionLocal, engine
import models
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import os
import sys
import random
import time
from collections import defaultdict, deque
from threading import Lock
from urllib import error, request


def _parse_cors_origins() -> List[str]:
    """Comma-separated URLs, or * for all. Example: http://localhost:3000,https://app.example.com"""
    raw = (os.getenv("CORS_ORIGINS") or "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app = FastAPI()

_cors_origins = _parse_cors_origins()
_allow_credentials = False if _cors_origins == ["*"] else True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """Body for POST /users/ — register by username only."""

    username: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("username cannot be blank")
        return s


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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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
        time_of_day=str(payload.get("time_of_day", payload.get("timeOfDay", "unspecified"))).strip()[:40] or "unspecified",
        amount_of_time=str(payload.get("amount_of_time", payload.get("amountOfTime", "unspecified"))).strip()[:40] or "unspecified",
        day_of_week=str(payload.get("day_of_week", payload.get("dayOfWeek", "unspecified"))).strip()[:40] or "unspecified",
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


async def _openai_chat_completion(system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], int]:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI service is not configured.")

    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
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

            # Rate / quota limits — retry with backoff, then surface 429 to the client
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
            await asyncio.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.2))

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
    system_prompt = (
        "You recommend one practical next task. Return strict JSON only with this exact shape: "
        '{"suggestion":{"reccomendedTask":"string","context":"string"}}.'
    )
    user_prompt = json.dumps(
        {
            "recent_task_history": _trim_task_history(task_history),
            "rules": [
                "Recommend one specific task.",
                "Context should explain why this task is recommended.",
                "Avoid unsafe or medical/financial advice.",
            ],
        }
    )
    return system_prompt, user_prompt

@app.post("/tasks/", response_model=TaskModel)
async def create_task(task: TaskBase, db: db_dependency):
    db_transaction = models.Task(**task.dict())
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction


@app.post("/users/", response_model=PersonModel)
async def create_user(body: CreateUserRequest, db: db_dependency):
    username = body.username
    existing = db.query(models.Person).filter(models.Person.user_name == username).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username already exists")
    # email unused by current clients; column exists on model — store empty string
    person = models.Person(user_name=username, email="")
    db.add(person)
    db.commit()
    db.refresh(person)
    return person

@app.get("/users", response_model=List[PersonModel])
async def read_users(db: db_dependency, skip: int = 0, limit: int = 100):
    users = db.query(models.Person).offset(skip).limit(limit).all()
    return users


@app.get("/tasks", response_model=List[TaskModel])
async def read_tasks(db: db_dependency, skip: int = 0, limit: int = 100):
    tasks = db.query(models.Task).offset(skip).limit(limit).all()
    return tasks


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


@app.post("/api/tasks/enrich", response_model=EnrichTaskResponse)
async def enrich_task(body: EnrichTaskRequest, request: Request):
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/tasks/enrich"
    _enforce_rate_limit(client_key)

    system_prompt, user_prompt = _build_enrich_prompts(body.task, body.taskHistory)
    raw_payload, retries_used = await _openai_chat_completion(system_prompt, user_prompt)
    normalized = _normalize_enriched_task(raw_payload, body.task)
    return EnrichTaskResponse(task=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


@app.post("/api/suggestions", response_model=SuggestionResponse)
async def create_suggestion(body: SuggestionRequest, request: Request):
    client_key = f"{request.client.host if request.client else 'unknown'}:/api/suggestions"
    _enforce_rate_limit(client_key)

    system_prompt, user_prompt = _build_suggestion_prompts(body.taskHistory)
    raw_payload, retries_used = await _openai_chat_completion(system_prompt, user_prompt)
    normalized = _normalize_suggestion(raw_payload)
    return SuggestionResponse(suggestion=normalized, meta=OpenAIRequestMeta(model=OPENAI_MODEL, retries_used=retries_used))


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