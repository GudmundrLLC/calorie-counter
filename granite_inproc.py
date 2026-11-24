"""IBM Granite running INSIDE the Meals process · tier 1 of the AI chain.

F2026-11-23-GATEWAY-KEYPLANE rule 1 + F2026-11-22-GRANITE-FIRST: Meals has a
minimum level of AI out of the box, with no key and no network at inference.
Ported from the Gateway's `app/granite_inproc.py` (commit bca3f72), itself the
port of gud-plan `services/granite_inproc.py` (pilot 408) and CCT_Research
`src/granite.js`: IBM Granite 4.0 (Apache-2.0), onnx-community ONNX export,
CPU via ONNX Runtime + `tokenizers`. The only network traffic is the one-time
weight download from Hugging Face to the Railway volume.

Meals wiring (llm_client.py):
  · `hosting() == "inproc"` turns this module on (GRANITE_HOSTING=inproc, or
    unset with a mounted Railway volume)
  · llm_client asks Granite first only once it is loaded and benchmarked fast
    enough (`chain_ready()`); while downloading/loading it is skipped (0 ms)
  · busy / prompt-too-long / not-ready raise GraniteUnavailable
    (breaker_neutral) → llm_client falls through to the Gateway without
    counting a Granite failure; GraniteTimeout counts as a real failure

Behaviour (mirrors CCT / gud-plan):
  · weights cached at $GRANITE_CACHE, else $RAILWAY_VOLUME_MOUNT_PATH/models,
    else <repo>/.cache/models (gitignored) · never in git or the image
  · resumable download (HTTP Range) with size + SHA-256 integrity check
    (Hugging Face publishes the LFS SHA-256 as X-Linked-Etag); a verified
    manifest lets restarts skip the network entirely
  · background preload at startup (never blocks /healthz)
  · single-flight generation (CPU-bound); a caller that would wait longer
    than GRANITE_QUEUE_WAIT_S gets GraniteBusy so llm_client falls through
  · status(): idle → downloading (progress %) → loading → ready | error

Env:
  GRANITE_HOSTING          inproc turns this on · anything else turns it off ·
                           unset → inproc when a Railway volume is mounted
  GRANITE_MODEL            default onnx-community/granite-4.0-1b-ONNX-web;
                           "small" or "350m" → onnx-community/granite-4.0-350m-ONNX-web
                           (auto-picked when unset and the container has < 3.5 GB)
  GRANITE_DTYPE            q4 (default) | q4f16 | fp16 | fp32
  GRANITE_PRELOAD          default on · 0/false → load on first request
  GRANITE_CACHE            weight directory override
  GRANITE_THREADS          ONNX Runtime intra-op threads (default: cgroup CPU
                           quota / 2, capped at 8)
  GRANITE_MAX_NEW_TOKENS   hard cap on generated tokens (default 768)
  GRANITE_MAX_INPUT_TOKENS longer prompts fall through to the chain (default 6144)
  GRANITE_QUEUE_WAIT_S     max wait for the single generation slot (default 3)
  GRANITE_MIN_TOKENS_PER_S chain-first only when the startup benchmark decodes at
                           least this fast (default 4; CPUs without AVX2 run the
                           q4 kernels ~0.5 tok/s and are skipped, not slowed)
  GRANITE_MAX_SECONDS      per-call wall clock; a blocking call that runs past it
                           raises so the chain falls through (default 60)
  HF_ENDPOINT              Hugging Face mirror (default https://huggingface.co)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import types
import hashlib
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger("granite_inproc")

PROVIDER_NAME = "granite-inproc"   # llm_client tier name (gud-plan pilot 308c parity)
MODEL_1B = "onnx-community/granite-4.0-1b-ONNX-web"
MODEL_350M = "onnx-community/granite-4.0-350m-ONNX-web"
_LOW_MEMORY_BYTES = 3_500_000_000
_PREFILL_CHUNK = 256          # keeps the prefill logits tensor ~100 MB
_REPETITION_PENALTY = 1.05    # CCT parity (do_sample=False, repetition_penalty=1.05)
_REPO_ROOT = Path(__file__).resolve().parent
_SMALL_FILES = ("config.json", "generation_config.json", "special_tokens_map.json",
                "tokenizer.json", "tokenizer_config.json", "chat_template.jinja")
_OPTIONAL_FILES = {"special_tokens_map.json", "chat_template.jinja"}


class GraniteUnavailable(RuntimeError):
    """Granite is disabled, still downloading/loading, or failed to load."""
    # Pilot 458 · not-eligible-for-this-request (prompt too long, loading,
    # busy) is not a provider failure · the walker skips the Granite cooldown.
    breaker_neutral = True


class GraniteTimeout(GraniteUnavailable):
    """A generation ran past GRANITE_MAX_SECONDS · a real slowness signal."""
    breaker_neutral = False


class GraniteBusy(GraniteUnavailable):
    """The single generation slot stayed taken past GRANITE_QUEUE_WAIT_S."""


# ── config ─────────────────────────────────────────────────────────────

def _truthy(v: str | None, default: bool) -> bool:
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def hosting() -> str:
    """"inproc" or "off". GRANITE_HOSTING wins; unset → inproc when a Railway
    volume is mounted (somewhere to keep the weights), off elsewhere."""
    raw = (os.environ.get("GRANITE_HOSTING") or "").strip().lower()
    if raw:
        return "inproc" if raw == "inproc" else "off"
    return "inproc" if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") else "off"


_RUNTIME_MISSING: list = []


def runtime_missing() -> str | None:
    """None when numpy + onnxruntime + tokenizers import, else the reason
    (cached · e.g. Intel macOS has no onnxruntime>=1.20 wheel)."""
    if not _RUNTIME_MISSING:
        try:
            import numpy, onnxruntime, tokenizers  # noqa: F401
            _RUNTIME_MISSING.append(None)
        except Exception as e:  # noqa: BLE001
            _RUNTIME_MISSING.append(f"inproc runtime missing: {type(e).__name__}: {e}"[:160])
    return _RUNTIME_MISSING[0]


def enabled() -> bool:
    return hosting() == "inproc"


def _cgroup_memory_limit() -> int | None:
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(p).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            return int(raw)
    return None


def _cgroup_cpus() -> float | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    return None


def model_id() -> str:
    raw = (os.environ.get("GRANITE_MODEL") or "").strip()
    if raw.lower() in {"small", "350m"}:
        return MODEL_350M
    if raw:
        return raw
    mem = _cgroup_memory_limit()
    return MODEL_350M if mem is not None and mem < _LOW_MEMORY_BYTES else MODEL_1B


def dtype() -> str:
    return (os.environ.get("GRANITE_DTYPE") or "q4").strip().lower()


def cache_root() -> Path:
    if os.environ.get("GRANITE_CACHE"):
        return Path(os.environ["GRANITE_CACHE"]).expanduser().resolve()
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if vol:
        return Path(vol) / "models"
    return _REPO_ROOT / ".cache" / "models"


def _threads() -> int:
    n = _int_env("GRANITE_THREADS", 0)
    if n > 0:
        return n
    cpus = _cgroup_cpus() or float(os.cpu_count() or 2)
    return max(1, min(8, int(cpus // 2)))


def _hf() -> str:
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


# ── state ──────────────────────────────────────────────────────────────

_state: dict = {"status": "idle", "detail": "", "progress": 0, "error": None,
                "loaded_at": None, "load_seconds": None, "generations": 0,
                "last_tokens_per_s": None, "bench_tokens_per_s": None}
_state_lock = threading.Lock()
_load_lock = threading.Lock()
_gen_lock = threading.Lock()
_loader: threading.Thread | None = None
_engine: "_Engine | None" = None


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def status() -> dict:
    with _state_lock:
        s = dict(_state)
    ok, why = chain_eligible()
    s.update(chain_eligible=bool(ok and _engine is not None), chain_reason=why)
    s.update(hosting=hosting(), enabled=enabled(), provider=PROVIDER_NAME,
             model=model_id(), dtype=dtype(), cache=str(cache_root()),
             busy=_gen_lock.locked(),
             label="IBM Granite (runs inside Meals)",
             memory_limit_bytes=_cgroup_memory_limit())
    return s


def is_ready() -> bool:
    """llm_inproc readiness probe (pilot 308c contract): enabled, loaded and
    fast enough to go first. Never blocks; a cold model starts loading."""
    return chain_ready()


# ── download ───────────────────────────────────────────────────────────

def _suffix() -> str:
    return "" if dtype() == "fp32" else f"_{dtype()}"


def _model_dir() -> Path:
    return cache_root() / model_id()


def _manifest_path() -> Path:
    return _model_dir() / ".granite_manifest.json"


def _read_manifest() -> dict:
    try:
        return json.loads(_manifest_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_manifest(m: dict) -> None:
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
    tmp.replace(p)


def _head(client, url: str) -> tuple[int | None, str | None]:
    """(size, sha256) for a Hub file. LFS files expose both via X-Linked-*."""
    r = client.head(url, follow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308) or r.status_code == 200:
        size = r.headers.get("x-linked-size") or (
            r.headers.get("content-length") if r.status_code == 200 else None)
        etag = (r.headers.get("x-linked-etag") or "").strip('"').lower()
        if r.status_code != 200 and not size:
            r2 = client.head(url, follow_redirects=True)
            r2.raise_for_status()
            size = r2.headers.get("content-length")
        sha = etag if re.fullmatch(r"[0-9a-f]{64}", etag or "") else None
        return (int(size) if size and str(size).isdigit() else None), sha
    r.raise_for_status()
    return None, None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(client, rel: str, on_bytes: Callable[[int, int | None], None] | None = None,
              attempts: int = 40) -> dict:
    """Resumable, integrity-checked download of one Hub file. Returns its
    manifest entry {size, sha256}."""
    url = f"{_hf()}/{model_id()}/resolve/main/{rel}"
    dest = _model_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    total, sha = _head(client, url)
    for attempt in range(1, attempts + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if total and have > total:
            dest.unlink()
            have = 0
        try:
            if not (total and have == total):
                headers = {"Range": f"bytes={have}-"} if have else {}
                with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                    if r.status_code == 416:
                        pass
                    else:
                        r.raise_for_status()
                        append = have > 0 and r.status_code == 206
                        written = have if append else 0
                        with open(dest, "ab" if append else "wb") as out:
                            for chunk in r.iter_bytes(1 << 20):
                                out.write(chunk)
                                written += len(chunk)
                                if on_bytes:
                                    on_bytes(written, total)
            size = dest.stat().st_size
            if total and size != total:
                raise IOError(f"short file {size}/{total}")
            if sha:
                got = _sha256(dest)
                if got != sha:
                    dest.unlink()
                    raise IOError(f"sha256 mismatch for {rel}")
            return {"size": size, "sha256": sha}
        except Exception as e:  # network blip, short read, bad hash → retry
            _set(detail=f"retrying {rel} ({e.__class__.__name__}: {str(e)[:120]})")
            log.warning("granite_inproc: download %s attempt %d failed: %s", rel, attempt, e)
            time.sleep(min(30.0, 1.0 * attempt))
    raise IOError(f"could not download {rel}")


def _onnx_files() -> list[str]:
    """Graph + external-data files for the chosen dtype, from config.json's
    transformers.js_config.use_external_data_format (fp32 is split in 4)."""
    name = f"model{_suffix()}.onnx"
    n = 1
    try:
        cfg = json.loads((_model_dir() / "config.json").read_text())
        n = int(((cfg.get("transformers.js_config") or {})
                 .get("use_external_data_format") or {}).get(name, 1) or 1)
    except (OSError, ValueError, TypeError):
        pass
    files = [f"onnx/{name}", f"onnx/{name}_data"]
    files += [f"onnx/{name}_data_{i}" for i in range(1, n)]
    return files


def _manifest_complete(m: dict, files: list[str]) -> bool:
    d = _model_dir()
    for rel in files:
        e = m.get(rel)
        if not e or not (d / rel).exists() or (d / rel).stat().st_size != e.get("size"):
            return False
    return True


def ensure_files() -> Path:
    """Download (or verify) every file the model needs. Offline-safe once a
    verified manifest exists."""
    import httpx
    manifest = _read_manifest()
    small_ok = all((_model_dir() / f).exists() for f in _SMALL_FILES if f not in _OPTIONAL_FILES)
    if small_ok and _manifest_complete(manifest, _onnx_files()):
        return _model_dir()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0),
                      headers={"user-agent": "granite-inproc/1"}) as client:
        for rel in _SMALL_FILES:
            try:
                manifest[rel] = _download(client, rel, attempts=5)
            except Exception:
                if rel in _OPTIONAL_FILES:
                    continue
                raise
        _write_manifest(manifest)
        for rel in _onnx_files():
            def _progress(n: int, total: int | None, _rel=rel) -> None:
                pct = round(n * 100 / total) if total else 0
                _set(progress=pct, detail=f"downloading {_rel} — {n / 1e9:.2f}"
                     + (f" / {total / 1e9:.2f}" if total else "") + " GB")
            manifest[rel] = _download(client, rel, on_bytes=_progress)
            _write_manifest(manifest)
    return _model_dir()


# ── engine ─────────────────────────────────────────────────────────────

class _Engine:
    """Tokenizer + chat template + ONNX session with a greedy KV-cache loop."""

    def __init__(self, model_dir: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        cfg = json.loads((model_dir / "config.json").read_text())
        tok_cfg = json.loads((model_dir / "tokenizer_config.json").read_text())
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tmpl_path = model_dir / "chat_template.jinja"
        tmpl = tmpl_path.read_text() if tmpl_path.exists() else tok_cfg.get("chat_template")
        if not tmpl:
            raise GraniteUnavailable("model has no chat template")
        self.template = _compile_template(tmpl)
        self.special = {k: tok_cfg.get(k) for k in ("bos_token", "eos_token", "pad_token", "unk_token")}
        eos = cfg.get("eos_token_id")
        self.stop_ids = set(eos if isinstance(eos, list) else [eos]) - {None}
        eot = self.tokenizer.token_to_id("<|end_of_text|>")
        if eot is not None:
            self.stop_ids.add(eot)
        so = ort.SessionOptions()
        so.intra_op_num_threads = _threads()
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(model_dir / f"onnx/model{_suffix()}.onnx"),
                                            sess_options=so, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.output_names = [o.name for o in self.session.get_outputs()]
        kv = [i for i in self.session.get_inputs() if i.name.startswith("past_key_values.")]
        self.kv_inputs = [i.name for i in kv]
        self.kv_shape = (cfg.get("num_key_value_heads") or kv[0].shape[1],
                         (cfg.get("hidden_size") // cfg.get("num_attention_heads"))
                         if cfg.get("hidden_size") else kv[0].shape[3])
        self.kv_dtype = np.float16 if kv and "float16" in kv[0].type else np.float32

    # prompt → ids
    def encode_chat(self, messages: list[dict]) -> list[int]:
        text = self.template.render(messages=_normalize_messages(messages),
                                    add_generation_prompt=True, **self.special)
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def decode(self, ids: list[int]) -> str:
        return self.tokenizer.decode(ids, skip_special_tokens=True)

    def _run(self, ids: list[int], total: int, past: dict):
        np = self.np
        feeds = {"input_ids": np.asarray([ids], dtype=np.int64),
                 "attention_mask": np.ones((1, total), dtype=np.int64), **past}
        if "position_ids" in self.input_names:
            feeds["position_ids"] = np.arange(total - len(ids), total, dtype=np.int64)[None, :]
        outs = self.session.run(None, feeds)
        new_past = {}
        for name, val in zip(self.output_names, outs):
            if name.startswith("present."):
                new_past["past_key_values." + name[len("present."):]] = val
        return outs[0][0, -1], new_past

    def generate(self, ids: list[int], max_new_tokens: int) -> Iterator[int]:
        np = self.np
        h, d = self.kv_shape
        past = {n: np.zeros((1, h, 0, d), dtype=self.kv_dtype) for n in self.kv_inputs}
        total = 0
        logits = None
        for i in range(0, len(ids), _PREFILL_CHUNK):
            chunk = ids[i:i + _PREFILL_CHUNK]
            total += len(chunk)
            logits, past = self._run(chunk, total, past)
        seen = np.zeros(logits.shape[-1], dtype=bool)
        seen[np.asarray(ids, dtype=np.int64)] = True
        for _ in range(max_new_tokens):
            scores = logits.astype(np.float32, copy=True)
            # HF RepetitionPenaltyLogitsProcessor semantics
            s = scores[seen]
            scores[seen] = np.where(s < 0, s * _REPETITION_PENALTY, s / _REPETITION_PENALTY)
            nxt = int(scores.argmax())
            if nxt in self.stop_ids:
                return
            yield nxt
            seen[nxt] = True
            total += 1
            logits, past = self._run([nxt], total, past)


def _compile_template(source: str):
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    def _raise(msg):
        raise GraniteUnavailable(f"chat template: {msg}")

    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    env.filters["tojson"] = lambda x, indent=None, ensure_ascii=False: json.dumps(
        x, indent=indent, ensure_ascii=ensure_ascii)
    env.globals["raise_exception"] = _raise
    env.globals["strftime_now"] = lambda fmt: time.strftime(fmt)
    return env.from_string(source)


def _normalize_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages or []:
        role = (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) or "user"
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role == "developer":
            role = "system"
        out.append({"role": role, "content": content if content is not None else ""})
    return out


def _clean(text: str) -> str:
    """CCT parity: drop small-model artefacts like a lone "[Bullet 1]" line."""
    text = re.sub(r"(?im)^\s*\[(bullet|point)\s*\d+\]\s*$", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── lifecycle ──────────────────────────────────────────────────────────

def load() -> "_Engine":
    """Download + load synchronously (idempotent, thread-safe)."""
    global _engine
    if _engine is not None:
        return _engine
    with _load_lock:
        if _engine is not None:
            return _engine
        try:
            _set(status="downloading", error=None, progress=0, detail="checking weights")
            d = ensure_files()
            _set(status="loading", progress=100, detail="loading model into memory")
            t0 = time.time()
            eng = _Engine(d)
            secs = round(time.time() - t0, 1)
            bench = _benchmark(eng)
            _engine = eng
            _set(status="ready", detail=f"loaded in {secs} s · {bench} tok/s",
                 load_seconds=secs, bench_tokens_per_s=bench,
                 loaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            log.warning("granite_inproc: ready %s (%s) in %ss · threads=%d",
                        model_id(), dtype(), secs, _threads())
            print(f"[granite] ready: {model_id()} ({dtype()}) in {secs}s", flush=True)
            return eng
        except Exception as e:
            _set(status="error", error=f"{e.__class__.__name__}: {e}"[:400], detail="")
            log.error("granite_inproc: load failed: %s", e)
            print(f"[granite] load failed: {e}", flush=True)
            raise


def _benchmark(eng: "_Engine") -> float | None:
    """Decode speed (tokens/s, prefill excluded) on a tiny prompt. Decides
    whether Granite is fast enough to sit FIRST in the chain."""
    try:
        ids = eng.encode_chat([{"role": "user", "content": "Count from one to twenty in words."}])
        it = eng.generate(ids, 17)
        next(it)  # prefill + first token
        t0, n = time.time(), 0
        for _ in it:
            n += 1
        secs = time.time() - t0
        return round(n / secs, 2) if n and secs > 0 else None
    except Exception as e:  # benchmark never blocks readiness
        log.warning("granite_inproc: benchmark failed: %s", e)
        return None


def chain_eligible() -> tuple[bool, str]:
    """(eligible, reason) for chain-first placement once loaded."""
    with _state_lock:
        bench = _state.get("bench_tokens_per_s")
    floor = float(os.environ.get("GRANITE_MIN_TOKENS_PER_S", "4") or 4)
    if bench is not None and bench < floor:
        return False, f"too slow for chain-first: {bench} tok/s < {floor}"
    return True, "ok"


def start_preload(force: bool = False) -> bool:
    """Kick a background download+load. Returns True when a thread started.
    Never blocks: safe to call from FastAPI lifespan."""
    global _loader
    if not enabled() or _engine is not None:
        return False
    if not force and not _truthy(os.environ.get("GRANITE_PRELOAD"), True):
        return False
    if _loader is not None and _loader.is_alive():
        return False

    def _bg():
        try:
            load()
        except Exception:
            pass

    _loader = threading.Thread(target=_bg, name="granite-preload", daemon=True)
    _loader.start()
    return True


def chain_ready() -> bool:
    """True when llm_client should put Granite first right now. While
    disabled/downloading/loading/errored it is simply skipped (0 ms). With
    GRANITE_PRELOAD off, the first ask starts the load (CCT behaviour)."""
    if not enabled():
        return False
    if _engine is not None:
        return chain_eligible()[0]
    with _state_lock:
        st = _state["status"]
    if st in ("idle", "error") and (_loader is None or not _loader.is_alive()):
        # error → retry at most every 10 min
        if st == "idle" or time.time() - _last_retry[0] > 600:
            _last_retry[0] = time.time()
            start_preload(force=True)
    return False


_last_retry = [0.0]


# ── generation ─────────────────────────────────────────────────────────

def _prepare(messages: list[dict], max_tokens: int | None):
    if not enabled():
        raise GraniteUnavailable("GRANITE_HOSTING is not inproc")
    eng = _engine
    if eng is None:
        raise GraniteUnavailable(f"granite not ready ({_state['status']})")
    ids = eng.encode_chat(messages)
    limit = _int_env("GRANITE_MAX_INPUT_TOKENS", 6144)
    if len(ids) > limit:
        raise GraniteUnavailable(f"prompt {len(ids)} tokens > GRANITE_MAX_INPUT_TOKENS={limit}")
    cap = _int_env("GRANITE_MAX_NEW_TOKENS", 768)
    max_new = max(1, min(int(max_tokens or cap), cap))
    return eng, ids, max_new


def _acquire() -> None:
    wait = float(os.environ.get("GRANITE_QUEUE_WAIT_S", "3") or 3)
    if not _gen_lock.acquire(timeout=max(0.0, wait)):
        raise GraniteBusy(f"granite busy > {wait}s")


def _record(n_tokens: int, secs: float) -> None:
    with _state_lock:
        _state["generations"] += 1
        if secs > 0 and n_tokens:
            _state["last_tokens_per_s"] = round(n_tokens / secs, 2)


def generate(messages: list[dict], max_tokens: int | None = None, **_ignored) -> dict:
    """Blocking generation. Returns {content, prompt_tokens, completion_tokens,
    seconds, tokens_per_s}. Greedy decoding (CCT parity)."""
    eng, ids, max_new = _prepare(messages, max_tokens)
    _acquire()
    deadline = float(os.environ.get("GRANITE_MAX_SECONDS", "60") or 60)
    try:
        t0 = time.time()
        out: list[int] = []
        for tid in eng.generate(ids, max_new):
            out.append(tid)
            if time.time() - t0 > deadline:
                raise GraniteTimeout(f"granite exceeded GRANITE_MAX_SECONDS={deadline}")
        secs = time.time() - t0
    finally:
        _gen_lock.release()
    _record(len(out), secs)
    return {"content": _clean(eng.decode(out)), "prompt_tokens": len(ids),
            "completion_tokens": len(out), "seconds": round(secs, 3),
            "tokens_per_s": round(len(out) / secs, 2) if secs > 0 else None}


class _Stream:
    """Iterator of OpenAI-style stream chunks that owns the generation slot
    and releases it on exhaustion, close() or garbage collection."""

    def __init__(self, eng: "_Engine", ids: list[int], max_new: int, model: str):
        self._eng, self._ids, self._max_new, self._model = eng, ids, max_new, model
        self._held = True
        self._it = self._gen()

    def _release(self):
        if self._held:
            self._held = False
            _gen_lock.release()

    def _gen(self):
        t0 = time.time()
        out: list[int] = []
        emitted = ""
        deadline = float(os.environ.get("GRANITE_MAX_SECONDS", "60") or 60)
        try:
            for tid in self._eng.generate(self._ids, self._max_new):
                if not out:
                    _note_prefill(len(self._ids), time.time() - t0)
                if time.time() - t0 > deadline:
                    break  # mid-stream: end the answer rather than error
                out.append(tid)
                text = self._eng.decode(out)
                if text.endswith("�"):
                    continue  # partial multi-byte char · wait for the next token
                delta, emitted = text[len(emitted):], text
                if delta:
                    yield _chunk(delta, self._model)
        finally:
            _record(len(out), time.time() - t0)
            self._release()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self._it.close()
        self._release()

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass


def _chunk(text: str, model: str):
    delta = types.SimpleNamespace(content=text, role="assistant")
    return types.SimpleNamespace(model=model, choices=[types.SimpleNamespace(
        index=0, delta=delta, finish_reason=None)])


def _note_prefill(n_ids: int, secs: float) -> None:
    """Pilot 539 · learn this host's prefill throughput (prompt tokens/s to
    the first generated token) so interactive streams can be routed by the
    time-to-first-token they would cost, not just by the context window."""
    if n_ids < 64 or secs <= 0:
        return
    rate = n_ids / secs
    with _state_lock:
        prev = _state.get("prefill_tokens_per_s")
        _state["prefill_tokens_per_s"] = round(rate if not prev else 0.7 * prev + 0.3 * rate, 1)


def stream_prefill_check(n_ids: int) -> tuple[bool, str]:
    """(ok, reason) · would this prompt's prefill keep an interactive stream
    silent past GRANITE_STREAM_MAX_PREFILL_S? Uses the learned prefill rate
    once known, else the GRANITE_STREAM_MAX_INPUT_TOKENS cap."""
    budget_s = float(os.environ.get("GRANITE_STREAM_MAX_PREFILL_S", "8") or 8)
    with _state_lock:
        rate = _state.get("prefill_tokens_per_s")
    if rate:
        est = n_ids / float(rate)
        if est > budget_s:
            return False, (f"prompt {n_ids} tokens ≈ {est:.0f}s prefill at {rate} tok/s "
                           f"> GRANITE_STREAM_MAX_PREFILL_S={budget_s:g}")
        return True, "ok"
    cap = _int_env("GRANITE_STREAM_MAX_INPUT_TOKENS", 1536)
    if n_ids > cap:
        return False, f"prompt {n_ids} tokens > GRANITE_STREAM_MAX_INPUT_TOKENS={cap}"
    return True, "ok"


def stream(messages: list[dict], max_tokens: int | None = None, **_ignored) -> _Stream:
    """Streaming generation. Readiness / size / busy errors raise HERE (before
    any chunk) so llm_client falls through cleanly.

    Pilot 539 · interactive streams also skip Granite (breaker-neutral, 0 ms)
    when the prompt's CPU prefill would leave the user staring at nothing:
    a 15.5k-char copilot prompt cost ~95 s end-to-end on Railway CPU."""
    eng, ids, max_new = _prepare(messages, max_tokens)
    ok, why = stream_prefill_check(len(ids))
    if not ok:
        raise GraniteUnavailable(why)
    _acquire()
    return _Stream(eng, ids, max_new, model_id())


def generate_stream(messages: list[dict], max_tokens: int | None = None, **_ignored):
    """llm_inproc stream contract: iterator of text deltas. Readiness, size
    and busy checks raise here, before the first delta."""
    s = stream(messages, max_tokens)
    return (c.choices[0].delta.content for c in s)


# ── OpenAI-compatible shim (direct use · llm_client calls generate/stream) ──

class _Completions:
    def create(self, model=None, messages=None, max_tokens=None, temperature=None,
               stream=False, **_ignored):
        if stream:
            return globals()["stream"](messages or [], max_tokens)
        r = generate(messages or [], max_tokens)
        msg = types.SimpleNamespace(content=r["content"], role="assistant")
        usage = types.SimpleNamespace(prompt_tokens=r["prompt_tokens"],
                                      completion_tokens=r["completion_tokens"],
                                      total_tokens=r["prompt_tokens"] + r["completion_tokens"])
        return types.SimpleNamespace(model=model_id(), usage=usage, choices=[
            types.SimpleNamespace(index=0, message=msg, finish_reason="stop")])


def openai_compat_client():
    """Object exposing `.chat.completions.create(...)` like the OpenAI SDK."""
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


def _reset_for_tests() -> None:
    global _engine, _loader
    _engine = None
    _loader = None
    _last_retry[0] = 0.0
    _set(status="idle", detail="", progress=0, error=None, loaded_at=None,
         load_seconds=None, generations=0, last_tokens_per_s=None,
         bench_tokens_per_s=None)
