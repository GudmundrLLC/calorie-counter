"""Voice transcription · RESIDUAL direct xAI call (F2026-11-23-GATEWAY-KEYPLANE).

This is the one place Meals still calls a provider directly. The Gudmundr
Gateway has no audio endpoint yet, and voice logging is a working feature,
so it stays until there is a replacement. Blocked on: Gateway
`POST /svc/llm/v1/audio/transcriptions`. When that ships, point this at the
Gateway with GATEWAY_SERVICE_TOKEN and delete XAI_API_KEY from the service.

Text and photo estimates never come here; they go through llm_client.
Never logs audio, keys or tokens.
"""
from __future__ import annotations

import io
import os

XAI_BASE = "https://api.x.ai/v1"
XAI_WHISPER_MODEL = "whisper-large-v3"


def _key() -> str:
    return (os.environ.get("XAI_API_KEY") or "").strip()


def configured() -> bool:
    return bool(_key())


def transcribe(audio_data: bytes, filename: str = "audio.webm") -> str:
    if not _key():
        raise RuntimeError("Voice transcription is not configured")
    import openai
    f = io.BytesIO(audio_data)
    f.name = filename
    client = openai.OpenAI(base_url=XAI_BASE, api_key=_key(), timeout=60)
    return client.audio.transcriptions.create(model=XAI_WHISPER_MODEL, file=f).text
