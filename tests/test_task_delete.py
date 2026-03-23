from __future__ import annotations


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_delete_task_204_and_gone(client, register_user):
    token, _, _ = register_user(suffix="tdel")
    h = _bearer(token)
    r = client.post(
        "/tasks/",
        headers=h,
        json={
            "sentiment": "neutral",
            "category": "work",
            "label": "One",
            "context": "",
            "time_of_day": "morning",
            "amount_of_time": "5m",
            "day_of_week": "Mon",
            "personality_traits": ["Focused"],
        },
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]

    d = client.delete(f"/tasks/{tid}", headers=h)
    assert d.status_code == 204

    lst = client.get("/tasks/", headers=h)
    assert lst.status_code == 200
    ids = {t["task_id"] for t in lst.json()}
    assert tid not in ids


def test_delete_task_wrong_user_404(client, register_user):
    t1, _, _ = register_user(suffix="owner")
    t2, _, _ = register_user(suffix="other")
    h1 = _bearer(t1)
    h2 = _bearer(t2)
    r = client.post(
        "/tasks/",
        headers=h1,
        json={
            "sentiment": "neutral",
            "category": "x",
            "label": "Mine",
            "context": "",
            "time_of_day": "morning",
            "amount_of_time": "5m",
            "day_of_week": "Mon",
        },
    )
    tid = r.json()["task_id"]
    d = client.delete(f"/tasks/{tid}", headers=h2)
    assert d.status_code == 404
