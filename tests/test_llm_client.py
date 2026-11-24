"""llm_client + granite_inproc unit tests with a fake Granite engine.

No real inference here: onnxruntime>=1.20 has no Intel macOS 12 wheel and
models are never downloaded to the Mac. Real inference is proven on prod
(/api/ai/probe). Run: uv run --with pytest pytest -q tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import granite_inproc  # noqa: E402
import llm_client  # noqa: E402

MSGS = [{"role": "user", "content": "hi"}]
_REAL_CHAIN_READY = granite_inproc.chain_ready


class FakeHTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    llm_client._reset_for_tests()
    monkeypatch.setenv("GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("GATEWAY_SERVICE_TOKEN", "test-token")
    calls = {"granite": 0, "gateway": []}

    def fake_granite(messages, max_tokens, temperature):
        calls["granite"] += 1
        return '{"total_calories": 100, "items": []}', "granite-test", {
            "served_provider": "granite-inproc", "served_model": "granite-test"}

    def fake_gateway(messages, model, max_tokens, temperature):
        calls["gateway"].append(model)
        return '{"total_calories": 200, "items": []}', model, {
            "served_provider": "gemini", "served_model": "gemini-x"}

    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: True)
    monkeypatch.setattr(llm_client, "_call_granite", fake_granite)
    monkeypatch.setattr(llm_client, "_call_gateway", fake_gateway)
    yield calls
    llm_client._reset_for_tests()


def test_granite_answers_first(_env):
    r = llm_client.chat_meta(MSGS)
    assert r["tier"] == "granite-inproc"
    assert r["fallback_from"] is None
    assert _env["gateway"] == []


def test_granite_not_ready_goes_to_gateway_with_auto(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)
    r = llm_client.chat_meta(MSGS)
    assert r["tier"] == "gateway"
    assert _env["gateway"] == ["auto"]
    assert r["served_provider"] == "gemini"


def test_busy_granite_falls_through_without_tripping_breaker(_env, monkeypatch):
    def busy(*a, **k):
        raise granite_inproc.GraniteBusy("busy")
    monkeypatch.setattr(llm_client, "_call_granite", busy)
    for _ in range(5):
        r = llm_client.chat_meta(MSGS)
        assert r["tier"] == "gateway"
        assert r["fallback_from"] == "granite-inproc"
        assert r["attempts"][0]["skipped"] is True
    assert not llm_client._cb_is_open("granite-inproc")


def test_granite_timeout_counts_and_trips_after_three(_env, monkeypatch):
    def slow(*a, **k):
        raise granite_inproc.GraniteTimeout("slow")
    monkeypatch.setattr(llm_client, "_call_granite", slow)
    for _ in range(3):
        assert llm_client.chat_meta(MSGS)["tier"] == "gateway"
    assert llm_client._cb_is_open("granite-inproc")
    r = llm_client.chat_meta(MSGS)
    assert r["attempts"] == []  # shelved granite is not even tried


def test_validate_rejection_moves_to_next_tier(_env, monkeypatch):
    monkeypatch.setattr(llm_client, "_call_granite",
                        lambda *a: ("not json at all", "g", {"served_provider": None, "served_model": None}))
    import json
    r = llm_client.chat_meta(MSGS, validate=json.loads)
    assert r["tier"] == "gateway"
    assert r["parsed"] == {"total_calories": 200, "items": []}
    assert r["attempts"][0]["error"] == "Rejected"
    assert not llm_client._cb_is_open("granite-inproc")


def test_vision_skips_granite_and_uses_vision_model(_env, monkeypatch):
    monkeypatch.setenv("GATEWAY_VISION_MODEL", "gemini:default")
    r = llm_client.chat_meta(MSGS, vision=True)
    assert r["tier"] == "gateway"
    assert _env["granite"] == 0
    assert _env["gateway"] == ["gemini:default"]


def test_gateway_breaker_three_failures_in_60s(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)

    def boom(*a, **k):
        raise FakeHTTPError(502)
    monkeypatch.setattr(llm_client, "_call_gateway", boom)
    for _ in range(3):
        with pytest.raises(llm_client.AINotAvailable):
            llm_client.chat_meta(MSGS)
    assert llm_client._cb_is_open("gateway")
    with pytest.raises(llm_client.AINotAvailable) as ei:
        llm_client.chat_meta(MSGS)
    assert ei.value.attempts == []


def test_gateway_401_shelves_at_once(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)

    def unauth(*a, **k):
        raise FakeHTTPError(401)
    monkeypatch.setattr(llm_client, "_call_gateway", unauth)
    with pytest.raises(llm_client.AINotAvailable):
        llm_client.chat_meta(MSGS)
    assert llm_client._cb_is_open("gateway")


def test_success_resets_breaker(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)
    state = {"fail": True}

    def flaky(*a, **k):
        if state["fail"]:
            raise FakeHTTPError(500)
        return "ok", "auto", {"served_provider": None, "served_model": None}
    monkeypatch.setattr(llm_client, "_call_gateway", flaky)
    for _ in range(2):
        with pytest.raises(llm_client.AINotAvailable):
            llm_client.chat_meta(MSGS)
    state["fail"] = False
    assert llm_client.chat_meta(MSGS)["tier"] == "gateway"
    assert "gateway" not in llm_client._CB_FAILURES


def test_no_tiers_is_graceful(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)
    monkeypatch.delenv("GATEWAY_SERVICE_TOKEN")
    with pytest.raises(llm_client.AINotAvailable, match="AI not available"):
        llm_client.chat_meta(MSGS)


def test_model_hint_resolves_per_tier(_env, monkeypatch):
    monkeypatch.setattr(granite_inproc, "chain_ready", lambda: False)
    monkeypatch.setenv("GATEWAY_MODEL_FAST", "auto:fast")
    llm_client.chat_meta(MSGS, model_hint="fast")
    llm_client.chat_meta(MSGS, model_hint="default")
    assert _env["gateway"] == ["auto:fast", "auto"]


def test_stream_falls_over_only_before_first_token(_env, monkeypatch):
    def not_ready(*a, **k):
        raise granite_inproc.GraniteUnavailable("loading")
    monkeypatch.setattr(granite_inproc, "generate_stream", not_ready)

    class Chunk:
        def __init__(self, t):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": t})()})()]

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return iter([Chunk("Hel"), Chunk("lo")])
    monkeypatch.setattr(llm_client, "_gateway_client", lambda: FakeClient)
    metas = []
    assert "".join(llm_client.chat_stream(MSGS, on_meta=metas.append)) == "Hello"
    assert metas[0]["tier"] == "gateway"
    assert metas[0]["attempts"][0]["tier"] == "granite-inproc"


def test_stream_mid_stream_failure_raises(_env, monkeypatch):
    def half(*a, **k):
        yield "first"
        raise RuntimeError("cut")
    monkeypatch.setattr(granite_inproc, "generate_stream", half)
    got = []
    with pytest.raises(RuntimeError, match="cut"):
        for t in llm_client.chat_stream(MSGS):
            got.append(t)
    assert got == ["first"]


def test_status_has_no_secrets(_env):
    st = llm_client.status()
    assert "test-token" not in repr(st)
    assert st["chain"] == ["granite-inproc", "gateway"]


# ── granite_inproc sizing + hosting (no model is loaded) ──

def test_size_by_memory(monkeypatch):
    monkeypatch.delenv("GRANITE_MODEL", raising=False)
    monkeypatch.setattr(granite_inproc, "_cgroup_memory_limit", lambda: 2_000_000_000)
    assert granite_inproc.model_id() == granite_inproc.MODEL_350M
    monkeypatch.setattr(granite_inproc, "_cgroup_memory_limit", lambda: 8_000_000_000)
    assert granite_inproc.model_id() == granite_inproc.MODEL_1B
    monkeypatch.setattr(granite_inproc, "_cgroup_memory_limit", lambda: None)
    assert granite_inproc.model_id() == granite_inproc.MODEL_1B


def test_hosting_and_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("GRANITE_HOSTING", raising=False)
    monkeypatch.delenv("GRANITE_CACHE", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    assert granite_inproc.hosting() == "off"
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    assert granite_inproc.hosting() == "inproc"
    assert granite_inproc.cache_root() == tmp_path / "models"
    monkeypatch.setenv("GRANITE_HOSTING", "off")
    assert granite_inproc.hosting() == "off"
    monkeypatch.setenv("GRANITE_HOSTING", "inproc")
    assert granite_inproc.hosting() == "inproc"


def test_disabled_granite_is_never_ready(monkeypatch):
    monkeypatch.setenv("GRANITE_HOSTING", "off")
    granite_inproc._reset_for_tests()
    assert _REAL_CHAIN_READY() is False
    assert granite_inproc.start_preload() is False
    with pytest.raises(granite_inproc.GraniteUnavailable):
        granite_inproc.generate(MSGS)
