"""Navigation slot resolver · v2·2 phase 2e-2f-2g · 2026-09-24.

Turns three inputs — tenant identifier, role identifier, current user —
into ``dict[slot, list[FeatureRow]]``. Templates iterate the dict; the
admin UI mutates the underlying ``nav_placement`` rows; per-tenant / per-
role differentiation stays declarative.

Resolution algorithm
--------------------
1. Load rows for the baseline ``(tenant='default', role='default')`` from
   ``writeback.db.nav_placement``.
2. Overlay rows for the current tenant with role='default'.
3. Overlay rows for the current tenant with the user's role.
4. Merge the resulting row set with :data:`nav_features.NAV_FEATURES`;
   drop rows whose ``feature_key`` isn't registered (stale placement).
5. Filter out features the user lacks permission for.
6. Filter out ``visible=0`` rows unless the caller asks for them (admin UI
   wants to see hidden rows to move them back into a visible slot).
7. Group by ``slot`` and sort by ``slot_order`` within each slot.

Caching
-------
In-process dict keyed by ``(tenant_id, role, permission_signature)``. Cache
values are the fully-resolved dicts. Invalidate on any write to
``nav_placement`` — callers who mutate the table MUST call
:func:`invalidate_cache` (also exposed as ``RightDrawer`` doesn't touch
this side, so no JS invalidation needed).

Sub-millisecond hits: resolver runs on every page render, so the cache is
load-bearing. Cold miss is one ``SELECT`` + Python dict work — fine.
Warm hit is a dict lookup.

Fallback: if the DB or table isn't available (rare cold-boot race,
Lakebase blip), fall back to :data:`nav_features.NAV_FEATURES` defaults so
the page still renders SOMETHING useful.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from nav_features import NAV_FEATURES, NavFeature, Slot


# Writeback DB path — resolved lazily so tests can point elsewhere via env.
def _wb_path() -> Path:
    import os
    # Mirror db.DATA_DIR resolution without importing db (keeps this
    # module cheap to import + avoids Lakebase probe side-effects).
    data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
    return data_dir / "writeback.db"


@dataclass(frozen=True)
class FeatureRow:
    """Resolved feature + placement pair. What templates iterate over."""

    feature: NavFeature
    slot: str
    slot_order: int
    visible: bool

    # Convenience passthroughs so templates don't need ``.feature.label``.
    @property
    def key(self) -> str: return self.feature.key
    @property
    def label(self) -> str: return self.feature.label
    @property
    def icon(self) -> str: return self.feature.icon
    @property
    def opens(self) -> str: return self.feature.opens
    @property
    def permission(self) -> str: return self.feature.permission


# ── Cache ────────────────────────────────────────────────────────────
_CACHE: dict[tuple[str, str, str], dict[str, list[FeatureRow]]] = {}
_CACHE_LOCK = threading.RLock()


def invalidate_cache(tenant_id: str | None = None, role: str | None = None) -> None:
    """Clear cache entries. Called on any write to nav_placement so the
    next render sees the mutation.

    * ``tenant_id=None, role=None`` — wipe everything (safest default;
      admin UI's guarantee of correctness beats micro-optimisation).
    * ``tenant_id='X', role=None`` — wipe every entry for that tenant.
    * ``tenant_id='X', role='Y'`` — wipe only that pair.
    """
    with _CACHE_LOCK:
        if tenant_id is None:
            _CACHE.clear()
            return
        to_kill = [k for k in _CACHE if k[0] == tenant_id and (role is None or k[1] == role)]
        for k in to_kill:
            _CACHE.pop(k, None)


def _permission_signature(user: Mapping | None) -> str:
    """Hash the user's permission set so the cache key includes it. A user
    with ``permissions`` including 'presenter' can't share a cached result
    with a user who lacks it. ``None`` / missing → 'all' (see permit)."""
    if user is None:
        return "all"
    perms = user.get("permissions")
    if perms is None:
        return "all"
    return ",".join(sorted(perms))


def _user_permits(user: Mapping | None, required: str) -> bool:
    """Does the user have the permission the feature requires?

    Contract:
      * ``required == 'any'`` — always True.
      * ``user is None`` OR ``user['permissions']`` missing — treat as
        having ALL permissions. Matches bp-demo's current unauth'd model
        where every visitor sees every chip. When per-user auth ships,
        callers pass a ``permissions`` set and this check tightens.
    """
    if required == "any":
        return True
    if user is None:
        return True
    perms = user.get("permissions")
    if perms is None:
        return True
    return required in perms


# ── SQL ──────────────────────────────────────────────────────────────
_OVERLAY_SQL = """
SELECT tenant_id, role, feature_key, slot, slot_order, visible
FROM nav_placement
WHERE (tenant_id = 'default' AND role = 'default')
   OR (tenant_id = ? AND role = 'default')
   OR (tenant_id = ? AND role = ?)
"""


def _fetch_overlay_rows(tenant_id: str, role: str) -> list[tuple]:
    """Load overlay rows for (tenant, role) plus the baseline. Returns
    empty list on any DB error — the resolver falls back to registry
    defaults so the render never crashes on this path."""
    wb = _wb_path()
    if not wb.exists():
        return []
    try:
        conn = sqlite3.connect(str(wb))
        try:
            cur = conn.execute(_OVERLAY_SQL, (tenant_id, tenant_id, role))
            return cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _apply_overlay(rows: Iterable[tuple], tenant_id: str, role: str) -> dict[str, tuple]:
    """Reduce raw overlay rows to one winning row per feature_key. Later
    rows in precedence order clobber earlier ones for the same key."""
    def rank(row: tuple) -> int:
        t, r, *_ = row
        if t == "default" and r == "default": return 0
        if t == tenant_id and r == "default": return 1
        if t == tenant_id and r == role:      return 2
        return -1  # ignored — shouldn't happen given WHERE clause
    winners: dict[str, tuple] = {}
    for row in sorted(rows, key=rank):
        _t, _r, feature_key, slot, slot_order, visible = row
        winners[feature_key] = (slot, slot_order, bool(visible))
    return winners


def _rows_from_defaults() -> dict[str, tuple]:
    """Fallback: build the row set from the code registry's default_slot
    values. Used when nav_placement is empty or unreachable so the app
    still has a nav layout to render."""
    return {
        f.key: (f.default_slot, 0, True)
        for f in NAV_FEATURES.values()
    }


def resolve_nav_slots(
    tenant_id: str = "default",
    role: str = "default",
    user: Mapping | None = None,
    include_hidden: bool = False,
) -> dict[str, list[FeatureRow]]:
    """Return ``{slot: [FeatureRow, ...]}`` for the given (tenant, role, user)."""
    key = (tenant_id, role, _permission_signature(user))
    # Fast-path — permission-scoped cache hit.
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and not include_hidden:
        return cached

    # Slow-path — hit DB, apply overlay, filter.
    raw = _fetch_overlay_rows(tenant_id, role)
    winners = _apply_overlay(raw, tenant_id, role)
    if not winners:
        winners = _rows_from_defaults()

    grouped: dict[str, list[FeatureRow]] = {}
    for feature_key, (slot, slot_order, visible) in winners.items():
        feature = NAV_FEATURES.get(feature_key)
        if feature is None:
            continue  # stale placement row — drop silently
        if not visible and not include_hidden:
            continue
        if not _user_permits(user, feature.permission):
            continue
        grouped.setdefault(slot, []).append(
            FeatureRow(feature=feature, slot=slot, slot_order=slot_order, visible=visible)
        )
    for rows in grouped.values():
        rows.sort(key=lambda fr: (fr.slot_order, fr.feature.key))

    # Cache only the visible variant — hidden inclusion is an admin path
    # and shouldn't pollute the render-time cache.
    if not include_hidden:
        with _CACHE_LOCK:
            _CACHE[key] = grouped
    return grouped


# Convenience — light wrapper for the template-context integration.
def slots_for_request(request) -> dict[str, list[FeatureRow]]:
    """Resolve nav slots from a FastAPI/Starlette request. Reads the
    ``tenant_id`` from the ``BP_DEMO_TENANT`` env var (mirrors db.py's
    tenant flip) and the ``role`` from session; falls back to defaults.

    Kept intentionally tiny so ``_ctx()`` in app.py can wire this in with
    a single call. Full session/user integration lands with the admin UI
    (v2·9)."""
    import os
    tenant = os.environ.get("BP_DEMO_TENANT") or "default"
    role = "default"
    user: Mapping | None = None
    try:
        session = getattr(request, "session", None)
        if session:
            role = session.get("nav_role", role) or role
            perms = session.get("nav_permissions")
            if perms is not None:
                user = {"permissions": list(perms)}
    except Exception:
        pass
    return resolve_nav_slots(tenant_id=tenant, role=role, user=user)
