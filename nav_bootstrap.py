"""Nav v2 · boot-time init for calorie-counter · S2 · Phase 3 · 2026-09-25.

Mirrors bp-demo's db._apply_migration + _autoseed_nav_placement_defaults
pattern, but scoped to the calorie-counter root (which has no db.py — the
app uses inline sqlite3 against calories.db). This module is import-time
side-effectful: applying the migration + seeding baseline rows on cold
boot so the placement engine has a table + rows to overlay against.

Idempotent — safe on every boot.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).parent

# Match calorie-counter's data-location convention: prefer Railway volume
# at /app/data when mounted, else fall back to APP_DIR.
_VOLUME = Path("/app/data")
DATA_DIR = _VOLUME if _VOLUME.exists() else APP_DIR
WB_PATH = DATA_DIR / "writeback.db"


def _wb_conn() -> sqlite3.Connection:
    """Local writeback connection. Nav v2 rows only — calorie data stays in calories.db."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(WB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _apply_migration(rel_path: str, label: str) -> None:
    """Load a migration module by relative path and call apply(conn)."""
    import importlib.util
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        mig_path = APP_DIR / rel_path
        if not mig_path.exists():
            print(f"[{label}] SKIP · migration file missing at {mig_path}", flush=True)
            return
        spec = importlib.util.spec_from_file_location(label, str(mig_path))
        if spec is None or spec.loader is None:
            print(f"[{label}] SKIP · importlib spec unavailable", flush=True)
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        conn = sqlite3.connect(str(WB_PATH))
        try:
            mod.apply(conn)
        finally:
            conn.close()
        print(f"[{label}] applied {rel_path}", flush=True)
    except Exception as e:
        print(f"[{label}] FAILED {rel_path}: {type(e).__name__}: {e}", flush=True)


def _autoseed_nav_placement_defaults() -> None:
    """Seed baseline placement rows if the (default/default) set is empty."""
    try:
        if not WB_PATH.exists():
            return
        conn = sqlite3.connect(str(WB_PATH))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM nav_placement "
                "WHERE tenant_id='default' AND role='default'"
            ).fetchone()
            if row and row[0] > 0:
                return
            import importlib.util
            seed_path = APP_DIR / "scripts" / "seed_nav_placement_defaults.py"
            if not seed_path.exists():
                print("[nav-placement-seed] SKIP · seed script missing", flush=True)
                return
            spec = importlib.util.spec_from_file_location("seed_nav", str(seed_path))
            if spec is None or spec.loader is None:
                return
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            n = mod.apply_seed(conn, wipe=False, dry_run=False)
            print(f"[nav-placement-seed] inserted {n} baseline rows", flush=True)
        finally:
            conn.close()
    except Exception as e:
        print(f"[nav-placement-seed] FAILED: {type(e).__name__}: {e}", flush=True)


def init() -> None:
    """One-shot boot init — called from app.py at import time."""
    _apply_migration("migrations/2026_09_24_nav_placement.py", "nav-placement-mig")
    _autoseed_nav_placement_defaults()


# Import-time side effect (matches bp-demo/qual-demo pattern).
init()
