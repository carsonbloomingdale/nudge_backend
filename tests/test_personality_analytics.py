from __future__ import annotations

from uuid import UUID

import models
from database import SessionLocal


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_personality_chart_cache_hit_raw_only(client, register_user):
    token, _, _ = register_user(suffix="ptcache")
    me = client.get("/auth/me", headers=_bearer(token))
    uid = UUID(me.json()["user_id"])

    db = SessionLocal()
    try:
        t = models.Task(
            user_id=uid,
            journal_id=None,
            sentiment="neutral",
            category="work",
            label="x",
            context="",
            time_of_day="morning",
            amount_of_time="5m",
            day_of_week="Mon",
        )
        db.add(t)
        db.commit()
    finally:
        db.close()

    h = _bearer(token)
    r1 = client.get("/api/analytics/personality-traits-chart?use_ai=false", headers=h)
    assert r1.status_code == 200
    assert r1.json().get("meta", {}).get("cached") is not True

    r2 = client.get("/api/analytics/personality-traits-chart?use_ai=false", headers=h)
    assert r2.status_code == 200
    assert r2.json()["meta"].get("cached") is True
    assert r2.json()["aggregate_source"] == r1.json()["aggregate_source"]


def test_personality_chart_empty(client, register_user):
    token, _, _ = register_user(suffix="ptempty")
    r = client.get(
        "/api/analytics/personality-traits-chart?use_ai=false",
        headers=_bearer(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_associations"] == 0
    assert data["segments"] == []
    assert data["chart_mode"] == "raw_only"


def test_personality_chart_falls_back_to_task_category(client, register_user):
    """When personality_traits table is empty, chart uses task.category counts."""
    token, _, _ = register_user(suffix="ptcat")
    me = client.get("/auth/me", headers=_bearer(token))
    uid = UUID(me.json()["user_id"])

    db = SessionLocal()
    try:
        t = models.Task(
            user_id=uid,
            journal_id=None,
            sentiment="neutral",
            category="health",
            label="Walk",
            context="",
            time_of_day="morning",
            amount_of_time="5m",
            day_of_week="Mon",
        )
        db.add(t)
        db.commit()
    finally:
        db.close()

    r = client.get(
        "/api/analytics/personality-traits-chart?use_ai=false",
        headers=_bearer(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["aggregate_source"] == "task_category"
    assert data["total_associations"] == 1
    assert data["raw_aggregates"] == [{"label": "health", "count": 1}]


def test_personality_chart_raw_aggregates(client, register_user):
    token, _, _ = register_user(suffix="ptagg")
    me = client.get("/auth/me", headers=_bearer(token))
    uid = UUID(me.json()["user_id"])

    db = SessionLocal()
    try:
        t = models.Task(
            user_id=uid,
            journal_id=None,
            sentiment="neutral",
            category="x",
            label="task1",
            context="",
            time_of_day="morning",
            amount_of_time="5m",
            day_of_week="Mon",
        )
        db.add(t)
        db.flush()
        db.add(
            models.PersonalityTrait(
                task_id=t.task_id,
                trait_id=1,
                label="Planner",
            )
        )
        db.add(
            models.PersonalityTrait(
                task_id=t.task_id,
                trait_id=2,
                label="Planner",
            )
        )
        db.add(
            models.PersonalityTrait(
                task_id=t.task_id,
                trait_id=3,
                label="Creative",
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.get(
        "/api/analytics/personality-traits-chart?use_ai=false",
        headers=_bearer(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_associations"] == 3
    assert data["aggregate_source"] == "personality_traits"
    assert {x["label"]: x["count"] for x in data["raw_aggregates"]} == {"Planner": 2, "Creative": 1}
    assert len(data["segments"]) == 2
    assert data["chart_mode"] == "raw_only"


def test_personality_chart_prefers_pinned_canonical_trait_label(client, register_user):
    token, _, _ = register_user(suffix="ptpin")
    me = client.get("/auth/me", headers=_bearer(token))
    uid = UUID(me.json()["user_id"])

    db = SessionLocal()
    try:
        db.add(models.PinnedPersonalityTrait(user_id=uid, label="Wellness Focused"))
        t = models.Task(
            user_id=uid,
            journal_id=None,
            sentiment="neutral",
            category="health",
            label="task1",
            context="",
            time_of_day="morning",
            amount_of_time="5m",
            day_of_week="Mon",
        )
        db.add(t)
        db.flush()
        db.add(models.PersonalityTrait(task_id=t.task_id, trait_id=101, label="Wellness Oriented"))
        db.add(models.PersonalityTrait(task_id=t.task_id, trait_id=102, label="Wellness Focused"))
        db.commit()
    finally:
        db.close()

    r = client.get(
        "/api/analytics/personality-traits-chart?use_ai=false",
        headers=_bearer(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert {x["label"]: x["count"] for x in data["raw_aggregates"]} == {"Wellness Focused": 2}
    assert data["meta"].get("pinned_canonicalization_applied") == 1
