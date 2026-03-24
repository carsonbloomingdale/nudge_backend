from __future__ import annotations


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_task(client, token: str, *, category: str, label: str, traits: list[str]) -> None:
    r = client.post(
        "/tasks/",
        headers=_bearer(token),
        json={
            "sentiment": "neutral",
            "category": category,
            "label": label,
            "context": "test context",
            "time_of_day": "morning",
            "amount_of_time": "15m",
            "day_of_week": "Mon",
            "personality_traits": traits,
        },
    )
    assert r.status_code == 200, r.text


def test_growth_goal_suggestions_and_pin(client, register_user):
    token, _, _ = register_user(suffix="goalsuggest")
    _create_task(client, token, category="health", label="Morning run", traits=["Disciplined"])
    _create_task(client, token, category="health", label="Workout", traits=["Disciplined"])

    s = client.get("/api/growth-goals/suggestions", headers=_bearer(token))
    assert s.status_code == 200, s.text
    payload = s.json()
    assert payload["suggestions"]
    top_goal = payload["suggestions"][0]
    assert top_goal["score"] >= 1

    p = client.post(f"/api/growth-goals/{top_goal['goal_id']}/pin", headers=_bearer(token))
    assert p.status_code == 200, p.text
    pinned = p.json()["goals"]
    assert any(g["goal_id"] == top_goal["goal_id"] for g in pinned)


def test_goal_and_trait_activity_endpoints(client, register_user):
    token, _, _ = register_user(suffix="goalseries")
    _create_task(client, token, category="focus", label="Deep work block", traits=["Focused"])
    _create_task(client, token, category="focus", label="Plan roadmap", traits=["Focused", "Planner"])

    s = client.get("/api/growth-goals/suggestions", headers=_bearer(token))
    assert s.status_code == 200, s.text
    goal_id = s.json()["suggestions"][0]["goal_id"]

    g = client.get(
        f"/api/analytics/growth-goals/{goal_id}/activity?grain=day",
        headers=_bearer(token),
    )
    assert g.status_code == 200, g.text
    gdata = g.json()
    assert gdata["grain"] == "day"
    assert gdata["total"] >= 1
    assert len(gdata["buckets"]) >= 1

    t = client.get(
        "/api/analytics/traits/Focused/activity?grain=day",
        headers=_bearer(token),
    )
    assert t.status_code == 200, t.text
    tdata = t.json()
    assert tdata["total"] >= 1

    tt = client.get("/api/analytics/traits/activity/totals?grain=day", headers=_bearer(token))
    assert tt.status_code == 200, tt.text
    assert tt.json()["total"] >= tdata["total"]

    # Case-insensitive trait lookup should work for FE-provided labels.
    t_case = client.get(
        "/api/analytics/traits/focused/activity?grain=day",
        headers=_bearer(token),
    )
    assert t_case.status_code == 200, t_case.text
    assert t_case.json()["total"] == tdata["total"]

    by_label = client.get("/api/analytics/traits/activity/by-label?grain=day", headers=_bearer(token))
    assert by_label.status_code == 200, by_label.text
    labels = {row["trait_label"]: row["total"] for row in by_label.json()["traits"]}
    assert labels.get("Focused", 0) >= 1

    gt = client.get("/api/analytics/growth-goals/activity/totals?grain=day", headers=_bearer(token))
    assert gt.status_code == 200, gt.text
    assert gt.json()["total"] >= gdata["total"]
