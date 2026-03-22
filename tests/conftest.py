"""
Pytest loads this file before test modules. Set env so `database` / `main` use an isolated
in-memory SQLite DB and never read `.env.local` (which would override DATABASE_URL).
"""

from __future__ import annotations

import os

os.environ["NUDGE_TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["JWT_SECRET_KEY"] = "unit-test-jwt-secret-key-32chars!!"
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_twilio_auth_token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000001")
os.environ.setdefault("SMS_DRY_RUN", "true")

import pytest

import main  # noqa: E402  — after env


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(main.app)


@pytest.fixture
def register_user(client):
    """Register a unique user; return (access_token, username, email)."""

    def _register(
        *,
        suffix: str,
        extra: dict | None = None,
    ) -> tuple[str, str, str]:
        username = f"user_{suffix}"
        email = f"{suffix}@example.test"
        body = {
            "username": username,
            "email": email,
            "password": "password123",
        }
        if extra:
            body.update(extra)
        r = client.post("/auth/register", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        # Register sets JWT cookies; get_access_token_from_request reads cookie before
        # Authorization. Clear cookies so tests that pass Bearer use that token, not
        # the last user who registered on this client.
        client.cookies.clear()
        return data["access_token"], username, email

    return _register
