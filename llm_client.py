"""Meals AI client · the app's only path to a language model.

F2026-11-23-GATEWAY-KEYPLANE + F2026-11-22-GRANITE-FIRST. Same shape as
gud-plan `llm_client.py`, cut down to the two tiers this doctrine allows:

  1. in-app IBM Granite (granite_inproc · no key, no network at inference)
  2. Gudmundr Gateway  POST {GATEWAY_URL}/svc/llm/v1/chat/completions
     with model=auto (the Gateway walks Granite first, then keyed providers)
  3. AINotAvailable · the caller shows a graceful "AI not available"

No other tiers. The app holds one credential, GATEWAY_SERVICE_TOKEN; every
provider key lives on the Gateway.

Behaviour kept from gud-plan:
  · circuit breaker: 3 failures within 60 s shelve a tier for 5 min; a
    Gateway 401 (bad service token) shelves it at once. Success resets.
  · breaker-neutral skips: Granite still loading, its one slot busy, prompt
    too long, or an answer the caller's `validate` rejects → fall through
    without counting a failure.
  · streaming fails over only before the first token; after that the error
    is raised (the caller may already have flushed text).
  · callers pass `model_hint` ("default" | "fast"); the client resolves the
    real model name per tier.
  · vision requests skip in-app Granite (it has no image input) and go to the
    Gateway with GATEWAY_VISION_MODEL.

Never logs prompts, keys or tokens.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterator

import granite_inproc

log = logging.getLogger("llm_client")

TIER_GRANITE = "granite-inproc"
TIER_GATEWAY = "gateway"

_CB_WINDOW_S = 60
_CB_THRESHOLD = 3
_CB_COOLDOWN_S = 300
_REQUEST_TIMEOUT_S = 60.0

_cb_lock = threading.Lock()
_CB_FAILURES: dict[str, list[float]] = {}
_CB_SKIP_UNTIL: dict[str, float] = {}
_CB_REASON: dict[str, str] = {}


class AINotAvailable(RuntimeError):
    """Every tier failed or was skipped. `attempts` lists what was tried."""

    def __init__(self, msg: str, attempts: list[dict] | None = None):
        super().__init__(msg)
        self.attempts = attempts or []


class Rejected(ValueError):
    """The caller's `validate` refused a tier's answer (e.g. not valid JSON).
    Breaker-neutral: the tier is healthy, the next tier gets a try."""
    breaker_neutral = True


# ── config ─────────────────────────────────────────────────────────────

def gateway_url() -> str:
    return (os.environ.get("GATEWAY_URL") or "https://gateway.gudmundr.com").rstrip("/")


def _gateway_token() -> str:
    return (os.environ.get("GATEWAY_SERVICE_TOKEN") or "").strip()


def gateway_configured() -> bool:
    return bool(gateway_url() and _gateway_token())


def vision_model() -> str:
    return (os.environ.get("GATEWAY_VISION_MODEL") or "gemini:default").strip()


def _gateway_model(model_hint: str, vision: bool) -> str:
    if vision:
        return vision_model()
    if model_hint == "fast":
        return (os.environ.get("GATEWAY_MODEL_FAST") or "auto").strip()
    return (os.environ.get("GATEWAY_MODEL_DEFAULT") or "auto").strip()


# ── circuit breaker ────────────────────────────────────────────────────

def _http_status(exc: BaseException | None) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _breaker_neutral(exc: BaseException | None) -> bool:
    return bool(exc is not None and getattr(exc, "breaker_neutral", False))


def _cb_is_open(name: str) -> bool:
    with _cb_lock:
        return time.time() < _CB_SKIP_UNTIL.get(name, 0.0)


def _record_failure(name: str, exc: BaseException | None) -> None:
    if _breaker_neutral(exc):
        return
    now = time.time()
    with _cb_lock:
        if name == TIER_GATEWAY and _http_status(exc) == 401:
            _CB_SKIP_UNTIL[name] = now + _CB_COOLDOWN_S
            _CB_REASON[name] = "gateway rejected the service token (401)"
            log.warning("llm_client: gateway paused %ds (401)", _CB_COOLDOWN_S)
            return
        recent = [t for t in _CB_FAILURES.get(name, []) if now - t < _CB_WINDOW_S] + [now]
        _CB_FAILURES[name] = recent
        if len(recent) >= _CB_THRESHOLD:
            _CB_SKIP_UNTIL[name] = now + _CB_COOLDOWN_S
            _CB_REASON[name] = f"{len(recent)} failures in {_CB_WINDOW_S}s"
            log.warning("llm_client: %s circuit-broken for %ds", name, _CB_COOLDOWN_S)


def _record_success(name: str) -> None:
    with _cb_lock:
        _CB_FAILURES.pop(name, None)
        _CB_SKIP_UNTIL.pop(name, None)
        _CB_REASON.pop(name, None)


def _reset_for_tests() -> None:
    with _cb_lock:
        _CB_FAILURES.clear()
        _CB_SKIP_UNTIL.clear()
        _CB_REASON.clear()


# ── tiers ──────────────────────────────────────────────────────────────

def _tiers(vision: bool) -> list[str]:
    """Ordered tiers for this call (skips shelved and not-ready ones)."""
    out: list[str] = []
    if not vision and not _cb_is_open(TIER_GRANITE) and granite_inproc.chain_ready():
        out.append(TIER_GRANITE)
    if gateway_configured() and not _cb_is_open(TIER_GATEWAY):
        out.append(TIER_GATEWAY)
    return out


def _gateway_client():
    import openai
    return openai.OpenAI(base_url=f"{gateway_url()}/svc/llm/v1", api_key=_gateway_token(),
                         timeout=_REQUEST_TIMEOUT_S, max_retries=0)


def _served(headers) -> dict:
    try:
        return {"served_provider": (headers.get("x-gw-provider") or "").strip() or None,
                "served_model": (headers.get("x-gw-model") or "").strip() or None}
    except Exception:  # noqa: BLE001
        return {"served_provider": None, "served_model": None}


def _call_granite(messages, max_tokens, temperature) -> tuple[str, str, dict]:
    r = granite_inproc.generate(messages, max_tokens=max_tokens, temperature=temperature)
    model = granite_inproc.model_id()
    return (r.get("content") or "").strip(), model, {
        "served_provider": TIER_GRANITE, "served_model": model,
        "tokens_per_s": r.get("tokens_per_s")}


def _call_gateway(messages, model, max_tokens, temperature) -> tuple[str, str, dict]:
    raw = _gateway_client().chat.completions.with_raw_response.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
    resp = raw.parse()
    served = _served(raw.headers)
    content = (resp.choices[0].message.content or "").strip()
    return content, model, served


# ── public API ─────────────────────────────────────────────────────────

def chat_meta(messages: list[dict], *, model_hint: str = "default", max_tokens: int = 1024,
              temperature: float = 0.2, vision: bool = False,
              validate: Callable[[str], object] | None = None) -> dict:
    """Blocking completion through the chain. Returns
        {content, tier, model, served_provider, served_model, latency_ms,
         fallback_from, attempts, parsed}
    `validate(content)` may return a parsed value (kept as `parsed`) or raise;
    a raise moves on to the next tier without tripping the breaker.
    Raises AINotAvailable when every tier fails or none is available."""
    attempts: list[dict] = []
    for tier in _tiers(vision):
        t0 = time.time()
        try:
            if tier == TIER_GRANITE:
                content, model, served = _call_granite(messages, max_tokens, temperature)
            else:
                content, model, served = _call_gateway(
                    messages, _gateway_model(model_hint, vision), max_tokens, temperature)
            if not content:
                raise ValueError("empty content")
            parsed = None
            if validate is not None:
                try:
                    parsed = validate(content)
                except Exception as e:  # noqa: BLE001
                    raise Rejected(f"answer rejected: {type(e).__name__}") from e
            latency = int((time.time() - t0) * 1000)
            _record_success(tier)
            log.info("llm_client: %s answered model=%s latency=%dms", tier, model, latency)
            return {"content": content, "tier": tier, "model": model,
                    "served_provider": served.get("served_provider"),
                    "served_model": served.get("served_model"),
                    "latency_ms": latency,
                    "fallback_from": attempts[0]["tier"] if attempts else None,
                    "attempts": attempts, "parsed": parsed}
        except Exception as e:  # noqa: BLE001
            attempts.append({"tier": tier, "latency_ms": int((time.time() - t0) * 1000),
                             "error": type(e).__name__,
                             "skipped": _breaker_neutral(e)})
            log.warning("llm_client: %s failed (%s)", tier, type(e).__name__)
            _record_failure(tier, e)
    if not attempts:
        raise AINotAvailable("AI not available (no tier ready: in-app Granite is "
                             "loading or off, and GATEWAY_SERVICE_TOKEN is not set)")
    raise AINotAvailable("AI not available (every tier failed)", attempts)


def chat(messages: list[dict], **kw) -> str:
    return chat_meta(messages, **kw)["content"]


def chat_stream(messages: list[dict], *, model_hint: str = "default", max_tokens: int = 1024,
                temperature: float = 0.2, on_meta: Callable[[dict], None] | None = None
                ) -> Iterator[str]:
    """Streaming completion. Falls over to the next tier only before the
    first token; a mid-stream failure is raised."""
    attempts: list[dict] = []
    for tier in _tiers(False):
        t0 = time.time()
        yielded = False
        try:
            if tier == TIER_GRANITE:
                model = granite_inproc.model_id()
                it = granite_inproc.generate_stream(messages, max_tokens=max_tokens)
            else:
                model = _gateway_model(model_hint, False)
                stream = _gateway_client().chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens,
                    temperature=temperature, stream=True)
                it = (c.choices[0].delta.content for c in stream
                      if c.choices and c.choices[0].delta.content)
            if on_meta:
                try:
                    on_meta({"tier": tier, "model": model, "attempts": list(attempts)})
                except Exception:  # noqa: BLE001
                    pass
            for tok in it:
                if tok:
                    yielded = True
                    yield tok
            if not yielded:
                raise ValueError("stream yielded no content")
            _record_success(tier)
            return
        except Exception as e:  # noqa: BLE001
            attempts.append({"tier": tier, "latency_ms": int((time.time() - t0) * 1000),
                             "error": type(e).__name__, "skipped": _breaker_neutral(e)})
            _record_failure(tier, e)
            if yielded:
                raise
    raise AINotAvailable("AI not available", attempts)


def status() -> dict:
    """Non-secret routing status (for /api/ai/status and the page context)."""
    g = granite_inproc.status()
    now = time.time()
    with _cb_lock:
        shelved = {k: {"until_s": int(v - now), "reason": _CB_REASON.get(k, "")}
                   for k, v in _CB_SKIP_UNTIL.items() if v > now}
    tiers = []
    if g.get("enabled"):
        tiers.append(TIER_GRANITE)
    if gateway_configured():
        tiers.append(TIER_GATEWAY)
    return {
        "chain": [TIER_GRANITE, TIER_GATEWAY],
        "available_tiers": tiers,
        "route": ("granite+gateway" if len(tiers) == 2 else tiers[0] if tiers else "none"),
        "granite": {k: g.get(k) for k in (
            "status", "detail", "progress", "error", "enabled", "hosting", "model", "dtype",
            "cache", "busy", "chain_eligible", "chain_reason", "bench_tokens_per_s",
            "last_tokens_per_s", "generations", "load_seconds", "loaded_at",
            "memory_limit_bytes")},
        "granite_runtime_missing": granite_inproc.runtime_missing(),
        "gateway": {"url": gateway_url(), "configured": gateway_configured(),
                    "text_model": _gateway_model("default", False),
                    "vision_model": vision_model()},
        "shelved": shelved,
    }
