"""Idempotent boot-time migration · v2·2 phase 2c-2d · 2026-09-24.

Creates the ``nav_placement`` table that :mod:`nav_resolver` reads at
render time. Overlay contract:

    PRIMARY KEY (tenant_id, role, feature_key)

The resolver stacks matching rows for the current request:

    baseline (tenant='default', role='default')
       ↓ overlaid by
    current tenant (tenant='<tenant_id>', role='default')
       ↓ overlaid by
    current role   (tenant='<tenant_id>', role='<user_role>')

so a per-tenant admin can move features around without touching the code
registry, and a per-role slice narrows what a persona sees.

Boot integration
----------------
Callable as :func:`apply(conn)` for programmatic use — invoked from
:mod:`db` right after the writeback SQLite fallback initialises so the
table always exists when the resolver's first ``SELECT`` fires.

Also runnable standalone::

    uv run python migrations/2026_09_24_nav_placement.py

for the rare case of hand-repairing a corrupted writeback.db.

Design source: navigation-v2-execution-plan-2026-09-24.html §III·v2·2
and universal-navigation-v2-configurable-2026-09-24.html §III.

Note on filename: the leading date breaks Python's dotted-import syntax
(``2026_...`` is not a valid identifier). :mod:`db` loads this module via
:mod:`importlib.util.spec_from_file_location` — see ``db._apply_migration``.
The date-first naming is intentional (matches the design plan's dated
filenames) so future migrations sort chronologically on disk.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


DDL = """
CREATE TABLE IF NOT EXISTS nav_placement (
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    role        TEXT NOT NULL DEFAULT 'default',
    feature_key TEXT NOT NULL,
    slot        TEXT NOT NULL,
    slot_order  INTEGER NOT NULL DEFAULT 0,
    visible     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, role, feature_key)
);

-- Fast lookup by (tenant, role) so the resolver's overlay query is a
-- single sequential scan across at most a few dozen rows per request.
CREATE INDEX IF NOT EXISTS idx_nav_placement_tenant_role
    ON nav_placement(tenant_id, role);

-- Slot filter for the admin UI's per-slot listing (v2·9). Optional but
-- cheap and future-proof.
CREATE INDEX IF NOT EXISTS idx_nav_placement_slot
    ON nav_placement(slot);
"""


def apply(conn: sqlite3.Connection) -> None:
    """Create the ``nav_placement`` table + supporting indexes if absent.

    Idempotent — safe to invoke on every boot.
    """
    conn.executescript(DDL)
    conn.commit()


def apply_at(wb_path: str | Path) -> None:
    """Open ``wb_path`` and apply the migration. Convenience wrapper for
    the standalone CLI invocation."""
    conn = sqlite3.connect(str(wb_path))
    try:
        apply(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import os
    # Mirror db.DATA_DIR resolution without importing db (to avoid the
    # implicit Lakebase probe / heavy imports when this file is being run
    # by an operator poking at writeback.db in isolation).
    data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent / "data")))
    wb_path = data_dir / "writeback.db"
    if not wb_path.exists():
        print(f"[nav_placement migration] writeback.db not found at {wb_path}", flush=True)
        raise SystemExit(1)
    apply_at(wb_path)
    print(f"[nav_placement migration] applied to {wb_path}", flush=True)
