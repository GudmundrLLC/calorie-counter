"""Calorie Counter — track food intake via text, voice, or photo."""

import os
import json
import sqlite3
import base64
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader
import anthropic
import uvicorn


# ── Config ──────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent
_VOLUME = Path("/app/data")
DB_DIR = _VOLUME if _VOLUME.exists() else APP_DIR
DB_PATH = DB_DIR / "calories.db"
UPLOAD_DIR = DB_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MODEL = os.environ.get("CALORIE_MODEL", "claude-sonnet-4-6-20250514")

app = FastAPI(title="Calorie Counter", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_jinja = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")), autoescape=True)


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
            input_type  TEXT NOT NULL CHECK(input_type IN ('text','voice','photo')),
            raw_input   TEXT,
            photo_filename TEXT,
            items       TEXT NOT NULL DEFAULT '[]',
            total_calories REAL NOT NULL DEFAULT 0,
            confidence  TEXT DEFAULT 'medium',
            meal_type   TEXT DEFAULT 'snack',
            notes       TEXT DEFAULT '',
            entry_date  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);
    """)
    conn.commit()
    conn.close()


init_db()


# ── AI estimation ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a precise nutritionist. Estimate calories for food described by the user.

Return ONLY valid JSON (no markdown fences) with this structure:
{
  "items": [{"name": "food", "quantity": "amount", "calories": 123}],
  "total_calories": 123,
  "meal_type": "breakfast|lunch|dinner|snack",
  "confidence": "high|medium|low",
  "notes": "Brief note"
}

Rules:
- Reference USDA data. Round to nearest whole number.
- Infer meal_type from food type and time context.
- confidence: high = common food with well-known values, medium = reasonable estimate, low = vague description.
- If quantity isn't specified, assume one standard serving."""


def _get_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _parse_ai_response(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1]
    if t.endswith("```"):
        t = t[:-3].strip()
    return json.loads(t)


def _time_context() -> str:
    return f"Current time: {datetime.now().strftime('%I:%M %p')}"


def _guess_meal() -> str:
    h = datetime.now().hour
    if 5 <= h < 11:
        return "breakfast"
    if 11 <= h < 15:
        return "lunch"
    if 15 <= h < 21:
        return "dinner"
    return "snack"


def estimate_text(description: str) -> dict:
    client = _get_client()
    if not client:
        return _fallback(description)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": f"{_time_context()}\n\nFood: {description}"}],
        )
        return _parse_ai_response(resp.content[0].text)
    except Exception as e:
        return {"items": [], "total_calories": 0, "confidence": "low",
                "meal_type": _guess_meal(), "notes": f"Estimation error: {e}"}


def estimate_photo(image_data: bytes, media_type: str) -> dict:
    client = _get_client()
    if not client:
        return {"items": [], "total_calories": 0, "confidence": "low",
                "meal_type": _guess_meal(),
                "notes": "Photo analysis requires ANTHROPIC_API_KEY"}
    try:
        b64 = base64.standard_b64encode(image_data).decode()
        resp = client.messages.create(
            model=MODEL, max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text",
                 "text": f"{_time_context()}\n\nIdentify all food in this photo and estimate calories."}
            ]}],
        )
        return _parse_ai_response(resp.content[0].text)
    except Exception as e:
        return {"items": [], "total_calories": 0, "confidence": "low",
                "meal_type": _guess_meal(), "notes": f"Photo error: {e}"}


# Simple keyword fallback when no API key is set
_COMMON = {
    "egg": 78, "toast": 80, "butter": 100, "coffee": 5, "milk": 150,
    "juice": 110, "apple": 95, "banana": 105, "rice": 205, "chicken": 165,
    "salad": 150, "sandwich": 350, "pizza": 285, "burger": 354,
    "fries": 365, "pasta": 220, "steak": 271, "salmon": 208,
    "yogurt": 150, "cereal": 200, "oatmeal": 150, "soup": 150,
    "taco": 210, "burrito": 350, "sushi": 200, "beer": 153,
    "wine": 125, "soda": 140, "cookie": 160, "cake": 350,
    "ice cream": 270, "donut": 250, "bagel": 270, "water": 0,
}


def _fallback(desc: str) -> dict:
    low = desc.lower()
    items, total = [], 0
    for food, cal in _COMMON.items():
        if food in low:
            items.append({"name": food, "quantity": "1 serving", "calories": cal})
            total += cal
    if not items:
        items = [{"name": desc, "quantity": "1 serving", "calories": 200}]
        total = 200
    return {"items": items, "total_calories": total, "confidence": "low",
            "meal_type": _guess_meal(),
            "notes": "Keyword fallback — set ANTHROPIC_API_KEY for AI estimates."}


# ── Helpers ─────────────────────────────────────────────────────────────

