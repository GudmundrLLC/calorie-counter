"""LLM routing for Calorie Counter (F2026-10-12 LLM standing rule · GRANITE-FIRST).

Every model call goes through the Gudmundr Gateway OpenAI-compatible proxy
({GATEWAY_URL}/svc/llm/v1) when GATEWAY_SERVICE_TOKEN is set:

  text estimate  -> model "auto"            (Gateway chain · IBM Granite first)
  photo estimate -> GATEWAY_VISION_MODEL    (default "gemini:default" · a
                                             vision-capable provider pin)

Documented fallback (transition only): when the Gateway token is not set,
or the Gateway call fails, the app falls back to its original direct xAI
Grok call (XAI_API_KEY) so meal logging keeps working. Voice transcription
stays on xAI Whisper because the Gateway proxy has no audio endpoint yet
(Gateway-kit request: /svc/llm/v1/audio/transcriptions).

Never logs prompts, keys or tokens.
"""
from __future__ import annotations

import os

import openai as _openai

GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://gateway.gudmundr.com").rstrip("/")
GATEWAY_TEXT_MODEL = os.environ.get("GATEWAY_TEXT_MODEL", "auto")
GATEWAY_VISION_MODEL = os.environ.get("GATEWAY_VISION_MODEL", "gemini:default")

XAI_BASE = "https://api.x.ai/v1"
XAI_TEXT_MODEL = "grok-3"
XAI_VISION_MODEL = "grok-2-vision-1212"
XAI_WHISPER_MODEL = "whisper-large-v3"


def _gateway_token() -> str:
    return (os.environ.get("GATEWAY_SERVICE_TOKEN") or "").strip()


def _xai_key() -> str:
    return (os.environ.get("XAI_API_KEY") or "").strip()


def gateway_enabled() -> bool:
    return bool(GATEWAY_URL and _gateway_token())


def status() -> dict:
    """Non-secret routing status for /api/ai-status and the bottom bar."""
    return {
        "route": "gateway" if gateway_enabled() else ("xai-fallback" if _xai_key() else "none"),
        "text_model": GATEWAY_TEXT_MODEL if gateway_enabled() else XAI_TEXT_MODEL,
        "vision_model": GATEWAY_VISION_MODEL if gateway_enabled() else XAI_VISION_MODEL,
        "transcription": "xai-whisper" if _xai_key() else "unavailable",
    }


def _gateway_client():
    return _openai.OpenAI(base_url=f"{GATEWAY_URL}/svc/llm/v1", api_key=_gateway_token(), timeout=60)


def _xai_client():
    return _openai.OpenAI(base_url=XAI_BASE, api_key=_xai_key(), timeout=60)


def chat(messages: list[dict], *, vision: bool = False, max_tokens: int = 1024, temperature: float = 0.2) -> tuple[str, str]:
    """Return (text, route). Raises RuntimeError when no route is configured."""
    errors: list[str] = []
    if gateway_enabled():
        try:
            r = _gateway_client().chat.completions.create(
                model=GATEWAY_VISION_MODEL if vision else GATEWAY_TEXT_MODEL,
                messages=messages, max_tokens=max_tokens, temperature=temperature,
            )
            return r.choices[0].message.content or "", "gateway"
        except Exception as e:  # noqa: BLE001 · fall through to the documented fallback
            errors.append(f"gateway:{type(e).__name__}")
    if _xai_key():
        r = _xai_client().chat.completions.create(
            model=XAI_VISION_MODEL if vision else XAI_TEXT_MODEL,
            messages=messages, max_tokens=max_tokens, temperature=temperature,
        )
        return r.choices[0].message.content or "", "xai-fallback"
    raise RuntimeError("No AI route configured (" + ", ".join(errors or ["no GATEWAY_SERVICE_TOKEN or XAI_API_KEY"]) + ")")


def transcribe(audio_data: bytes, filename: str = "audio.webm") -> str:
    if not _xai_key():
        raise RuntimeError("Voice transcription is not configured")
    import io
    f = io.BytesIO(audio_data)
    f.name = filename
    r = _xai_client().audio.transcriptions.create(model=XAI_WHISPER_MODEL, file=f)
    return r.text
