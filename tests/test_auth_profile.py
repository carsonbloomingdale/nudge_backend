from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import main
import models
from database import SessionLocal
from main import RegisterRequest


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_minimal_me_has_default_profile_fields(client, register_user):
    token, username, email = register_user(suffix="minimal")
    r = client.get("/auth/me", headers=_bearer_headers(token))
    assert r.status_code == 200
    data = r.json()
    assert data["username"] == username
    assert data["email"] == email
    assert data["first_name"] is None
    assert data["last_name"] is None
    assert data["phone_e164"] is None
    assert data["timezone"] is None
    assert data["sms_opt_in"] is False
    assert data["phone_verified_at"] is None


def test_auth_me_sms_test_sends_message(client, monkeypatch):
    import sms_checkin

    sent: list[tuple[str, str]] = []

    def fake_send(to: str, body: str) -> str:
        sent.append((to, body))
        return "SMtestfake"

    monkeypatch.setattr(sms_checkin, "send_twilio_sms", fake_send)
    r = client.post(
        "/auth/register",
        json={
            "username": "sms_test_btn_user",
            "email": "sms_test_btn@example.test",
            "password": "password123",
            "phone_e164": "+15555550888",
            "timezone": "America/Chicago",
            "sms_opt_in": True,
        },
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    client.cookies.clear()
    sent.clear()

    r2 = client.post("/auth/me/sms/test", headers=_bearer_headers(token))
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}
    assert len(sent) == 1
    assert sent[0][0] == "+15555550888"
    assert "Nudge test message" in sent[0][1]


def test_register_sms_opt_in_sends_welcome_sms(client, monkeypatch):
    import sms_checkin

    sent: list[tuple[str, str]] = []

    def fake_send(to: str, body: str) -> str:
        sent.append((to, body))
        return "SMwelcomefake"

    monkeypatch.setattr(sms_checkin, "send_twilio_sms", fake_send)
    r = client.post(
        "/auth/register",
        json={
            "username": "welcome_sms_user",
            "email": "welcome_sms@example.test",
            "password": "password123",
            "phone_e164": "+15555550999",
            "timezone": "America/Chicago",
            "sms_opt_in": True,
        },
    )
    assert r.status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == "+15555550999"
    assert "Welcome to Nudge" in sent[0][1]


def test_register_with_optional_profile_persisted(client, register_user):
    token, _, _ = register_user(
        suffix="full",
        extra={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone_e164": "+15555550123",
            "timezone": "America/New_York",
            "sms_opt_in": True,
        },
    )
    r = client.get("/auth/me", headers=_bearer_headers(token))
    assert r.status_code == 200
    data = r.json()
    assert data["first_name"] == "Ada"
    assert data["last_name"] == "Lovelace"
    assert data["phone_e164"] == "+15555550123"
    assert data["timezone"] == "America/New_York"
    assert data["sms_opt_in"] is True


def test_patch_partial_updates_only_sent_fields(client, register_user):
    token, _, _ = register_user(
        suffix="patchpart",
        extra={
            "first_name": "Keep",
            "last_name": "Me",
            "timezone": "UTC",
            "sms_opt_in": False,
        },
    )
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"sms_opt_in": True, "timezone": "Europe/Berlin"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["first_name"] == "Keep"
    assert data["last_name"] == "Me"
    assert data["timezone"] == "Europe/Berlin"
    assert data["sms_opt_in"] is True


def test_patch_invalid_phone_422(client, register_user):
    token, _, _ = register_user(suffix="badphone")
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"phone_e164": "555-1234"},
    )
    assert r.status_code == 422


def test_patch_invalid_timezone_422(client, register_user):
    token, _, _ = register_user(suffix="badtz")
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"timezone": "Not/A_Real_Zone"},
    )
    assert r.status_code == 422


def test_patch_email_conflict_409(client, register_user):
    token_a, _, _ = register_user(suffix="email_a")
    _, _, email_b = register_user(suffix="email_b")
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token_a),
        json={"email": email_b},
    )
    assert r.status_code == 409


def test_patch_username_conflict_409(client, register_user):
    token_a, _, _ = register_user(suffix="uname_a")
    _, username_b, _ = register_user(suffix="uname_b")
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token_a),
        json={"username": username_b},
    )
    assert r.status_code == 409


def test_patch_phone_change_clears_phone_verified_at(client, register_user):
    token, _, _ = register_user(
        suffix="verify",
        extra={"phone_e164": "+15555550100"},
    )
    db = SessionLocal()
    try:
        user = db.query(models.Person).filter(models.Person.user_name == "user_verify").one()
        user.phone_verified_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
    finally:
        db.close()

    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"phone_e164": "+15555550101"},
    )
    assert r.status_code == 200
    assert r.json()["phone_verified_at"] is None


def test_register_request_rejects_invalid_e164():
    with pytest.raises(ValidationError):
        RegisterRequest(
            username="u",
            email="a@b.co",
            password="password123",
            phone_e164="00invalid",
        )


def test_register_request_rejects_bad_timezone():
    with pytest.raises(ValidationError):
        RegisterRequest(
            username="u",
            email="a@b.co",
            password="password123",
            timezone="Mars/Phobos",
        )


def test_patch_request_accepts_empty_body_no_op(client, register_user):
    token, username, email = register_user(suffix="noop")
    r = client.patch("/auth/me", headers=_bearer_headers(token), json={})
    assert r.status_code == 200
    data = r.json()
    assert data["username"] == username
    assert data["email"] == email


def test_patch_clear_optional_string_with_null(client, register_user):
    token, _, _ = register_user(
        suffix="clear",
        extra={"first_name": "Temp", "phone_e164": "+15555550999"},
    )
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"first_name": None, "phone_e164": None},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["first_name"] is None
    assert data["phone_e164"] is None


def test_patch_extra_json_keys_ignored(client, register_user):
    token, _, _ = register_user(suffix="extra")
    r = client.patch(
        "/auth/me",
        headers=_bearer_headers(token),
        json={"first_name": "Fred", "not_allowlisted": "ignored"},
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Fred"
