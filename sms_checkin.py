"""
Daily SMS check-in (Twilio): 5pm local send, inbound reply → Task rows.
See .env.example for TWILIO_* and SCHEDULER_SECRET.

US toll-free (and some numbers): Twilio requires Console verification for your **sending**
identity before outbound SMS succeeds — see
https://www.twilio.com/docs/messaging/compliance/toll-free/console-onboarding
That is not caused by OTP wording; unverified TF affects every Programmable SMS send the same way.

Phone OTP: if `TWILIO_VERIFY_SERVICE_SID` is unset, the 6-digit **expiry is enforced only in our
database** (`phone_otp_challenge.expires_at`). With Verify, Twilio validates the code; the placeholder
row in `phone_otp_challenge` is only for rate limits.

Optional: set TWILIO_MESSAGING_SERVICE_SID to send via a Messaging Service (often how verified toll-free is attached).

Failure modes: reply over SMS_CHECKIN_MAX_BODY_CHARS (rejected, user notified);
OpenAI missing/mis-parsed JSON → single fallback task from raw text.
Opt-out: STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT (first word, case-insensitive).
HELP/INFO: short compliance message (Twilio also sends carrier help on many numbers).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

import models
from database import DbSession, SessionLocal
from openai_client import OPENAI_API_KEY, openai_chat_completion

logger = logging.getLogger(__name__)


class SmsUpstreamError(Exception):
    """Twilio Programmable SMS rejected the request (e.g. unverified toll-free, invalid To)."""

    pass


sms_router = APIRouter(tags=["sms"])

TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_MESSAGING_SERVICE_SID = (os.getenv("TWILIO_MESSAGING_SERVICE_SID") or "").strip()
TWILIO_FROM_NUMBER = (
    (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_FROM_NUMBER") or "").strip()
)
SMS_CHECKIN_HOUR_LOCAL = int(os.getenv("SMS_CHECKIN_HOUR_LOCAL", "17"))
SMS_CHECKIN_END_MINUTE = int(os.getenv("SMS_CHECKIN_END_MINUTE", "20"))
MAX_SMS_BODY_CHARS = int(os.getenv("SMS_CHECKIN_MAX_BODY_CHARS", "900"))
SMS_SCHEDULER_INTERVAL_MINUTES = int(os.getenv("SMS_SCHEDULER_INTERVAL_MINUTES", "2"))

DAILY_PROMPT_TEXT = (
    "Nudge check-in: what did you get done today? Reply with a short note. "
    "Reply STOP to opt out, HELP for info."
)
HELP_REPLY_TEXT = (
    "Nudge: daily task check-ins via SMS. Visit the app to manage your account. "
    "Reply STOP to unsubscribe. Msg&data rates may apply."
)
CONFIRM_SAVED_TEXT = "Thanks — saved to your tasks in Nudge."
NO_SESSION_TEXT = "No active check-in right now. Open the Nudge app to log tasks."
TOO_LONG_TEXT = "That message is too long. Please send a shorter reply (one or two sentences)."
WELCOME_SMS_TEXT = (
    "Welcome to Nudge! You're signed up for SMS check-ins. "
    "We'll text you around 5pm in your timezone to ask what you got done. "
    "Reply STOP to opt out, HELP for info."
)
TEST_SMS_TEXT = (
    "Nudge test message: SMS is working. "
    "You'll get daily check-ins around 5pm in your timezone. "
    "Reply STOP to opt out, HELP for info."
)

_OPT_OUT_KEYWORDS = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
)
_HELP_KEYWORDS = frozenset({"HELP", "INFO"})

_scheduler = None


def _scheduler_secret() -> str:
    return (os.getenv("SCHEDULER_SECRET") or "").strip()


def twilio_configured() -> bool:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return False
    return bool(TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER)


def _public_request_url(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = (forwarded or request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = request.url.netloc
    path = request.url.path
    query = request.url.query
    if query:
        return f"{scheme}://{host}{path}?{query}"
    return f"{scheme}://{host}{path}"


def _form_to_params(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in form:
        raw = form.get(key)
        if raw is None:
            continue
        if hasattr(raw, "read"):
            continue
        out[str(key)] = str(raw)
    return out


def _twilio_signature_ok(url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature or not TWILIO_AUTH_TOKEN:
        return False
    return bool(RequestValidator(TWILIO_AUTH_TOKEN).validate(url, params, signature))


def send_twilio_sms(to_e164: str, body: str) -> str:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        raise RuntimeError("Twilio is not configured")
    if not (TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER):
        raise RuntimeError("Twilio is not configured (set TWILIO_MESSAGING_SERVICE_SID or TWILIO_PHONE_NUMBER)")
    dry = os.getenv("SMS_DRY_RUN", "").lower() in ("1", "true", "yes")
    if dry:
        logger.info("SMS_DRY_RUN skip send to=%s", to_e164)
        return "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    from twilio.rest import Client

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    kwargs: dict[str, str] = {"to": to_e164, "body": body}
    if TWILIO_MESSAGING_SERVICE_SID:
        kwargs["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        kwargs["from_"] = TWILIO_FROM_NUMBER
    try:
        msg = client.messages.create(**kwargs)
    except TwilioRestException as exc:
        logger.warning(
            "Twilio messages.create failed status=%s code=%s to=%s msg=%s",
            exc.status,
            exc.code,
            to_e164[:12],
            exc.msg,
        )
        suffix = f" (Twilio {exc.code})" if exc.code is not None else ""
        raise SmsUpstreamError(f"{exc.msg}{suffix}") from exc
    return str(msg.sid)


def send_welcome_sms_if_opted_in(
    phone_e164: Optional[str],
    sms_opt_in: bool,
    phone_verified_at: Optional[datetime],
) -> None:
    """Best-effort welcome SMS after phone OTP verification; failures are logged only (never raises)."""
    if not sms_opt_in:
        return
    phone = (phone_e164 or "").strip()
    if not phone or phone_verified_at is None:
        return
    if not twilio_configured():
        return
    try:
        send_twilio_sms(phone, WELCOME_SMS_TEXT)
    except Exception:
        logger.exception("Welcome SMS failed to=%s", phone[:8])


def send_account_test_sms(phone_e164: str) -> str:
    """Send a one-off test SMS to the user's number. Raises if Twilio fails or env incomplete."""
    phone = (phone_e164 or "").strip()
    if not phone:
        raise ValueError("Phone number is required")
    return send_twilio_sms(phone, TEST_SMS_TEXT)


