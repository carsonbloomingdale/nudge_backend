from __future__ import annotations


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_post_tasks_creates_journal_and_task(client, register_user):
    token, _, _ = register_user(suffix="jt1")
    r = client.post(
        "/tasks/",
        headers=_bearer(token),
        json={
            "sentiment": "positive",
            "category": "work",
            "label": "Ship feature",
            "context": "test",
            "time_of_day": "morning",
            "amount_of_time": "30m",
            "day_of_week": "Monday",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["journal_id"] is not None
    assert data["label"] == "Ship feature"


def test_journal_crud_and_embedded_tasks(client, register_user):
    token, _, _ = register_user(suffix="jfull")
    h = _bearer(token)
    create = client.post(
        "/api/journals/",
        headers=h,
        json={
            "items": [
                {
                    "sentiment": "neutral",
                    "category": "health",
                    "label": "Walk",
                    "context": "outside",
                    "time_of_day": "afternoon",
                    "amount_of_time": "20m",
                    "day_of_week": "Tuesday",
                },
                {
                    "sentiment": "positive",
                    "category": "social",
                    "label": "Call friend",
                    "context": "",
                    "time_of_day": "evening",
                    "amount_of_time": "1h",
                    "day_of_week": "Tuesday",
                },
            ],
            "note": " Good day ",
            "source": "app",
        },
    )
    assert create.status_code == 200, create.text
    body = create.json()
    jid = body["journal_id"]
    assert body["note"] == "Good day"
    assert len(body["tasks"]) == 2
    assert {t["label"] for t in body["tasks"]} == {"Walk", "Call friend"}

    lst = client.get("/api/journals/", headers=h)
    assert lst.status_code == 200
    assert len(lst.json()) == 1

    one = client.get(f"/api/journals/{jid}", headers=h)
    assert one.status_code == 200
    assert len(one.json()["tasks"]) == 2

    patch = client.patch(f"/api/journals/{jid}", headers=h, json={"note": None})
    assert patch.status_code == 200
    assert patch.json()["note"] is None

    dl = client.delete(f"/api/journals/{jid}", headers=h)
    assert dl.status_code == 204

    empty = client.get("/api/journals/", headers=h)
    assert empty.json() == []


def test_journal_accepts_notes_alias(client, register_user):
    token, _, _ = register_user(suffix="alias")
    h = _bearer(token)
    r = client.post(
        "/api/journals/",
        headers=h,
        json={
            "items": [
                {
                    "sentiment": "neutral",
                    "category": "x",
                    "label": "y",
                    "context": "",
                    "time_of_day": "morning",
                    "amount_of_time": "5m",
                    "day_of_week": "Wed",
                }
            ],
            "notes": "Written via notes key",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "Written via notes key"


def test_presign_returns_503_without_s3(client, register_user, monkeypatch):
    monkeypatch.delenv("S3_ATTACHMENTS_BUCKET", raising=False)
    import journal_storage

    assert not journal_storage.attachments_configured()

    token, _, _ = register_user(suffix="jpre")
    h = _bearer(token)
    c = client.post(
        "/api/journals/",
        headers=h,
        json={
            "items": [
                {
                    "sentiment": "neutral",
                    "category": "x",
                    "label": "y",
                    "context": "",
                    "time_of_day": "morning",
                    "amount_of_time": "5m",
                    "day_of_week": "Wed",
                }
            ]
        },
    )
    jid = c.json()["journal_id"]
    r = client.post(
        f"/api/journals/{jid}/attachments/presign",
        headers=h,
        json={"content_type": "image/jpeg", "byte_size": 1024},
    )
    assert r.status_code == 503
