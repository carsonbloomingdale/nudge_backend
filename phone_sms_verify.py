"""
SMS verification for phone_e164 before welcome, daily prompts, or test SMS.

**Preferred:** set `TWILIO_VERIFY_SERVICE_SID` so codes are sent via Twilio Verify (SMS channel).
That uses Twilio’s verification product instead of custom Programmable SMS bodies, which aligns
with how toll-free and verification traffic is handled.

**Fallback (no Verify SID):** app-generated 6-digit OTP + Programmable SMS; expiry is only in
`phone_otp_challenge.expires_at`. Twilio does not validate that code.

You may still need Twilio Console approval for toll-free **Programmable** sends (welcome, daily,
test) via `TWILIO_MESSAGING_SERVICE_SID` / `TWILIO_PHONE_NUMBER`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

import models
import sms_checkin

logger = logging.getLogger(__name__)

# Stored in `phone_otp_challenge.code_hash` when the code is managed by Twilio Verify (not a local HMAC).
_TWILIO_VERIFY_ROW_HASH = "tv:" + "0" * 61  # len 64, cannot match SHA-256 hex of a 6-digit code

SMS_OTP_EXPIRE_MINUTES = int(os.getenv("SMS_OTP_EXPIRE_MINUTES", "10"))
SMS_OTP_MAX_SENDS_PER_HOUR = int(os.getenv("SMS_OTP_MAX_SENDS_PER_HOUR", "5"))
SMS_OTP_COOLDOWN_SECONDS = int(os.getenv("SMS_OTP_COOLDOWN_SECONDS", "45"))


def twilio_verify_configured() -> bool:
    sid = (os.getenv("TWILIO_VERIFY_SERVICE_SID") or "").strip()
    return bool(sid and sms_checkin.twilio_configured())


def _twilio_verify_service_sid() -> str:
    return (os.getenv("TWILIO_VERIFY_SERVICE_SID") or "").strip()


_OTP_BODY = (
    "Nudge: your verification code is {code}. It expires in {minutes} minutes. "
    "If you did not request this, ignore this message."
)


def _otp_pepper() -> bytes:
    key = (os.getenv("JWT_SECRET_KEY") or "").strip()
    if not key or len(key) < 32:
        raise RuntimeError("JWT_SECRET_KEY (min 32 chars) is required for phone OTP hashing")
    return key.encode("utf-8")


def hash_otp_code(code: str) -> str:
    c = (code or "").strip()
    return hmac.new(_otp_pepper(), c.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _purge_expired_otp_challenges(db: Session, user_id, now: datetime) -> None:
    """Remove expired rows so rate limits / cooldown reflect whether the user can get a fresh code."""
    for row in (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user_id)
        .all()
    ):
        if row.expires_at is None:
            continue
        if _ensure_utc(row.expires_at) < now:
            db.delete(row)


def send_phone_verification_otp(db: Session, user: models.Person) -> None:
    """
    Create a new OTP challenge and send SMS. Raises RuntimeError if Twilio not configured;
    ValueError for validation / rate limits.
    """
    if not sms_checkin.twilio_configured():
        raise RuntimeError("SMS is not configured")
    phone = (user.phone_e164 or "").strip()
    if not phone:
        raise ValueError("Add a phone number to your profile first")
    if user.phone_verified_at is not None:
        raise ValueError("This phone number is already verified")

    now = datetime.now(timezone.utc)
    _purge_expired_otp_challenges(db, user.user_id, now)
    db.flush()

    hour_ago = now - timedelta(hours=1)
    recent = 0
    for row in (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user.user_id)
        .all()
    ):
        if row.created_at is None:
            continue
        if _ensure_utc(row.created_at) >= hour_ago:
            recent += 1
    if recent >= SMS_OTP_MAX_SENDS_PER_HOUR:
        raise ValueError(
            f"Too many verification codes sent in the last hour (limit {SMS_OTP_MAX_SENDS_PER_HOUR}). "
            "Wait up to an hour after your oldest recent request, or contact support."
        )

    last = (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user.user_id)
        .order_by(models.PhoneOtpChallenge.created_at.desc())
        .first()
    )
    if last is not None and last.created_at is not None:
        last_ts = _ensure_utc(last.created_at)
        if (now - last_ts).total_seconds() < SMS_OTP_COOLDOWN_SECONDS:
            raise ValueError("Please wait a moment before requesting another code")

    code = generate_otp_code()
    expires = now + timedelta(minutes=SMS_OTP_EXPIRE_MINUTES)
    db.query(models.PhoneOtpChallenge).filter(models.PhoneOtpChallenge.user_id == user.user_id).delete()
    db.add(
        models.PhoneOtpChallenge(
            user_id=user.user_id,
            code_hash=hash_otp_code(code),
            expires_at=expires,
        )
    )
    body = _OTP_BODY.format(code=code, minutes=SMS_OTP_EXPIRE_MINUTES)
    try:
        db.flush()
        sms_checkin.send_twilio_sms(phone, body)
        db.commit()
    except sms_checkin.SmsUpstreamError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def verify_phone_otp(db: Session, user: models.Person, code: str) -> bool:
    """If code matches an active challenge, set phone_verified_at and clear challenges. Returns True on success."""
    raw = (code or "").strip()
    if not re.fullmatch(r"\d{6}", raw):
        return False

    now = datetime.now(timezone.utc)
    row = (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user.user_id)
        .order_by(models.PhoneOtpChallenge.created_at.desc())
        .first()
    )
    if row is None:
        return False
    if row.expires_at is None or _ensure_utc(row.expires_at) <= now:
        return False
    if not hmac.compare_digest(row.code_hash, hash_otp_code(raw)):
        return False

    db.query(models.PhoneOtpChallenge).filter(models.PhoneOtpChallenge.user_id == user.user_id).delete()
    user.phone_verified_at = now
    db.add(user)
    db.commit()
    db.refresh(user)
    return True


def _twilio_verify_client() -> Client:
    return Client(sms_checkin.TWILIO_ACCOUNT_SID, sms_checkin.TWILIO_AUTH_TOKEN)


def send_phone_verification_twilio_verify(db: Session, user: models.Person) -> None:
    """
    Start SMS verification via Twilio Verify (not Programmable SMS with a custom body).
    Records a placeholder row in phone_otp_challenge for rate limits only.
    """
    if not twilio_verify_configured():
        raise RuntimeError("Twilio Verify is not configured (set TWILIO_VERIFY_SERVICE_SID and Twilio credentials)")
    phone = (user.phone_e164 or "").strip()
    if not phone:
        raise ValueError("Add a phone number to your profile first")
    if user.phone_verified_at is not None:
        raise ValueError("This phone number is already verified")

    sid = _twilio_verify_service_sid()
    now = datetime.now(timezone.utc)
    _purge_expired_otp_challenges(db, user.user_id, now)
    db.flush()

    hour_ago = now - timedelta(hours=1)
    recent = 0
    for row in (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user.user_id)
        .all()
    ):
        if row.created_at is None:
            continue
        if _ensure_utc(row.created_at) >= hour_ago:
            recent += 1
    if recent >= SMS_OTP_MAX_SENDS_PER_HOUR:
        raise ValueError(
            f"Too many verification codes sent in the last hour (limit {SMS_OTP_MAX_SENDS_PER_HOUR}). "
            "Wait up to an hour after your oldest recent request, or contact support."
        )

    last = (
        db.query(models.PhoneOtpChallenge)
        .filter(models.PhoneOtpChallenge.user_id == user.user_id)
        .order_by(models.PhoneOtpChallenge.created_at.desc())
        .first()
    )
    if last is not None and last.created_at is not None:
        last_ts = _ensure_utc(last.created_at)
        if (now - last_ts).total_seconds() < SMS_OTP_COOLDOWN_SECONDS:
            raise ValueError("Please wait a moment before requesting another code")

    expires = now + timedelta(minutes=SMS_OTP_EXPIRE_MINUTES)
    db.query(models.PhoneOtpChallenge).filter(models.PhoneOtpChallenge.user_id == user.user_id).delete()
    db.add(
        models.PhoneOtpChallenge(
            user_id=user.user_id,
            code_hash=_TWILIO_VERIFY_ROW_HASH,
            expires_at=expires,
        )
    )
    try:
        db.flush()
        client = _twilio_verify_client()
        client.verify.v2.services(sid).verifications.create(to=phone, channel="sms")
        db.commit()
    except TwilioRestException as exc:
        db.rollback()
        raise sms_checkin.SmsUpstreamError(getattr(exc, "msg", None) or str(exc)) from exc
    except Exception:
        db.rollback()
        raise


def verify_phone_twilio_verify(db: Session, user: models.Person, code: str) -> bool:
    """Confirm the code with Twilio Verify; on success sets phone_verified_at."""
    raw = (code or "").strip()
    if not re.fullmatch(r"\d{6}", raw):
        return False
    if not twilio_verify_configured():
        return False
    phone = (user.phone_e164 or "").strip()
    if not phone:
        return False

    sid = _twilio_verify_service_sid()
    try:
        client = _twilio_verify_client()
        check = client.verify.v2.services(sid).verification_checks.create(to=phone, code=raw)
    except TwilioRestException as exc:
        logger.warning("Twilio Verify check failed: %s", exc)
        return False

    if getattr(check, "status", None) != "approved":
        return False

    now = datetime.now(timezone.utc)
    db.query(models.PhoneOtpChallenge).filter(models.PhoneOtpChallenge.user_id == user.user_id).delete()
    user.phone_verified_at = now
    db.add(user)
    db.commit()
    db.refresh(user)
    return True


def send_phone_verification(db: Session, user: models.Person) -> None:
    """Send or start phone verification (Twilio Verify if configured, else Programmable SMS OTP)."""
    if twilio_verify_configured():
        return send_phone_verification_twilio_verify(db, user)
    return send_phone_verification_otp(db, user)


def verify_phone_code(db: Session, user: models.Person, code: str) -> bool:
    """Validate the SMS code (Twilio Verify or local OTP challenge)."""
    if twilio_verify_configured():
        return verify_phone_twilio_verify(db, user, code)
    return verify_phone_otp(db, user, code)