def run_daily_sms_prompts(db: Session) -> None:
    if not twilio_configured():
        return

    users = (
        db.query(models.Person)
        .filter(
            models.Person.sms_opt_in.is_(True),
            models.Person.phone_e164.isnot(None),
            models.Person.phone_verified_at.isnot(None),
            models.Person.timezone.isnot(None),
        )
        .all()
    )

    for user in users:
        phone = (user.phone_e164 or "").strip()
        tzname = (user.timezone or "").strip()
        if not phone or not tzname:
            continue
        try:
            tz = ZoneInfo(tzname)
        except ZoneInfoNotFoundError:
            logger.warning("Invalid timezone for user %s: %s", user.user_id, tzname)
            continue

        local_now = datetime.now(tz)
        if local_now.hour != SMS_CHECKIN_HOUR_LOCAL:
            continue
        if local_now.minute >= SMS_CHECKIN_END_MINUTE:
            continue

        local_date = local_now.date().isoformat()
        already = (
            db.query(models.SmsDailyCheckin)
            .filter(
                models.SmsDailyCheckin.user_id == user.user_id,
                models.SmsDailyCheckin.local_date == local_date,
            )
            .first()
        )
        if already:
            continue

        try:
            sid = send_twilio_sms(phone, DAILY_PROMPT_TEXT)
        except Exception:
            logger.exception("Outbound daily SMS failed user_id=%s", user.user_id)
            continue

        row = models.SmsDailyCheckin(
            user_id=user.user_id,
            local_date=local_date,
            outbound_message_sid=sid,
            status="awaiting_reply",
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("Duplicate sms check-in row user=%s date=%s (race)", user.user_id, local_date)
        else:
            db.refresh(row)


def _first_token_upper(body: str) -> str:
    s = body.strip()
    if not s:
        return ""
    return s.split()[0].upper()


def _fallback_tasks(sms_text: str, day_of_week: str) -> list[dict[str, str]]:
    label = (sms_text or "").strip()[:200] or "Daily check-in"
    return [
        {
            "sentiment": "neutral",
            "category": "other",
            "label": label,
            "context": "Logged from daily SMS check-in.",
            "time_of_day": "evening",
            "amount_of_time": "unspecified",
            "day_of_week": day_of_week,
        }
    ]


def _normalize_task_dict(item: dict[str, Any], fallback_label: str) -> dict[str, str]:
    sentiment = str(item.get("sentiment", "neutral")).lower().strip()
    if sentiment not in {"positive", "neutral", "negative"}:
        sentiment = "neutral"
    label = str(item.get("label", "")).strip() or fallback_label[:200]
    return {
        "sentiment": sentiment,
        "category": (str(item.get("category", "other")).strip()[:80] or "other"),
        "label": label[:200],
        "context": str(item.get("context", "")).strip()[:300],
        "time_of_day": (str(item.get("time_of_day", item.get("timeOfDay", "unspecified"))).strip()[:40] or "unspecified"),
        "amount_of_time": (
            str(item.get("amount_of_time", item.get("amountOfTime", "unspecified"))).strip()[:40] or "unspecified"
        ),
        "day_of_week": (str(item.get("day_of_week", item.get("dayOfWeek", "unspecified"))).strip()[:40] or "unspecified"),
    }


def _tasks_from_llm(raw: dict[str, Any], sms_text: str, day_of_week: str) -> list[dict[str, str]]:
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        return _fallback_tasks(sms_text, day_of_week)
    out: list[dict[str, str]] = []
    for item in tasks[:5]:
        if isinstance(item, dict):
            out.append(_normalize_task_dict(item, sms_text))
    return out or _fallback_tasks(sms_text, day_of_week)


async def _extract_tasks_from_reply(sms_text: str, day_of_week: str) -> list[dict[str, str]]:
    if not OPENAI_API_KEY:
        return _fallback_tasks(sms_text, day_of_week)
    system_prompt = (
        "You turn a user's SMS check-in into structured tasks for a productivity app. "
        'Return strict JSON only: {"tasks":[{"sentiment":"positive|neutral|negative",'
        '"category":"string","label":"string","context":"string","time_of_day":"string",'
        '"amount_of_time":"string","day_of_week":"string"}]}. '
        "Use up to 5 tasks if they clearly did multiple things; otherwise one task. "
        "Use short strings; if unknown use 'unspecified'."
    )
    user_prompt = json.dumps({"sms_reply": sms_text[:800], "local_day_of_week": day_of_week})
    try:
        raw, _ = await openai_chat_completion(system_prompt, user_prompt, temperature=0.2)
    except HTTPException as exc:
        logger.warning("OpenAI SMS extraction failed: %s", exc.detail)
        return _fallback_tasks(sms_text, day_of_week)
    return _tasks_from_llm(raw, sms_text, day_of_week)


def _persist_tasks(db: Session, user_id: Any, task_dicts: list[dict[str, str]]) -> None:
    if not task_dicts:
        return
    from journal_service import insert_journal_with_tasks

    insert_journal_with_tasks(
        db,
        user_id=user_id,
        task_field_dicts=task_dicts,
        source="sms",
        note=None,
    )


def start_sms_scheduler() -> None:
    global _scheduler
    if os.getenv("NUDGE_TESTING", "").lower() in ("1", "true", "yes"):
        return
    if os.getenv("SMS_USE_APSCHEDULER", "true").lower() not in ("1", "true", "yes"):
        return
    if not twilio_configured():
        logger.info("SMS scheduler not started (Twilio env incomplete)")
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    def job() -> None:
        db = SessionLocal()
        try:
            run_daily_sms_prompts(db)
        finally:
            db.close()

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        job,
        "interval",
        minutes=max(1, SMS_SCHEDULER_INTERVAL_MINUTES),
        id="nudge_sms_daily_prompts",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("SMS daily prompt scheduler started (every %s min)", SMS_SCHEDULER_INTERVAL_MINUTES)


def stop_sms_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


@sms_router.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(request: Request, db: DbSession) -> Response:
    if not twilio_configured():
        raise HTTPException(status_code=503, detail="SMS webhook not configured")

    form = await request.form()
    params = _form_to_params(form)
    url = _public_request_url(request)
    sig = request.headers.get("X-Twilio-Signature")
    if not _twilio_signature_ok(url, params, sig):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    sid = (params.get("SmsSid") or params.get("MessageSid") or "").strip()
    if not sid:
        return Response(status_code=200)

    db.add(models.SmsInboundDedup(message_sid=sid))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return Response(status_code=200)

    from_num = (params.get("From") or "").strip()
    body_raw = params.get("Body") or ""

    user = (
        db.query(models.Person).filter(models.Person.phone_e164 == from_num).first()
        if from_num
        else None
    )

    token = _first_token_upper(body_raw)
    if token in _OPT_OUT_KEYWORDS:
        if user is not None:
            user.sms_opt_in = False
            db.add(user)
        db.commit()
        return Response(status_code=200)

    if token in _HELP_KEYWORDS:
        if user is not None and twilio_configured():
            try:
                send_twilio_sms(from_num, HELP_REPLY_TEXT)
            except Exception:
                logger.exception("HELP reply SMS failed")
        db.commit()
        return Response(status_code=200)

    if user is None:
        db.commit()
        return Response(status_code=200)

    if not user.sms_opt_in:
        db.commit()
        return Response(status_code=200)

    if user.phone_verified_at is None:
        try:
            send_twilio_sms(
                from_num,
                "Complete phone verification in the Nudge app before check-ins will work.",
            )
        except Exception:
            logger.exception("Unverified-user reply SMS failed")
        db.commit()
        return Response(status_code=200)

    if len(body_raw) > MAX_SMS_BODY_CHARS:
        try:
            send_twilio_sms(from_num, TOO_LONG_TEXT)
        except Exception:
            logger.exception("Too-long reply notice failed")
        db.commit()
        return Response(status_code=200)

    session_row = (
        db.query(models.SmsDailyCheckin)
        .filter(
            models.SmsDailyCheckin.user_id == user.user_id,
            models.SmsDailyCheckin.status == "awaiting_reply",
        )
        .order_by(models.SmsDailyCheckin.id.desc())
        .first()
    )

    if session_row is None:
        try:
            send_twilio_sms(from_num, NO_SESSION_TEXT)
        except Exception:
            logger.exception("No-session reply SMS failed")
        db.commit()
        return Response(status_code=200)

    tzname = (user.timezone or "").strip()
    try:
        tz = ZoneInfo(tzname) if tzname else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    dow = datetime.now(tz).strftime("%A")

    task_dicts = await _extract_tasks_from_reply(body_raw, dow)
    _persist_tasks(db, user.user_id, task_dicts)
    session_row.status = "completed"
    db.add(session_row)
    db.commit()

    try:
        send_twilio_sms(from_num, CONFIRM_SAVED_TEXT)
    except Exception:
        logger.exception("Confirmation SMS failed user=%s", user.user_id)

    return Response(status_code=200)


@sms_router.post("/internal/sms/run-daily-prompts")
async def internal_run_daily_prompts(request: Request, db: DbSession) -> dict[str, bool]:
    secret = _scheduler_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="SCHEDULER_SECRET is not configured")
    got = (request.headers.get("X-Scheduler-Secret") or "").strip()
    if got != secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    run_daily_sms_prompts(db)
    return {"ok": True}
