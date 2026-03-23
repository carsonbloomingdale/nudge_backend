from __future__ import annotations

import main


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_me_includes_enrichment_summary_null(client, register_user):
    token, _, _ = register_user(suffix="enrichsum_null")
    r = client.get("/auth/me", headers=_bearer_headers(token))
    assert r.status_code == 200
    data = r.json()
    assert "enrichment_summary" in data
    assert data["enrichment_summary"] is None


def test_refresh_enrichment_summary_persists(client, register_user, monkeypatch):
    token, _, _ = register_user(suffix="enrichsum_refresh")

    async def fake_openai(system_prompt: str, user_prompt: str, *, temperature: float = 0.2):
        assert "recent_tasks_sample" in user_prompt
        return {"summary": "Prefers structured work and calm pacing."}, 0

    monkeypatch.setattr(main, "openai_chat_completion", fake_openai)

    r = client.post(
        "/auth/me/enrichment-summary/refresh",
        headers=_bearer_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == "Prefers structured work and calm pacing."
    assert body["meta"]["model"]

    r2 = client.get("/auth/me", headers=_bearer_headers(token))
    assert r2.status_code == 200
    assert r2.json()["enrichment_summary"] == "Prefers structured work and calm pacing."


def test_build_batch_prompt_uses_summary_not_full_history():
    huge = [{"label": f"t{i}", "category": "x", "sentiment": "n", "context": "c" * 50} for i in range(40)]
    system, user = main._build_batch_enrich_prompts(
        ["a", "b"],
        huge,
        enrichment_summary="Server-side profile about the user.",
    )
    assert "user_profile_summary" in user
    assert "Server-side profile" in user
    parsed = __import__("json").loads(user)
    assert parsed["user_profile_summary"].startswith("Server-side")
    hist = parsed.get("recent_task_history") or []
    assert len(hist) <= 2


def test_normalize_enriched_task_splits_combined_traits_and_strips_trait_suffix():
    raw = {
        "task": {
            "label": "Done workout",
            "category": "health",
            "sentiment": "positive",
            "personality_traits": [
                "Thoughtful & Mindful Trait",
                "Organized, Health-Conscious",
                "Resilient traits",
            ],
        }
    }
    out = main._normalize_enriched_task(raw, "Done workout")
    assert out.personality_traits == [
        "Thoughtful",
        "Mindful",
        "Organized",
        "Health-Conscious",
        "Resilient",
    ]


def test_normalize_enriched_task_includes_all_pinned_traits():
    raw = {
        "task": {
            "label": "Plan deep work",
            "category": "work",
            "sentiment": "neutral",
            "personality_traits": ["Focused"],
        }
    }
    out = main._normalize_enriched_task(raw, "Plan deep work", pinned_traits=["Mindful", "Disciplined"])
    assert "Focused" in out.personality_traits
    assert "Mindful" in out.personality_traits
    assert "Disciplined" in out.personality_traits
