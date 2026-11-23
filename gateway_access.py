"""Gudmundr Gateway access claim v1 (shared by every ecosystem app).

Token claims (also returned by GET {GATEWAY_URL}/auth/userinfo):
  gw_v: 1 · gw_app: "<catalog app_id>"|null · gw_access: viewer|editor|admin|null
  gw_role: admin|member · gw_status: active|invited · gw_enforced: bool

Rules
  evaluate(claims, app_id)
    -> ("ok", access)   gw_v present, enforced, gw_app == app_id, gw_access set
    -> ("legacy", None) no gw_v (pre-v1 token) or gw_enforced false: caller
                         keeps its own allowlist (current behaviour)
    -> ("denied", None) anything else
  still_allowed(email, app_id)
    Revocation check for long sessions. Polls
    GET {GATEWAY_URL}/svc/access/apps/{app_id}/users with the service token
    (GATEWAY_SERVICE_TOKEN) at most every POLL_S seconds. Returns False only
    when a fresh list was fetched and the address is absent, suspended or has
    no access. Without a token, or when the Gateway is unreachable, it returns
    True (fail-open) so a Gateway outage cannot lock every user out.

Never logs addresses or tokens.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

ACCESS_LEVELS = ("viewer", "editor", "admin")
POLL_S = int(os.environ.get("GATEWAY_ACCESS_POLL_S", "60"))

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, str] | None]] = {}


def gateway_url() -> str:
    return os.environ.get("GATEWAY_URL", "https://gateway.gudmundr.com").rstrip("/")


def login_params(app_id: str, redirect_uri: str, state: str) -> dict:
    return {"app": app_id, "redirect_uri": redirect_uri, "state": state}


def evaluate(claims: dict | None, app_id: str) -> tuple[str, str | None]:
    if not claims:
        return "denied", None
    if claims.get("gw_v") is None:
        return "legacy", None
    # gw_enforced (per-app policy, flipped in the Gateway admin): false means
    # the Gateway is not yet authoritative for this app, so the app keeps its
    # own allowlist. Absent is treated as enforced (strict v1 reading).
    if claims.get("gw_enforced") is False:
        return "legacy", None
    access = claims.get("gw_access")
    if claims.get("gw_app") == app_id and access in ACCESS_LEVELS:
        return "ok", access
    return "denied", None


def _parse_users(body) -> dict[str, str]:
    rows = body.get("users") if isinstance(body, dict) else body
    if rows is None and isinstance(body, dict):
        rows = body.get("items")
    out: dict[str, str] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        addr = str(r.get("email") or "").strip().lower()
        status = str(r.get("status") or r.get("gw_status") or "active").lower()
        access = r.get("access") or r.get("gw_access")
        if addr and access in ACCESS_LEVELS and status not in ("suspended", "disabled", "revoked"):
            out[addr] = access
    return out


def _fetch(app_id: str) -> dict[str, str] | None:
    token = os.environ.get("GATEWAY_SERVICE_TOKEN", "").strip()
    if not token:
        return None
    req = urllib.request.Request(
        f"{gateway_url()}/svc/access/apps/{app_id}/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                return None
            return _parse_users(json.loads(r.read().decode("utf-8")))
    except Exception:  # noqa: BLE001 · fail-open
        return None


def current_access(email: str, app_id: str) -> str | None | bool:
    """Access level from the polled list; True when the list is unavailable."""
    now = time.time()
    with _lock:
        hit = _cache.get(app_id)
    if hit is None or now - hit[0] >= POLL_S:
        users = _fetch(app_id)
        with _lock:
            _cache[app_id] = (now, users)
        hit = (now, users)
    users = hit[1]
    if users is None:
        return True
    return users.get((email or "").strip().lower())


def still_allowed(email: str, app_id: str) -> bool:
    return current_access(email, app_id) is not None


# Seed payload for POST /svc/access/import (add-only). Build it inside the
# running service from the app's own allowlist; do not send until the
# Gateway side is live and the operator confirms.
def import_payload(app_id: str, source: str, users: list[tuple[str, str]]) -> dict:
    return {
        "app_id": app_id,
        "source": source,
        "users": [{"email": e.strip().lower(), "access": a} for e, a in users if e and a in ACCESS_LEVELS],
    }
