"""App-feature tests: public routes, deny-by-default auth, AI wiring.

The DDUUI chrome itself is covered by the shared Gateway conformance suite
(gudmundr-gateway tests/e2e/dduui_conformance), not here.
Run: uv run --with pytest pytest -q tests
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SECRET_KEY", "test-only-secret")
os.environ.pop("GATEWAY_SERVICE_TOKEN", None)
os.environ.pop("XAI_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import app as meals  # noqa: E402
import llm_client  # noqa: E402

client = TestClient(meals.app)


def test_healthz_public():
    assert client.get("/healthz").status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/api/entries", "/api/summary", "/api/me", "/api/export",
                                  "/api/report-data", "/api/blood-sugar",
                                  "/api/ai/status", "/api/ecosystem/my-apps"])
def test_unauthenticated_api_is_401(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/estimate", "/api/ai/probe", "/api/admin/backfill-calories-from-journal"])
def test_unauthenticated_post_is_401(path):
    assert client.post(path, json={}).status_code == 401


@pytest.mark.parametrize("path", ["/", "/journal", "/report", "/search", "/help"])
def test_unauthenticated_pages_show_sign_in_in_place(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert 'href="/login/gateway"' in r.text
    assert "data-dduui-menu" not in r.text  # no page menu, no user data
    assert r.headers["cache-control"] == "no-store"


def test_login_page_loads_kit_102():
    r = client.get("/login")
    assert r.status_code == 200
    assert "/static/dduui/v1.0.2/chrome.js?r=1.0.2" in r.text
    assert "/static/dduui/v1.0.2/chrome.css?r=1.0.2" in r.text
    assert "/svc/dduui/tokens.css?theme=lumen" in r.text
    assert "data-cc-pin" not in r.text


def test_estimate_reports_tier(monkeypatch):
    def fake(messages, **kw):
        return {"content": "{}", "tier": "granite-inproc", "model": "g", "served_model": "g-1b",
                "latency_ms": 5, "parsed": kw["validate"]('{"total_calories": 300, "items": []}'),
                "attempts": [], "fallback_from": None, "served_provider": None}
    monkeypatch.setattr(llm_client, "chat_meta", fake)
    r = meals.estimate_text("an apple")
    assert r["total_calories"] == 300
    assert r["ai"] == {"tier": "granite-inproc", "model": "g-1b", "latency_ms": 5}
    assert "error" not in r


def test_estimate_graceful_when_no_ai(monkeypatch):
    def none(messages, **kw):
        raise llm_client.AINotAvailable("AI not available")
    monkeypatch.setattr(llm_client, "chat_meta", none)
    r = meals.estimate_text("an apple")
    assert r["error"] is True
    assert r["notes"] == meals.AI_UNAVAILABLE


def test_photo_uses_vision(monkeypatch):
    seen = {}

    def fake(messages, **kw):
        seen.update(kw)
        raise llm_client.AINotAvailable("x")
    monkeypatch.setattr(llm_client, "chat_meta", fake)
    meals.estimate_photo(b"\x89PNG", "image/png")
    assert seen["vision"] is True


def test_valid_estimate_rejects_prose():
    with pytest.raises(Exception):
        meals._valid_estimate("I think about 300 calories")
    with pytest.raises(ValueError):
        meals._valid_estimate('{"total_calories": "lots"}')
    assert meals._valid_estimate('```json\n{"total_calories": 5}\n```')["total_calories"] == 5


def test_no_direct_provider_calls_in_app_code():
    """Only voice_xai.py (documented residual) may name a direct provider."""
    for name in ("app.py", "llm_client.py", "granite_inproc.py"):
        src = (ROOT / name).read_text().lower()
        for needle in ("api.x.ai", "xai_api_key", "groq", "anthropic", "nvidia", "radium", "keychain"):
            assert needle not in src, f"{needle} in {name}"