def _save_entry(input_type, raw_input, result, photo_filename=None):
    eid = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    conn = get_db()
    conn.execute("""
        INSERT INTO entries (id, input_type, raw_input, photo_filename, items,
            total_calories, confidence, meal_type, notes, entry_date, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (eid, input_type, raw_input, photo_filename,
          json.dumps(result.get("items", [])),
          result.get("total_calories", 0),
          result.get("confidence", "medium"),
          result.get("meal_type", _guess_meal()),
          result.get("notes", ""), today, now, now))
    conn.commit()
    conn.close()
    result.update(id=eid, entry_date=today, created_at=now,
                  raw_input=raw_input, input_type=input_type)
    return result


# ── Routes ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tpl = _jinja.get_template("index.html")
    return HTMLResponse(tpl.render())


@app.post("/api/estimate")
async def api_estimate(request: Request):
    body = await request.json()
    desc = body.get("description", "").strip()
    itype = body.get("input_type", "text")
    if not desc:
        raise HTTPException(400, "No food description provided")
    result = estimate_text(desc)
    return _save_entry(itype, desc, result)


@app.post("/api/estimate-photo")
async def api_estimate_photo(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    data = await file.read()
    if len(data) > 20_000_000:
        raise HTTPException(400, "Image too large (max 20 MB)")

    ext = file.content_type.split("/")[-1].split("+")[0]
    fname = f"{uuid.uuid4().hex[:12]}.{ext}"
    (UPLOAD_DIR / fname).write_bytes(data)

    result = estimate_photo(data, file.content_type)
    return _save_entry("photo", file.filename or "photo", result, fname)


@app.get("/api/entries")
async def api_entries(date_str: str = None, days: int = 1):
    conn = get_db()
    if date_str:
        rows = conn.execute(
            "SELECT * FROM entries WHERE entry_date=? ORDER BY created_at DESC",
            (date_str,)).fetchall()
    else:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = conn.execute(
            "SELECT * FROM entries WHERE entry_date>=? ORDER BY entry_date DESC, created_at DESC",
            (cutoff,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@app.get("/api/summary")
async def api_summary():
    conn = get_db()
    today_iso = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=6)).isoformat()

    t = conn.execute(
        "SELECT COALESCE(SUM(total_calories),0) as total, COUNT(*) as cnt "
        "FROM entries WHERE entry_date=?", (today_iso,)).fetchone()

    week = conn.execute(
        "SELECT entry_date, SUM(total_calories) as total, COUNT(*) as cnt "
        "FROM entries WHERE entry_date>=? GROUP BY entry_date ORDER BY entry_date",
        (week_ago,)).fetchall()

    meals = conn.execute(
        "SELECT meal_type, SUM(total_calories) as total, COUNT(*) as cnt "
        "FROM entries WHERE entry_date=? GROUP BY meal_type",
        (today_iso,)).fetchall()
    conn.close()

    wt = sum(r["total"] for r in week)
    wd = len(week) or 1
    return {
        "today": {"calories": t["total"], "entries": t["cnt"]},
        "week": {
            "total": wt, "daily_avg": round(wt / wd),
            "days": [{"date": r["entry_date"], "calories": r["total"],
                      "entries": r["cnt"]} for r in week],
        },
        "meals": {r["meal_type"]: {"calories": r["total"],
                                    "entries": r["cnt"]} for r in meals},
    }


@app.delete("/api/entries/{entry_id}")
async def api_delete(entry_id: str):
    conn = get_db()
    row = conn.execute("SELECT photo_filename FROM entries WHERE id=?",
                       (entry_id,)).fetchone()
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
    body = await request.json()
    conn = get_db()
    row = conn.execute("SELECT id FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Entry not found")
    allowed = {"meal_type", "notes", "total_calories"}
    sets, vals = [], []
    for k, v in body.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(datetime.now().isoformat())
        vals.append(entry_id)
        conn.execute(f"UPDATE entries SET {','.join(sets)}, updated_at=? WHERE id=?", vals)
        conn.commit()
    conn.close()
    return {"updated": entry_id}


@app.get("/api/export")
async def api_export(days: int = 30):
    conn = get_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM entries WHERE entry_date>=? ORDER BY entry_date DESC, created_at DESC",
        (cutoff,)).fetchall()
    conn.close()
    return {
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "model": "Claude Sonnet 4.6",
            "export_days": days,
            "total_entries": len(rows),
        },
        "entries": [_row_to_dict(r) for r in rows],
    }


@app.get("/api/photo/{filename}")
async def api_photo(filename: str):
    p = UPLOAD_DIR / filename
    if not p.exists():
        raise HTTPException(404, "Photo not found")
    return FileResponse(str(p))


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


def _row_to_dict(row):
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
