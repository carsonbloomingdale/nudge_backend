from __future__ import annotations

import json

import main


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_suggestions_no_body_uses_backend_history_and_smart_signals(client, register_user, monkeypatch):
    token, _, _ = register_user(suffix="suggestions_backend")
    h = _bearer_headers(token)

    for i in range(10):
        trait = "Mindful" if i < 7 else "Focused"
        r = client.post(
            "/tasks/",
            headers=h,
            json={
                "label": f"task {i}",
                "category": "work",
                "context": "context",
                "sentiment": "neutral",
                "personality_traits": [trait],
            },
        )
        assert r.status_code == 200, r.text

    captured: dict[str, object] = {}

    async def fake_openai(system_prompt: str, user_prompt: str, *, temperature: float = 0.2):
        captured["prompt"] = json.loads(user_prompt)
        return {"suggestion": {"reccomendedTask": "Do one focused mindful check-in.", "context": "pattern-based"}}, 0

    monkeypatch.setattr(main, "openai_chat_completion", fake_openai)

    resp = client.post("/api/suggestions", headers=h)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["suggestion"]["reccomendedTask"]

    prompt = captured["prompt"]
    assert isinstance(prompt, dict)
    assert len(prompt.get("recent_task_history", [])) > 0
    assert "smart_signals" in prompt
