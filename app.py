"""Calorie & Blood Sugar Tracker — text, voice, photo input via xAI Grok."""

import os
import io
import json
import sqlite3
import base64
import uuid
import secrets
from datetime import datetime, date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader
import openai as _openai
import uvicorn

from auth import oauth, setup_oauth, is_oauth_configured, BASE_URL


# ── Config ──────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent
_VOLUME = Path("/app/data")
DB_DIR = _VOLUME if _VOLUME.exists() else APP_DIR
DB_PATH = DB_DIR / "calories.db"
UPLOAD_DIR = DB_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "") or secrets.token_hex(32)
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
DEPLOY_MODE = os.environ.get("DEPLOY_MODE", "local")

FA_BASE_URL = os.environ.get("FA_BASE_URL", "https://family-alignment-production-d237.up.railway.app").rstrip("/")
FA_SYNC_TOKEN = os.environ.get("FA_SYNC_TOKEN", "")

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE = "https://api.x.ai/v1"
XAI_TEXT_MODEL = "grok-3"
XAI_VISION_MODEL = "grok-2-vision-1212"
XAI_WHISPER_MODEL = "whisper-large-v3"

app = FastAPI(title="Calorie & Blood Sugar Tracker", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

_jinja = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")), autoescape=True)


# ── Auth helpers ────────────────────────────────────────────────────────

_PUBLIC_PATHS = {"/login", "/logout", "/health", "/healthz", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/auth/", "/health", "/static/")


def _current_user(request: Request) -> dict | None:
    email = request.session.get("user_email")
    if not email:
        if not is_oauth_configured() or "localhost" in BASE_URL or "127.0.0.1" in BASE_URL:
            return {"email": "local@dev", "name": "Local Dev", "picture_url": None}
        return None
    # Verify against allowed list
    if ADMIN_EMAILS and email.lower() not in ADMIN_EMAILS:
        conn = get_db()
        row = conn.execute("SELECT id FROM allowed_emails WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        conn.close()
        if not row:
            request.session.clear()
            return None
    return {
        "email": email,
        "name": request.session.get("user_name", email),
        "picture_url": request.session.get("user_picture"),
    }


def _require_auth(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


# ── Database ────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    # Phase 1: base tables. Column additions for `entries` happen in Phase 2
    # (before any index referencing those columns is created).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id          TEXT PRIMARY KEY,
            user_email  TEXT NOT NULL DEFAULT 'local@dev',
            input_type  TEXT NOT NULL CHECK(input_type IN ('text','voice','photo')),
            raw_input   TEXT,
            photo_filename TEXT,
            items       TEXT NOT NULL DEFAULT '[]',
            total_calories REAL NOT NULL DEFAULT 0,
            total_protein_g REAL DEFAULT 0,
            total_carbs_g REAL DEFAULT 0,
            total_fat_g REAL DEFAULT 0,
            total_fiber_g REAL DEFAULT 0,
            confidence  TEXT DEFAULT 'medium',
            meal_type   TEXT DEFAULT 'snack',
            notes       TEXT DEFAULT '',
            entry_date  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
        CREATE INDEX IF NOT EXISTS idx_entries_user ON entries(user_email);

        CREATE TABLE IF NOT EXISTS blood_sugar (
            id          TEXT PRIMARY KEY,
            user_email  TEXT NOT NULL DEFAULT 'local@dev',
            value       REAL NOT NULL,
            unit        TEXT NOT NULL DEFAULT 'mg/dL',
            context     TEXT DEFAULT 'fasting',
            notes       TEXT DEFAULT '',
            measured_at TEXT NOT NULL,
            entry_date  TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bs_date ON blood_sugar(entry_date);
        CREATE INDEX IF NOT EXISTS idx_bs_user ON blood_sugar(user_email);

        CREATE TABLE IF NOT EXISTS allowed_emails (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS journal_entries (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            source_key     TEXT NOT NULL,
            user_email     TEXT,
            member_name    TEXT,
            entry_date     TEXT,
            content        TEXT,
            mood           TEXT,
            tags           TEXT,
            source_created_at TEXT,
            imported_at    TEXT NOT NULL,
            UNIQUE (source, source_key)
        );
        CREATE INDEX IF NOT EXISTS idx_journal_user ON journal_entries(user_email);
        CREATE INDEX IF NOT EXISTS idx_journal_date ON journal_entries(entry_date);

        CREATE TABLE IF NOT EXISTS journal_entry_email (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
            email      TEXT NOT NULL COLLATE NOCASE,
            UNIQUE (journal_id, email)
        );
        CREATE INDEX IF NOT EXISTS idx_journal_entry_email_email ON journal_entry_email(email);
    """)
    # Phase 2: ensure the source/source_key columns exist on `entries` — needed
    # both for fresh DBs (base CREATE TABLE omits them for simplicity) and for
    # existing DBs from before this migration. Column additions must happen
    # BEFORE any index that references them.
    for col_def in ("source TEXT NOT NULL DEFAULT ''",
                    "source_key TEXT NOT NULL DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Phase 3: indexes that depend on Phase-2 columns.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_source "
        "ON entries(source, source_key) WHERE source <> ''"
    )

    # Outbound sync queue for CYC → FA push (reliable, with retry).
    # A flusher thread claims rows via processing_until (stale-lock recovery
    # in case a worker dies mid-POST). completed_at NULL = still pending.
    # mirror_consumed_at is set by the local sync orchestrator when it has
    # durably copied the row into local SQLite — independent of live delivery.
    conn.execute("""CREATE TABLE IF NOT EXISTS outbound_sync (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        target            TEXT NOT NULL,
        endpoint_url      TEXT NOT NULL,
        payload_json      TEXT NOT NULL,
        source_ref        TEXT,
        attempts          INTEGER NOT NULL DEFAULT 0,
        next_attempt_at   TEXT NOT NULL DEFAULT (datetime('now')),
        processing_until  TEXT,
        last_error        TEXT,
        completed_at      TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    try:
        conn.execute("ALTER TABLE outbound_sync ADD COLUMN mirror_consumed_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_pending "
        "ON outbound_sync(next_attempt_at) WHERE completed_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_mirror "
        "ON outbound_sync(id) WHERE mirror_consumed_at IS NULL"
    )

    # Seed admin emails
    for em in ADMIN_EMAILS:
        conn.execute("INSERT OR IGNORE INTO allowed_emails (email) VALUES (?)", (em,))
    # Backfill journal_entry_email from journal_entries.user_email on first boot after the
    # multi-email migration. Runs only when the join table is empty and there are legacy rows.
    empty = conn.execute("SELECT 1 FROM journal_entry_email LIMIT 1").fetchone() is None
    if empty:
        conn.execute(
            """INSERT OR IGNORE INTO journal_entry_email (journal_id, email)
               SELECT id, user_email FROM journal_entries
               WHERE user_email IS NOT NULL AND user_email <> ''"""
        )
    conn.commit()
    conn.close()


init_db()


# ── xAI Grok client ────────────────────────────────────────────────────

def _xai_client():
    if not XAI_API_KEY:
        return None
    return _openai.OpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE)


CALORIE_SYSTEM = """You are a certified sports nutritionist with expertise in food portion estimation.
Analyze the food described and provide a detailed calorie breakdown.

Return ONLY valid JSON (no markdown fences, no explanation) with this structure:
{
  "items": [
    {"name": "food item", "quantity": "estimated quantity", "calories": 123,
     "protein_g": 10, "carbs_g": 20, "fat_g": 5, "fiber_g": 2}
  ],
  "total_calories": 456,
  "total_protein_g": 30,
  "total_carbs_g": 60,
  "total_fat_g": 15,
  "total_fiber_g": 6,
  "meal_type": "breakfast|lunch|dinner|snack",
  "confidence": "high|medium|low",
  "notes": "Brief note about assumptions"
}

Rules:
- Use USDA FoodData Central as primary reference
- Include macronutrient breakdown (protein, carbs, fat, fiber)
- Round all values to nearest whole number
- Infer meal_type from food type and time if not specified
- confidence: high = common foods, medium = reasonable estimate, low = vague description
- If quantity not specified, assume one standard serving
- For restaurant meals, estimate portions as typically served (larger than home)
- Account for cooking methods (fried adds calories, steamed doesn't)"""


def _time_context() -> str:
    return f"Current time: {datetime.now().strftime('%I:%M %p')}"


def _guess_meal() -> str:
    h = datetime.now().hour
    if 5 <= h < 11: return "breakfast"
    if 11 <= h < 15: return "lunch"
    if 15 <= h < 21: return "dinner"
    return "snack"


def _parse_response(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1]
    if t.endswith("```"):
        t = t[:-3].strip()
    return json.loads(t)


def _error_result(msg: str) -> dict:
    return {"items": [], "total_calories": 0, "total_protein_g": 0,
            "total_carbs_g": 0, "total_fat_g": 0, "total_fiber_g": 0,
            "confidence": "low", "meal_type": _guess_meal(), "notes": msg}


def estimate_text(description: str) -> dict:
    client = _xai_client()
    if not client:
        return _error_result("AI estimation requires XAI_API_KEY")
    try:
        resp = client.chat.completions.create(
            model=XAI_TEXT_MODEL, max_tokens=2048, temperature=0.2,
            messages=[
                {"role": "system", "content": CALORIE_SYSTEM},
                {"role": "user", "content": f"{_time_context()}\n\nFood: {description}"},
            ],
        )
        return _parse_response(resp.choices[0].message.content)
    except Exception as e:
        return _error_result(f"Estimation error: {e}")


def estimate_photo(image_data: bytes, media_type: str) -> dict:
    client = _xai_client()
    if not client:
        return _error_result("Photo analysis requires XAI_API_KEY")
    try:
        b64 = base64.standard_b64encode(image_data).decode()
        image_url = f"data:{media_type};base64,{b64}"
        resp = client.chat.completions.create(
            model=XAI_VISION_MODEL, max_tokens=2048, temperature=0.2,
            messages=[
                {"role": "system", "content": CALORIE_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": f"{_time_context()}\n\nIdentify all food items in this photo. Estimate portion sizes visually and calculate calories with macronutrient breakdown."},
                ]},
            ],
        )
        return _parse_response(resp.choices[0].message.content)
    except Exception as e:
        return _error_result(f"Photo analysis error: {e}")


def transcribe_audio(audio_data: bytes, filename: str = "audio.webm") -> str:
    client = _xai_client()
    if not client:
        return ""
    try:
        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename
        transcript = client.audio.transcriptions.create(
            model=XAI_WHISPER_MODEL, file=audio_file,
        )
        return transcript.text
    except Exception as e:
        print(f"[xAI] transcription error: {e}")
        return ""


# ── Helpers ─────────────────────────────────────────────────────────────

def _save_entry(user_email, input_type, raw_input, result, photo_filename=None):
    eid = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        INSERT INTO entries (id, user_email, input_type, raw_input, photo_filename, items,
            total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g,
            confidence, meal_type, notes, entry_date, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (eid, user_email, input_type, raw_input, photo_filename,
          json.dumps(result.get("items", [])),
          result.get("total_calories", 0),
          result.get("total_protein_g", 0),
          result.get("total_carbs_g", 0),
          result.get("total_fat_g", 0),
          result.get("total_fiber_g", 0),
          result.get("confidence", "medium"),
          result.get("meal_type", _guess_meal()),
          result.get("notes", ""), today, now, now))
    # Enqueue outbound push to FA. Backfilled fa_journal rows never come
    # through _save_entry(), so this path is only for user-created entries —
    # loop prevention is structural (source column stays '').
    _enqueue_cyc_entry_to_fa(conn, eid)
    conn.commit()
    conn.close()
    result.update(id=eid, entry_date=today, created_at=now,
                  raw_input=raw_input, input_type=input_type)
    return result


def _format_cyc_entry_for_fa_content(user_email: str, raw_input: str,
                                     items: list, total_calories: float,
                                     notes: str = "") -> str:
    """Build the enriched text a CYC calorie entry becomes when it lands in FA
    as a journal entry: the raw input plus a bulleted breakdown and total."""
    lines = [raw_input.strip() if raw_input else ""]
    lines.append("")
    if items:
        for it in items:
            name = it.get("name") or it.get("food") or "item"
            cal = it.get("calories", it.get("kcal", 0))
            qty = it.get("quantity") or it.get("portion") or ""
            qty_str = f" ({qty})" if qty else ""
            lines.append(f"- {name}{qty_str}: {cal} kcal")
    lines.append(f"Total: {int(round(total_calories))} kcal")
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines).strip()


def _enqueue_cyc_entry_to_fa(conn, entry_id: str) -> None:
    """Read the just-inserted entries row and enqueue a push to FA's
    /api/v1/sync/inbound-cyc-journal. Uses the same conn/transaction as the
    caller so enqueue is atomic with the entry write."""
    if not (FA_BASE_URL and FA_SYNC_TOKEN):
        return
    row = conn.execute(
        "SELECT id, user_email, raw_input, items, total_calories, notes, "
        "entry_date, created_at FROM entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if not row:
        return
    try:
        items = json.loads(row["items"] or "[]")
    except Exception:
        items = []
    content = _format_cyc_entry_for_fa_content(
        user_email=row["user_email"], raw_input=row["raw_input"],
        items=items, total_calories=row["total_calories"] or 0,
        notes=row["notes"] or "",
    )
    payload = {
        "cyc_entry_id": row["id"],
        "user_email": row["user_email"],
        "content": content,
        "entry_date": row["entry_date"],
        "entry_time": row["created_at"][11:16] if row["created_at"] else None,
        "tags": "cyc:calorie",
    }
    conn.execute(
        "INSERT INTO outbound_sync (target, endpoint_url, payload_json, source_ref) "
        "VALUES (?, ?, ?, ?)",
        ("fa", f"{FA_BASE_URL}/api/v1/sync/inbound-cyc-journal",
         json.dumps(payload), f"entries:{entry_id}"),
    )


def _flush_outbound_sync_once(max_jobs: int = 10) -> dict:
    """Claim up to max_jobs pending outbound rows and POST each. Multi-worker
    safe via processing_until (5-min lease; stale leases auto-reclaimed)."""
    import httpx
    conn = get_db()
    lease_until = (datetime.now() + timedelta(minutes=5)).isoformat()
    now_iso = datetime.now().isoformat()
    claimed = []
    # Race-safe claim: only rows past their next_attempt_at, not currently held.
    pending = conn.execute(
        "SELECT id FROM outbound_sync WHERE completed_at IS NULL "
        "AND next_attempt_at <= ? "
        "AND (processing_until IS NULL OR processing_until < ?) "
        "ORDER BY id ASC LIMIT ?",
        (now_iso, now_iso, max_jobs),
    ).fetchall()
    for r in pending:
        cur = conn.execute(
            "UPDATE outbound_sync SET processing_until = ? "
            "WHERE id = ? AND completed_at IS NULL "
            "AND (processing_until IS NULL OR processing_until < ?)",
            (lease_until, r["id"], now_iso),
        )
        if cur.rowcount == 1:
            claimed.append(r["id"])
    conn.commit()

    processed, failed = 0, 0
    for job_id in claimed:
        job = conn.execute(
            "SELECT endpoint_url, payload_json, attempts FROM outbound_sync WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not job:
            continue
        try:
            resp = httpx.post(
                job["endpoint_url"],
                headers={"Authorization": f"Bearer {FA_SYNC_TOKEN}",
                         "Content-Type": "application/json"},
                content=job["payload_json"],
                timeout=15.0,
            )
            if 200 <= resp.status_code < 300:
                conn.execute(
                    "UPDATE outbound_sync SET completed_at = ?, processing_until = NULL, "
                    "last_error = NULL WHERE id = ?",
                    (datetime.now().isoformat(), job_id),
                )
                processed += 1
            else:
                _outbound_backoff(conn, job_id, job["attempts"],
                                  f"HTTP {resp.status_code}: {resp.text[:200]}")
                failed += 1
        except Exception as e:
            _outbound_backoff(conn, job_id, job["attempts"], str(e)[:200])
            failed += 1
    conn.commit()
    conn.close()
    return {"claimed": len(claimed), "processed": processed, "failed": failed}


def _outbound_backoff(conn, job_id: int, prior_attempts: int, error: str) -> None:
    """Exponential backoff on failure: 30s, 2m, 8m, 32m, 2h, 8h, capped."""
    next_attempts = prior_attempts + 1
    delay_seconds = min(30 * (4 ** prior_attempts), 8 * 3600)
    next_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
    conn.execute(
        "UPDATE outbound_sync SET attempts = ?, next_attempt_at = ?, "
        "processing_until = NULL, last_error = ? WHERE id = ?",
        (next_attempts, next_at, error, job_id),
    )


def _row_to_dict(row):
    d = dict(row)
    if "items" in d:
        d["items"] = json.loads(d["items"])
    return d


# ── Routes: Auth ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    setup_oauth()
    _start_outbound_flusher()


def _start_outbound_flusher(interval_seconds: int = 30) -> None:
    """Kick off a background thread that flushes the outbound_sync queue every
    `interval_seconds`. Multi-worker safe (uvicorn workers each run one thread;
    row-level lease claiming prevents duplicate POSTs). Silent no-op if the
    FA target isn't configured."""
    if not (FA_BASE_URL and FA_SYNC_TOKEN):
        return
    import threading, time
    def _loop():
        while True:
            try:
                _flush_outbound_sync_once()
            except Exception as e:
                print(f"[outbound_flusher] error: {e}")
            time.sleep(interval_seconds)
    threading.Thread(target=_loop, daemon=True, name="outbound_flusher").start()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = _current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    error = request.query_params.get("error", "")
    tpl = _jinja.get_template("login.html")
    return HTMLResponse(tpl.render(error=error, oauth_configured=is_oauth_configured()))


@app.get("/auth/google/login")
async def google_login(request: Request):
    if not is_oauth_configured():
        return RedirectResponse("/login?error=oauth_not_configured", status_code=303)
    redirect_uri = BASE_URL.rstrip("/") + "/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    if not is_oauth_configured():
        return RedirectResponse("/login?error=oauth_not_configured", status_code=303)
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/login?error=oauth_failed", status_code=303)

    userinfo = token.get("userinfo", {})
    email = userinfo.get("email", "")
    if not email:
        return RedirectResponse("/login?error=oauth_failed", status_code=303)

    # Check whitelist — allow if table is empty (open access) or email is listed
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM allowed_emails").fetchone()["c"]
    if count > 0:
        allowed = conn.execute("SELECT id FROM allowed_emails WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if not allowed:
            conn.close()
            return RedirectResponse("/login?error=not_allowed", status_code=303)
    conn.close()

    request.session["user_email"] = email
    request.session["user_name"] = userinfo.get("name", email)
    request.session["user_picture"] = userinfo.get("picture", "")
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Routes: Pages ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    path = request.url.path
    if path not in _PUBLIC_PATHS and not any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        user = _current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
    tpl = _jinja.get_template("index.html")
    user = _current_user(request) or {}
    return HTMLResponse(tpl.render(user=user))


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tpl = _jinja.get_template("report.html")
    return HTMLResponse(tpl.render(user=user))


@app.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    rows = conn.execute(
        """SELECT je.entry_date, je.content, je.mood, je.tags
           FROM journal_entries je
           JOIN journal_entry_email jee ON jee.journal_id = je.id
           WHERE jee.email = ? COLLATE NOCASE
           ORDER BY je.entry_date DESC, je.source_created_at DESC""",
        (user["email"],),
    ).fetchall()
    conn.close()
    tpl = _jinja.get_template("journal.html")
    return HTMLResponse(tpl.render(user=user, entries=[dict(r) for r in rows]))


# ── Routes: Calorie API ────────────────────────────────────────────────

@app.post("/api/estimate")
async def api_estimate(request: Request):
    user = _require_auth(request)
    body = await request.json()
    desc = body.get("description", "").strip()
    itype = body.get("input_type", "text")
    if not desc:
        raise HTTPException(400, "No food description provided")
    result = estimate_text(desc)
    return _save_entry(user["email"], itype, desc, result)


@app.post("/api/transcribe")
async def api_transcribe(request: Request, audio: UploadFile = File(...)):
    _require_auth(request)
    data = await audio.read()
    text = transcribe_audio(data, audio.filename or "audio.webm")
    if not text:
        raise HTTPException(500, "Transcription failed — check XAI_API_KEY")
    return {"text": text}


@app.post("/api/estimate-photo")
async def api_estimate_photo(request: Request, file: UploadFile = File(...)):
    user = _require_auth(request)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    data = await file.read()
    if len(data) > 20_000_000:
        raise HTTPException(400, "Image too large (max 20 MB)")

    ext = file.content_type.split("/")[-1].split("+")[0]
    fname = f"{uuid.uuid4().hex[:12]}.{ext}"
    (UPLOAD_DIR / fname).write_bytes(data)

    result = estimate_photo(data, file.content_type)
    return _save_entry(user["email"], "photo", file.filename or "photo", result, fname)


@app.get("/api/entries")
async def api_entries(request: Request, date_str: str = None, days: int = 1):
    user = _require_auth(request)
    conn = get_db()
    if date_str:
        rows = conn.execute(
            "SELECT * FROM entries WHERE user_email=? AND entry_date=? ORDER BY created_at DESC",
            (user["email"], date_str)).fetchall()
    else:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = conn.execute(
            "SELECT * FROM entries WHERE user_email=? AND entry_date>=? ORDER BY entry_date DESC, created_at DESC",
            (user["email"], cutoff)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@app.get("/api/summary")
async def api_summary(request: Request):
    user = _require_auth(request)
    conn = get_db()
    today_iso = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    em = user["email"]

    t = conn.execute(
        "SELECT COALESCE(SUM(total_calories),0) as total, COUNT(*) as cnt "
        "FROM entries WHERE user_email=? AND entry_date=?", (em, today_iso)).fetchone()

    week = conn.execute(
        "SELECT entry_date, SUM(total_calories) as total, COUNT(*) as cnt "
        "FROM entries WHERE user_email=? AND entry_date>=? GROUP BY entry_date ORDER BY entry_date",
        (em, week_ago)).fetchall()

    meals = conn.execute(
        "SELECT meal_type, SUM(total_calories) as total, COUNT(*) as cnt "
        "FROM entries WHERE user_email=? AND entry_date=? GROUP BY meal_type",
        (em, today_iso)).fetchall()

    # Today's macros
    macros = conn.execute(
        "SELECT COALESCE(SUM(total_protein_g),0) as protein, "
        "COALESCE(SUM(total_carbs_g),0) as carbs, "
        "COALESCE(SUM(total_fat_g),0) as fat, "
        "COALESCE(SUM(total_fiber_g),0) as fiber "
        "FROM entries WHERE user_email=? AND entry_date=?", (em, today_iso)).fetchone()

    # Today's blood sugar
    bs = conn.execute(
        "SELECT * FROM blood_sugar WHERE user_email=? AND entry_date=? ORDER BY measured_at DESC",
        (em, today_iso)).fetchall()

    conn.close()

    wt = sum(r["total"] for r in week)
    wd = len(week) or 1
    return {
        "today": {"calories": t["total"], "entries": t["cnt"],
                  "protein": macros["protein"], "carbs": macros["carbs"],
                  "fat": macros["fat"], "fiber": macros["fiber"]},
        "week": {
            "total": wt, "daily_avg": round(wt / wd),
            "days": [{"date": r["entry_date"], "calories": r["total"],
                      "entries": r["cnt"]} for r in week],
        },
        "meals": {r["meal_type"]: {"calories": r["total"],
                                    "entries": r["cnt"]} for r in meals},
        "blood_sugar": [dict(r) for r in bs],
    }


@app.delete("/api/entries/{entry_id}")
async def api_delete(entry_id: str, request: Request):
    user = _require_auth(request)
    conn = get_db()
    row = conn.execute("SELECT photo_filename FROM entries WHERE id=? AND user_email=?",
                       (entry_id, user["email"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Entry not found")
    if row["photo_filename"]:
        p = UPLOAD_DIR / row["photo_filename"]
        if p.exists():
            p.unlink()
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return {"deleted": entry_id}


@app.patch("/api/entries/{entry_id}")
async def api_update(entry_id: str, request: Request):
    user = _require_auth(request)
    body = await request.json()
    conn = get_db()
    row = conn.execute("SELECT id FROM entries WHERE id=? AND user_email=?",
                       (entry_id, user["email"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Entry not found")
    # entry_date drives the reporting bucket; created_at is the displayed
    # time. Both are editable so the user can retroactively slot an entry
    # into the right day (e.g. logged next morning but eaten last night).
    allowed = {"meal_type", "notes", "total_calories", "entry_date", "created_at"}
    sets, vals = [], []
    for k, v in body.items():
        if k not in allowed:
            continue
        if k == "entry_date" and v:
            # Accept YYYY-MM-DD; reject anything else so a bad string can't
            # corrupt reporting queries.
            try:
                v = date.fromisoformat(str(v)).isoformat()
            except ValueError:
                raise HTTPException(400, "entry_date must be YYYY-MM-DD")
        if k == "created_at" and v:
            try:
                v = datetime.fromisoformat(str(v).replace("Z", "+00:00")).isoformat()
            except ValueError:
                raise HTTPException(400, "created_at must be ISO 8601")
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        vals.extend([datetime.now().isoformat(), entry_id])
        conn.execute(f"UPDATE entries SET {','.join(sets)}, updated_at=? WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"updated": entry_id}


# ── Routes: Blood Sugar API ────────────────────────────────────────────

@app.post("/api/blood-sugar")
async def api_add_blood_sugar(request: Request):
    user = _require_auth(request)
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "Blood sugar value required")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid blood sugar value")

    context = body.get("context", "fasting")
    notes = body.get("notes", "")
    measured_at = body.get("measured_at", datetime.now().isoformat())

    bid = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        INSERT INTO blood_sugar (id, user_email, value, unit, context, notes,
                                 measured_at, entry_date, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (bid, user["email"], value, "mg/dL", context, notes, measured_at, today, now))
    conn.commit()
    conn.close()
    return {"id": bid, "value": value, "context": context, "measured_at": measured_at}


@app.get("/api/blood-sugar")
async def api_get_blood_sugar(request: Request, days: int = 7):
    user = _require_auth(request)
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM blood_sugar WHERE user_email=? AND entry_date>=? ORDER BY measured_at DESC",
        (user["email"], cutoff)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/blood-sugar/{bs_id}")
async def api_delete_blood_sugar(bs_id: str, request: Request):
    user = _require_auth(request)
    conn = get_db()
    row = conn.execute("SELECT id FROM blood_sugar WHERE id=? AND user_email=?",
                       (bs_id, user["email"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Reading not found")
    conn.execute("DELETE FROM blood_sugar WHERE id=?", (bs_id,))
    conn.commit()
    conn.close()
    return {"deleted": bs_id}


# ── Routes: Report API ─────────────────────────────────────────────────

@app.get("/api/report-data")
async def api_report_data(request: Request, days: int = 7):
    user = _require_auth(request)
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    em = user["email"]
    conn = get_db()

    entries = conn.execute(
        "SELECT * FROM entries WHERE user_email=? AND entry_date>=? ORDER BY entry_date, created_at",
        (em, cutoff)).fetchall()

    daily = conn.execute(
        "SELECT entry_date, SUM(total_calories) as cal, SUM(total_protein_g) as protein, "
        "SUM(total_carbs_g) as carbs, SUM(total_fat_g) as fat, SUM(total_fiber_g) as fiber, "
        "COUNT(*) as cnt FROM entries WHERE user_email=? AND entry_date>=? "
        "GROUP BY entry_date ORDER BY entry_date", (em, cutoff)).fetchall()

    bs = conn.execute(
        "SELECT * FROM blood_sugar WHERE user_email=? AND entry_date>=? ORDER BY measured_at",
        (em, cutoff)).fetchall()

    conn.close()
    return {
        "user": user,
        "period_days": days,
        "from_date": cutoff,
        "to_date": date.today().isoformat(),
        "entries": [_row_to_dict(r) for r in entries],
        "daily_summary": [dict(r) for r in daily],
        "blood_sugar": [dict(r) for r in bs],
    }


# ── Routes: Export ──────────────────────────────────────────────────────

@app.get("/api/export")
async def api_export(request: Request, days: int = 30):
    user = _require_auth(request)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_email=? AND entry_date>=? ORDER BY entry_date DESC, created_at DESC",
        (user["email"], cutoff)).fetchall()
    bs = conn.execute(
        "SELECT * FROM blood_sugar WHERE user_email=? AND entry_date>=? ORDER BY measured_at DESC",
        (user["email"], cutoff)).fetchall()
    conn.close()
    return {
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "model": "xAI Grok-3",
            "export_days": days,
            "total_entries": len(rows),
            "total_blood_sugar": len(bs),
        },
        "entries": [_row_to_dict(r) for r in rows],
        "blood_sugar": [dict(r) for r in bs],
    }


@app.get("/api/photo/{filename}")
async def api_photo(filename: str):
    p = UPLOAD_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Photo not found")
    return FileResponse(str(p))


def _bearer_admin_ok(request: Request) -> bool:
    expected = FA_SYNC_TOKEN.strip()
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    presented = header.split(" ", 1)[1].strip()
    return bool(presented) and presented == expected


def _require_admin(request: Request):
    if _bearer_admin_ok(request):
        return {"email": "service:fa-sync", "name": "FA Sync Service"}
    user = _require_auth(request)
    if not ADMIN_EMAILS or user["email"].lower() not in ADMIN_EMAILS:
        raise HTTPException(403, "Admin only")
    return user


def _upsert_journal_rows(rows: list[dict], aemails: list[dict]) -> tuple[int, int]:
    # Members can have multiple allowed_emails (e.g. work + personal). Collect all
    # of them per member_name so /journal renders for whichever address the user
    # signs in with. journal_entries.user_email keeps the first-seen (primary) email
    # for backward compat + simple indexed lookups; the join table journal_entry_email
    # is the source of truth for auth-time matching.
    emails_by_name: dict[str, list[str]] = {}
    for r in aemails:
        name = (r.get("member_name") or "").strip()
        email = (r.get("email") or "").strip()
        if not (name and email):
            continue
        bucket = emails_by_name.setdefault(name, [])
        if email not in bucket:
            bucket.append(email)

    conn = get_db()
    imported = updated = 0
    now = datetime.now().isoformat()
    # Seed allowed_emails so family members can sign in with any address FA
    # reports for them. Without this, _current_user silently signs them out
    # (ADMIN_EMAILS + allowed_emails are the only two whitelists).
    for r in aemails:
        em = (r.get("email") or "").strip()
        if em:
            conn.execute("INSERT OR IGNORE INTO allowed_emails (email) VALUES (?)", (em,))
    for r in rows:
        member_name = (r.get("member_name") or "").strip()
        entry_date = r.get("entry_date") or ""
        source_created_at = r.get("created_at") or ""
        source_key = f"{member_name}|{entry_date}|{source_created_at}"
        emails = emails_by_name.get(member_name, [])
        primary_email = emails[0] if emails else None
        existing = conn.execute(
            "SELECT id FROM journal_entries WHERE source='fa' AND source_key=?",
            (source_key,),
        ).fetchone()
        if existing:
            journal_id = existing["id"]
            conn.execute(
                """UPDATE journal_entries SET
                     user_email=?, content=?, mood=?, tags=?, imported_at=?
                   WHERE id=?""",
                (primary_email, r.get("content"), r.get("mood"), r.get("tags"), now, journal_id),
            )
            updated += 1
        else:
            cur = conn.execute(
                """INSERT INTO journal_entries
                     (source, source_key, user_email, member_name, entry_date,
                      content, mood, tags, source_created_at, imported_at)
                   VALUES ('fa', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_key, primary_email, member_name, entry_date,
                 r.get("content"), r.get("mood"), r.get("tags"),
                 source_created_at, now),
            )
            journal_id = cur.lastrowid
            imported += 1
        conn.execute("DELETE FROM journal_entry_email WHERE journal_id = ?", (journal_id,))
        for em in emails:
            conn.execute(
                "INSERT INTO journal_entry_email (journal_id, email) VALUES (?, ?)",
                (journal_id, em),
            )
    conn.commit()
    conn.close()
    return imported, updated


@app.post("/api/admin/sync/fa-journal")
async def api_admin_sync_fa_journal(request: Request):
    _require_admin(request)
    if not FA_SYNC_TOKEN:
        raise HTTPException(500, "FA_SYNC_TOKEN not configured")

    import httpx

    url = f"{FA_BASE_URL}/api/v1/sync/export"
    headers = {"Authorization": f"Bearer {FA_SYNC_TOKEN}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"FA fetch failed: {e}")

    if not payload.get("ok"):
        raise HTTPException(502, f"FA returned error: {payload.get('errors')}")

    tables = payload.get("data", {}).get("tables", {})
    imported, updated = _upsert_journal_rows(
        tables.get("member_journal", []),
        tables.get("allowed_emails", []),
    )
    return {"ok": True, "imported": imported, "updated": updated,
            "total_rows": imported + updated, "source": "railway-fa"}


@app.post("/api/admin/sync/journal-upsert")
async def api_admin_sync_journal_upsert(request: Request):
    _require_admin(request)
    body = await request.json()
    imported, updated = _upsert_journal_rows(
        body.get("member_journal") or [],
        body.get("allowed_emails") or [],
    )
    return {"ok": True, "imported": imported, "updated": updated,
            "total_rows": imported + updated, "source": "posted"}


@app.post("/api/admin/sync/journal-delete")
async def api_admin_sync_journal_delete(request: Request):
    """Propagate a FA journal-entry deletion into CYC. Identifies the row by
    (source='fa', source_key='{member_name}|{entry_date}|{created_at}') —
    the same identity used by _upsert_journal_rows for idempotent inserts.

    Cascades: journal_entry_email (FK CASCADE) + any backfilled `entries`
    rows written with source='fa_journal' + source_key='{journal_id}:*'
    (the per-email calorie fanout). Manual food logs on the same day are
    untouched — they have source='' and are user-authored calorie data,
    not tied to the deleted journal entry."""
    _require_admin(request)
    body = await request.json()
    member_name = (body.get("member_name") or "").strip()
    entry_date = (body.get("entry_date") or "").strip()
    created_at = (body.get("created_at") or "").strip()
    if not (member_name and entry_date and created_at):
        raise HTTPException(400, "member_name, entry_date, created_at required")

    source_key = f"{member_name}|{entry_date}|{created_at}"
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM journal_entries WHERE source='fa' AND source_key=?",
        (source_key,),
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": True, "deleted": False, "reason": "not_found",
                "source_key": source_key}
    journal_id = row["id"]

    entries_deleted = conn.execute(
        "DELETE FROM entries WHERE source='fa_journal' AND source_key LIKE ?",
        (f"{journal_id}:%",),
    ).rowcount
    conn.execute("DELETE FROM journal_entries WHERE id = ?", (journal_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": True, "journal_id": journal_id,
            "backfilled_entries_deleted": entries_deleted,
            "source_key": source_key}


@app.get("/api/admin/journal-stats")
async def api_admin_journal_stats(request: Request):
    _require_admin(request)
    conn = get_db()
    total_entries = conn.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"]
    total_join = conn.execute("SELECT COUNT(*) AS n FROM journal_entry_email").fetchone()["n"]
    per_email = [
        {"email": r["email"], "count": r["n"]}
        for r in conn.execute(
            """SELECT email, COUNT(*) AS n FROM journal_entry_email
               GROUP BY email ORDER BY n DESC, email"""
        ).fetchall()
    ]
    orphans = [
        {"member_name": r["member_name"], "count": r["n"]}
        for r in conn.execute(
            """SELECT je.member_name, COUNT(*) AS n
               FROM journal_entries je
               LEFT JOIN journal_entry_email jee ON jee.journal_id = je.id
               WHERE jee.id IS NULL
               GROUP BY je.member_name ORDER BY n DESC"""
        ).fetchall()
    ]
    conn.close()
    return {"ok": True,
            "total_journal_entries": total_entries,
            "total_journal_entry_email": total_join,
            "per_email": per_email,
            "orphans_by_member": orphans}


@app.post("/api/admin/backfill-calories-from-journal")
async def api_admin_backfill_calories_from_journal(
    request: Request, email: str | None = None, limit: int = 25
):
    """Estimate calories for each journal entry that hasn't yet been processed
    and insert a backdated row into `entries`. Idempotent via (source, source_key).

    - `email`: process only entries linked to this address; omit for ALL emails.
    - `limit`: max entries this call processes (Railway request-timeout guard).

    Response returns processed/skipped counts + remaining, so a caller can loop
    until `remaining == 0`.
    """
    _require_admin(request)
    conn = get_db()

    # One-shot cleanup of legacy backfill rows written with the old source_key
    # scheme (single journal_id — didn't fan out per email, so charts filtered
    # by the signed-in user's email saw nothing). Detect legacy by absence of
    # ":" in source_key. Safe to run every request — after cleanup, matches 0.
    conn.execute(
        "DELETE FROM entries WHERE source='fa_journal' AND source_key NOT LIKE '%:%'"
    )

    # A journal entry needs processing (per email) when its (journal_id, email)
    # pair has no matching `entries` row. Query is at the (journal × email)
    # grain — one row per pair. To keep Grok calls minimal we still process by
    # journal_id (call Grok once, insert N rows below).
    base_query = """
        SELECT DISTINCT je.id AS journal_id, je.entry_date, je.content, je.member_name
        FROM journal_entries je
        JOIN journal_entry_email jee ON jee.journal_id = je.id
        WHERE NOT EXISTS (
            SELECT 1 FROM entries e
            WHERE e.source = 'fa_journal'
              AND e.source_key = CAST(je.id AS TEXT) || ':' || jee.email
        )
    """
    params: list = []
    if email:
        base_query += " AND jee.email = ? COLLATE NOCASE"
        params.append(email)
    base_query += " ORDER BY je.entry_date ASC LIMIT ?"
    params.append(limit)

    to_process = conn.execute(base_query, params).fetchall()

    # remaining is measured at the (journal × email) pair grain — same units
    # the caller intuits from `batch_size`.
    remaining_query = """
        SELECT COUNT(*) AS n FROM journal_entries je
        JOIN journal_entry_email jee ON jee.journal_id = je.id
        WHERE NOT EXISTS (
            SELECT 1 FROM entries e
            WHERE e.source = 'fa_journal'
              AND e.source_key = CAST(je.id AS TEXT) || ':' || jee.email
        )
    """
    if email:
        remaining_query += " AND jee.email = ? COLLATE NOCASE"
    remaining_before = conn.execute(
        remaining_query, ([email] if email else [])
    ).fetchone()["n"]

    processed = 0
    skipped_empty = 0
    errors = 0
    for row in to_process:
        emails_for_journal = [
            r["email"] for r in conn.execute(
                "SELECT email FROM journal_entry_email WHERE journal_id = ?",
                (row["journal_id"],),
            ).fetchall()
        ]
        if not emails_for_journal:
            continue
        content = (row["content"] or "").strip()
        if not content:
            skipped_empty += 1
            _insert_journal_calorie_entries_per_email(
                conn, row, emails_for_journal,
                {"items": [], "total_calories": 0,
                 "confidence": "low", "meal_type": "snack",
                 "notes": "empty journal content"}
            )
            continue
        result = estimate_text(content)
        if result.get("error"):
            errors += 1
            continue
        _insert_journal_calorie_entries_per_email(
            conn, row, emails_for_journal, result
        )
        processed += 1

    conn.commit()

    remaining_after = conn.execute(
        remaining_query, ([email] if email else [])
    ).fetchone()["n"]
    conn.close()

    return {"ok": True,
            "email_filter": email,
            "batch_size": len(to_process),
            "processed": processed,
            "skipped_empty": skipped_empty,
            "errors": errors,
            "remaining_before": remaining_before,
            "remaining_after": remaining_after}


def _insert_journal_calorie_entries_per_email(conn, journal_row, emails, estimate_result):
    """Insert one backdated `entries` row per linked email, so charts filtered
    by any of the journal's linked addresses see the data. Idempotent via
    unique (source, source_key) where source_key = "{journal_id}:{email}"."""
    now = datetime.now().isoformat()
    entry_date = journal_row["entry_date"] or now[:10]
    for em in emails:
        _insert_journal_calorie_entry(
            conn, journal_row, estimate_result,
            user_email=em, entry_date=entry_date,
            source_key=f"{journal_row['journal_id']}:{em}",
        )


def _insert_journal_calorie_entry(conn, journal_row, estimate_result,
                                  user_email, entry_date, source_key):
    """Insert a single backdated `entries` row for one (journal, email) pair.
    Idempotent via unique (source, source_key)."""
    eid = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT OR IGNORE INTO entries
            (id, user_email, input_type, raw_input, photo_filename, items,
             total_calories, total_protein_g, total_carbs_g, total_fat_g, total_fiber_g,
             confidence, meal_type, notes, entry_date, created_at, updated_at,
             source, source_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (eid, user_email, "text",
          journal_row["content"], None,
          json.dumps(estimate_result.get("items", [])),
          estimate_result.get("total_calories", 0),
          estimate_result.get("total_protein_g", 0),
          estimate_result.get("total_carbs_g", 0),
          estimate_result.get("total_fat_g", 0),
          estimate_result.get("total_fiber_g", 0),
          estimate_result.get("confidence", "medium"),
          estimate_result.get("meal_type", "snack"),
          estimate_result.get("notes", ""),
          entry_date, now, now,
          "fa_journal", source_key))


@app.get("/api/admin/outbound-pending")
async def api_admin_outbound_pending(request: Request, since_id: int = 0, limit: int = 100):
    """Local sync orchestrator polls this to mirror queue rows to local
    SQLite. Returns rows with id > since_id that haven't been mirror-consumed
    yet. Includes completed AND still-pending rows so the orchestrator gets
    a full picture regardless of live-delivery state."""
    _require_admin(request)
    conn = get_db()
    rows = conn.execute(
        """SELECT id, target, endpoint_url, payload_json, source_ref, attempts,
                  next_attempt_at, last_error, completed_at, created_at
             FROM outbound_sync
            WHERE id > ? AND mirror_consumed_at IS NULL
            ORDER BY id ASC LIMIT ?""",
        (since_id, limit),
    ).fetchall()
    conn.close()
    return {"app": "cyc", "count": len(rows),
            "rows": [dict(r) for r in rows]}


@app.post("/api/admin/outbound-ack")
async def api_admin_outbound_ack(request: Request):
    """Local orchestrator calls this after successfully mirroring rows into
    local SQLite. Marks mirror_consumed_at so future polls skip them."""
    _require_admin(request)
    body = await request.json()
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise HTTPException(400, "ids must be a list of integers")
    now = datetime.now().isoformat()
    conn = get_db()
    placeholders = ",".join("?" * len(ids)) if ids else "NULL"
    n = 0
    if ids:
        cur = conn.execute(
            f"UPDATE outbound_sync SET mirror_consumed_at = ? "
            f"WHERE id IN ({placeholders}) AND mirror_consumed_at IS NULL",
            [now] + ids,
        )
        n = cur.rowcount
    conn.commit()
    conn.close()
    return {"ok": True, "acked": n}


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
