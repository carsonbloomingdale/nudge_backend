"""Shared OpenAI chat completion used by main API routes and SMS check-in."""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, Optional
from urllib import error, request

from fastapi import HTTPException


def extract_json_object(raw_text: str) -> dict[str, Any]:
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


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_SUGGESTION_TEMPERATURE = float(os.getenv("OPENAI_SUGGESTION_TEMPERATURE", "0.85"))
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "2"))


async def openai_chat_completion(
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
            return extract_json_object(str(content)), attempt
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
