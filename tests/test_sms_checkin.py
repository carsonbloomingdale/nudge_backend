from __future__ import annotations

import os
import uuid
from datetime import date
from typing import Any

import pytest
from twilio.request_validator import RequestValidator

import models
from database import SessionLocal


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _twilio_form_post(client, path: str, data: dict[str, str]):
    token = os.environ["TWILIO_AUTH_TOKEN"]
    url = f"http://testserver{path}"
    sig = RequestValidator(token).compute_signature(url, data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": sig})


@pytest.fixture
def sms_profile(client, register_user):
    suf = uuid.uuid4().hex[:12]
    lower = uuid.uuid4().int % 10_000_000_000
    phone = f"+1{lower:010d}"
    token, username, _ = register_user(
        suffix=suf,
        extra={
            "phone_e164": phone,
            "timezone": "America/New_York",
            "sms_opt_in": True,
        },
    )
    return token, username, phone


def test_twilio_webhook_rejects_bad_signature(client, sms_profile):
    _token, _username, phone = sms_profile
    r = client.post(
        "/webhooks/twilio/sms",
        data={"Body": "hi", "From": phone, "SmsSid": "SMbad_sig"},
        headers={"X-Twilio-Signature": "deadbeef"},
    )
    assert r.status_code == 403


def test_twilio_inbound_creates_tasks_idempotent(client, sms_profile, monkeypatch):
    import sms_checkin

    token, username, phone = sms_profile

    async def fake_extract(sms_text: str, day_of_week: str) -> list[dict[str, Any]]:
        _ = sms_text, day_of_week
        return [
            {
                "sentiment": "positive",
                "category": "work",
                "label": "Shipped SMS feature",
                "context": "From test",
                "time_of_day": "evening",
                "amount_of_time": "1h",
                "day_of_week": "Sunday",
            }
        ]

    monkeypatch.setattr(sms_checkin, "_extract_tasks_from_reply", fake_extract)

    db = SessionLocal()
    try:
        user = db.query(models.Person).filter(models.Person.user_name == username).one()
        db.add(
            models.SmsDailyCheckin(
                user_id=user.user_id,
                local_date=date.today().isoformat(),
                outbound_message_sid="SMoutboundtest",
                status="awaiting_reply",
            )
        )
        db.commit()
    finally:
        db.close()

    sid = f"SMidemp{uuid.uuid4().hex}"
    payload = {
        "Body": "Finished the SMS pipeline today",
        "From": phone,
        "To": "+15550000001",
        "SmsSid": sid,
        "MessageSid": sid,
    }
    r1 = _twilio_form_post(client, "/webhooks/twilio/sms", payload)
    assert r1.status_code == 200

    r_tasks = client.get("/tasks/", headers=_bearer(token))
    assert r_tasks.status_code == 200
    labels = [t["label"] for t in r_tasks.json()]
    assert any("Shipped SMS feature" in x for x in labels)

    r2 = _twilio_form_post(client, "/webhooks/twilio/sms", payload)
    assert r2.status_code == 200
    r_tasks2 = client.get("/tasks/", headers=_bearer(token))
    assert sum(1 for t in r_tasks2.json() if "Shipped SMS feature" in t["label"]) == 1


def test_stop_opt_out(client, sms_profile):
    token, _username, phone = sms_profile
    sid = f"SMstop{uuid.uuid4().hex[:8]}"
    payload = {
        "Body": "stop",
        "From": phone,
        "SmsSid": sid,
        "MessageSid": sid,
    }
    r = _twilio_form_post(client, "/webhooks/twilio/sms", payload)
    assert r.status_code == 200
    me = client.get("/auth/me", headers=_bearer(token))
    assert me.json()["sms_opt_in"] is False


def test_internal_run_prompts_requires_secret(client, monkeypatch):
    monkeypatch.delenv("SCHEDULER_SECRET", raising=False)
    r = client.post("/internal/sms/run-daily-prompts", headers={"X-Scheduler-Secret": "anything"})
    assert r.status_code == 503

    monkeypatch.setenv("SCHEDULER_SECRET", "unit-test-scheduler-secret")
    r2 = client.post(
        "/internal/sms/run-daily-prompts",
        headers={"X-Scheduler-Secret": "wrong"},
    )
    assert r2.status_code == 403

    r3 = client.post(
        "/internal/sms/run-daily-prompts",
        headers={"X-Scheduler-Secret": "unit-test-scheduler-secret"},
    )
    assert r3.status_code == 200
    assert r3.json().get("ok") is True
