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
    """)
    # Seed admin emails
    for em in ADMIN_EMAILS:
        conn.execute("INSERT OR IGNORE INTO allowed_emails (email) VALUES (?)", (em,))
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
    conn.commit()
    conn.close()
    result.update(id=eid, entry_date=today, created_at=now,
                  raw_input=raw_input, input_type=input_type)
    return result


def _row_to_dict(row):
    d = dict(row)
    if "items" in d:
        d["items"] = json.loads(d["items"])
    return d


# ── Routes: Auth ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    setup_oauth()


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
        """SELECT entry_date, content, mood, tags
           FROM journal_entries
           WHERE user_email = ? COLLATE NOCASE
           ORDER BY entry_date DESC, source_created_at DESC""",
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

    rows = payload.get("data", {}).get("tables", {}).get("member_journal", [])
    email_by_name = {}
    aemails = payload.get("data", {}).get("tables", {}).get("allowed_emails", [])
    for r in aemails:
        name = (r.get("member_name") or "").strip()
        email = (r.get("email") or "").strip()
        if name and email and name not in email_by_name:
            email_by_name[name] = email

    conn = get_db()
    imported = updated = 0
    now = datetime.now().isoformat()
    for r in rows:
        member_name = (r.get("member_name") or "").strip()
        entry_date = r.get("entry_date") or ""
        source_created_at = r.get("created_at") or ""
        source_key = f"{member_name}|{entry_date}|{source_created_at}"
        user_email = email_by_name.get(member_name)
        existing = conn.execute(
            "SELECT id FROM journal_entries WHERE source='fa' AND source_key=?",
            (source_key,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE journal_entries SET
                     user_email=?, content=?, mood=?, tags=?, imported_at=?
                   WHERE id=?""",
                (user_email, r.get("content"), r.get("mood"), r.get("tags"), now, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO journal_entries
                     (source, source_key, user_email, member_name, entry_date,
                      content, mood, tags, source_created_at, imported_at)
                   VALUES ('fa', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_key, user_email, member_name, entry_date,
                 r.get("content"), r.get("mood"), r.get("tags"),
                 source_created_at, now),
            )
            imported += 1
    conn.commit()
    conn.close()

    return {"ok": True, "imported": imported, "updated": updated, "total_rows": len(rows)}


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
