from __future__ import annotations


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_pin_list_unpin_trait_flow(client, register_user):
    token, _, _ = register_user(suffix="pin_traits_flow")
    h = _bearer_headers(token)

    r1 = client.post("/api/analytics/pinned-traits", headers=h, json={"label": "Focused Trait"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["label"] == "Focused"

    r2 = client.get("/api/analytics/pinned-traits", headers=h)
    assert r2.status_code == 200, r2.text
    labels = [row["label"] for row in r2.json()["traits"]]
    assert labels == ["Focused"]

    r3 = client.delete("/api/analytics/pinned-traits/Focused", headers=h)
    assert r3.status_code == 200, r3.text
    assert r3.json()["traits"] == []


def test_sync_pinned_traits_dedupes_and_validates(client, register_user):
    token, _, _ = register_user(suffix="pin_traits_sync")
    h = _bearer_headers(token)

    r = client.put(
        "/api/analytics/pinned-traits",
        headers=h,
        json={"labels": ["Organized", "Organized", "Mindful traits"]},
    )
    assert r.status_code == 200, r.text
    labels = [row["label"] for row in r.json()["traits"]]
    assert set(labels) == {"Organized", "Mindful"}

    bad = client.post("/api/analytics/pinned-traits", headers=h, json={"label": "Focused & Driven"})
    assert bad.status_code == 422
