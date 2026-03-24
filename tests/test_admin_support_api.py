from __future__ import annotations

import models
from database import SessionLocal


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _set_role(username: str, role: str) -> None:
    db = SessionLocal()
    try:
        u = db.query(models.Person).filter(models.Person.user_name == username).first()
        assert u is not None
        u.role = role
        db.add(u)
        db.commit()
    finally:
        db.close()


def test_customer_ticket_ownership_and_reply(client, register_user):
    token_a, username_a, _ = register_user(suffix="ticketa")
    token_b, _, _ = register_user(suffix="ticketb")

    r = client.post(
        "/api/support/tickets",
        headers=_bearer(token_a),
        json={"subject": "Need help", "message": "Issue details", "priority": "normal"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["ticket_id"]

    own = client.get(f"/api/support/tickets/{tid}", headers=_bearer(token_a))
    assert own.status_code == 200

    other = client.get(f"/api/support/tickets/{tid}", headers=_bearer(token_b))
    assert other.status_code == 404

    reply = client.post(
        f"/api/support/tickets/{tid}/messages",
        headers=_bearer(token_a),
        json={"body": "additional context"},
    )
    assert reply.status_code == 200
    assert len(reply.json()["messages"]) >= 2

    # Ensure user exists and role setup helper touched valid row.
    db = SessionLocal()
    try:
        ua = db.query(models.Person).filter(models.Person.user_name == username_a).first()
        assert ua is not None
    finally:
        db.close()


def test_admin_routes_block_non_admin_and_allow_admin(client, register_user):
    admin_token, admin_username, _ = register_user(suffix="adminuser")
    user_token, _, _ = register_user(suffix="regularuser")
    _set_role(admin_username, "admin")

    # Non-admin blocked
    blocked = client.get("/api/admin/support/tickets", headers=_bearer(user_token))
    assert blocked.status_code == 403

    # Admin can list
    ok = client.get("/api/admin/support/tickets", headers=_bearer(admin_token))
    assert ok.status_code == 200
    assert "tickets" in ok.json()


def test_admin_ticket_update_and_audit_event(client, register_user):
    admin_token, admin_username, _ = register_user(suffix="adminaudit")
    customer_token, _, _ = register_user(suffix="custaudit")
    _set_role(admin_username, "admin")

    created = client.post(
        "/api/support/tickets",
        headers=_bearer(customer_token),
        json={"subject": "Bug report", "message": "please help", "priority": "low"},
    )
    assert created.status_code == 200
    tid = created.json()["ticket_id"]

    patched = client.patch(
        f"/api/admin/support/tickets/{tid}",
        headers=_bearer(admin_token),
        json={"status": "in_progress", "priority": "high"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_progress"
    assert patched.json()["priority"] == "high"

    db = SessionLocal()
    try:
        admin = db.query(models.Person).filter(models.Person.user_name == admin_username).first()
        assert admin is not None
        audit = (
            db.query(models.AdminAuditEvent)
            .filter(
                models.AdminAuditEvent.admin_user_id == admin.user_id,
                models.AdminAuditEvent.action == "admin_ticket_update",
                models.AdminAuditEvent.target_type == "support_ticket",
                models.AdminAuditEvent.target_id == str(tid),
            )
            .first()
        )
        assert audit is not None
    finally:
        db.close()


def test_admin_can_get_ticket_detail(client, register_user):
    admin_token, admin_username, _ = register_user(suffix="admindetail")
    customer_token, _, _ = register_user(suffix="custdetail")
    _set_role(admin_username, "admin")

    created = client.post(
        "/api/support/tickets",
        headers=_bearer(customer_token),
        json={"subject": "Need details", "message": "ticket body", "priority": "normal"},
    )
    assert created.status_code == 200
    tid = created.json()["ticket_id"]

    detail = client.get(f"/api/admin/support/tickets/{tid}", headers=_bearer(admin_token))
    assert detail.status_code == 200
    data = detail.json()
    assert data["ticket_id"] == tid
    assert data["subject"] == "Need details"
    assert len(data["messages"]) >= 1
