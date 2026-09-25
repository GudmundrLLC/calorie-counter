"""Seed nav_placement baseline rows · v2·3 phase 3a · 2026-09-24.

Populates ``writeback.db.nav_placement`` with the ``(tenant='default',
role='default')`` baseline that mirrors today's v1 10-slot skyline. The
resolver overlays tenant/role specific rows on top of these baselines;
without them, every empty-DB app would fall back to registry defaults
(technically fine, but the Data-viz-fidelity discipline says seed
explicitly so the admin UI at ``/admin/nav-config`` shows a clean row
set from day one).

Idempotent: uses ``INSERT OR REPLACE`` on the composite PK
``(tenant_id, role, feature_key)``, so re-running never duplicates or
drifts.

Invocation
----------
::

    uv run python scripts/seed_nav_placement_defaults.py [--dry-run] [--wipe]

* ``--dry-run``  — print the rows that would be inserted without
  touching the DB.
* ``--wipe``     — DELETE existing (default/default) rows first (safe;
  never touches tenant/role overlays). Use only when re-baselining after
  a schema change.

Baseline design source: navigation-v2-execution-plan-2026-09-24.html
§III·v2·3 phase 3a + v1's 10-slot skyline documented in
universal-navigation-principles-2026-09-24.html.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


# Extend sys.path so importing nav_features works when the script is run
# from bp-demo's project root OR from anywhere else.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nav_features import NAV_FEATURES  # noqa: E402


# Baseline placement — every feature's default_slot, in a stable order so
# `slot_order` is meaningful. Grouped comments make future tweaks readable.
# When adding a NEW feature: add it to nav_features.py + one row here.
BASELINE: list[tuple[str, str, int, int]] = [
    # (feature_key, slot, slot_order, visible)
    # ── Topbar action rail (right-hand cluster) ──────────────────────
    ("hamburger",       "topbar",           10, 1),  # locked · always leftmost
    ("help",            "topbar",           20, 1),  # locked · always second
    ("walkthroughs",    "topbar",           30, 1),
    ("capture-note",    "topbar",           40, 1),
    ("flag-page",       "topbar",           50, 1),
    ("presenter-notes", "topbar",           60, 1),
    ("ai-walkthrough",  "topbar",           70, 1),
    ("ai-copilot",      "topbar",           80, 1),
    ("notifications",   "topbar",           90, 1),
    ("settings",        "topbar",          100, 1),
    # ── Topbar overflow (⋯ dropdown) ─────────────────────────────────
    ("share",           "topbar:overflow",  10, 1),
    ("boardroom",       "topbar:overflow",  20, 1),
    ("kiosk",           "topbar:overflow",  30, 1),
    ("record",          "topbar:overflow",  40, 1),
    # ── Left sidecar rail ────────────────────────────────────────────
    ("sidecar-apps",    "sidecar",          10, 1),
    ("search",          "sidecar",          20, 1),
    ("voice",           "sidecar",          30, 1),
    # ── Hamburger actions ────────────────────────────────────────────
    ("logout",          "hamburger:actions", 10, 1),
]


def _resolve_wb_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", str(_ROOT / "data")))
    return data_dir / "writeback.db"


def _row_covers_all_features() -> None:
    """Sanity — every registered feature must have a baseline placement.
    Prevents "seeded but half the app is invisible" errors."""
    seeded_keys = {row[0] for row in BASELINE}
    missing = set(NAV_FEATURES.keys()) - seeded_keys
    extra   = seeded_keys - set(NAV_FEATURES.keys())
    if missing or extra:
        raise SystemExit(
            f"[seed] BASELINE ↔ NAV_FEATURES mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def apply_seed(conn: sqlite3.Connection, wipe: bool = False, dry_run: bool = False) -> int:
    """Insert the baseline rows. Returns the number of rows written."""
    _row_covers_all_features()
    if wipe and not dry_run:
        conn.execute(
            "DELETE FROM nav_placement WHERE tenant_id='default' AND role='default'"
        )
    if dry_run:
        for feature_key, slot, slot_order, visible in BASELINE:
            print(f"  default/default  {feature_key:20} → {slot:20} order={slot_order:>3} visible={visible}")
        return len(BASELINE)
    conn.executemany(
        "INSERT OR REPLACE INTO nav_placement "
        "(tenant_id, role, feature_key, slot, slot_order, visible, updated_at) "
        "VALUES ('default', 'default', ?, ?, ?, ?, datetime('now'))",
        BASELINE,
    )
    conn.commit()
    return len(BASELINE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print rows without writing")
    ap.add_argument("--wipe",    action="store_true", help="delete existing default/default rows first")
    args = ap.parse_args(argv)

    wb_path = _resolve_wb_path()
    if not wb_path.exists():
        print(f"[seed] writeback.db not found at {wb_path}", flush=True)
        return 1

    conn = sqlite3.connect(str(wb_path))
    try:
        n = apply_seed(conn, wipe=args.wipe, dry_run=args.dry_run)
    finally:
        conn.close()

    if args.dry_run:
        print(f"[seed] DRY-RUN would insert/replace {n} rows into {wb_path}")
    else:
        print(f"[seed] inserted/replaced {n} rows into {wb_path}"
              + (" (wiped first)" if args.wipe else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
