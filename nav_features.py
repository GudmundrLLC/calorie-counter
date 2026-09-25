"""Navigation feature registry · v2·2 phase 2a-2b · 2026-09-24.

Every navigable "thing" in the app carries a stable key. Features are
authored in code (they need Python handlers, JS bindings, template macros);
their *placement* — which slot they land in for a given tenant/role — is
authored as rows in ``writeback.db.nav_placement`` and resolved at request
time by :mod:`nav_resolver`.

Rationale
---------
* Boundary is right: features are code, placement is data. Adding a new
  feature ships as a normal PR + Railway rebuild. Moving an existing
  feature between slots is a single ``UPDATE`` in ``nav_placement`` — no
  deploy, no template edit, live via ``/admin/nav-config`` in v2·9.
* Same registry across the fleet — v2·10 rollout copies this file into
  each companion so tenant-level placement rows apply consistently.

See design source:
  AIGenerate_HTML/HTMLFiles/2. Concepts/BP-Demo-Audit/
  navigation-v2-execution-plan-2026-09-24.html §III·v2·2 and
  universal-navigation-v2-configurable-2026-09-24.html §III.

The registry MUST stay pure Python (no side effects on import) so migrations
and tests can import it cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Opens-mode taxonomy. Aligned with the visual primitives shipped in v2·1
# and the sidecar bridge modes managed by ecosystem-bridge.js (v2·5, S2).
OpensMode = Literal[
    "popup",     # small anchored popover (chip-adjacent form / capture note)
    "drawer",    # shared right-drawer (Settings / AI Copilot / Presenter)
    "menu",      # dropdown menu (hamburger / sidecar-apps switcher)
    "palette",   # command palette (⌘K)
    "page",      # full-page navigation
    "toggle",    # boolean UI mode toggle (boardroom / kiosk / voice)
    "action",    # fire-and-forget (logout / record-start / share)
]

# Permission gates. Enforced in ``resolve_nav_slots`` at render time so a
# user without the permission never sees the feature, even if it's placed
# in a slot. Keep this coarse — role/persona lives in ``nav_placement.role``
# so per-tenant permission overrides remain a placement concern.
Permission = Literal[
    "any",
    "presenter",
    "tester",
    "admin",
]

# Slot taxonomy — the v1 10-slot skyline expressed as string constants. The
# resolver groups + sorts feature rows by slot. Templates iterate the group.
# Layer 3 (S2's slave-adapter, v2·4) filters slots per master/slave shape.
Slot = Literal[
    "topbar",                    # right-hand action row on main topbar
    "topbar:overflow",           # ⋯ dropdown in the topbar
    "sidecar",                   # left-rail sidecar-apps launcher rail
    "hamburger",                 # ≡ menu grouped by section
    "hamburger:apps",            # apps subsection of the hamburger
    "hamburger:actions",         # actions subsection of the hamburger
    "drawer-header:ai-copilot",  # v2·7 · LLM + assumption chips slot
    "footer",                    # rare · reserved for compliance links
    "hidden",                    # placed but not visible (permission-off)
]


@dataclass(frozen=True)
class NavFeature:
    """A single navigable feature. Frozen so the registry can't be mutated
    at runtime — placement changes go through the DB, not this dict."""

    key: str
    label: str
    icon: str           # Lucide icon name (resolved by partials/icon.html)
    opens: OpensMode
    permission: Permission
    default_slot: Slot


# Registry — ~20 features covering everything the v1 skyline surfaced plus
# the drawer + sidecar-apps introduced in v2·1. Kept alphabetical by key
# so diffs stay readable when new features arrive.
NAV_FEATURES: dict[str, NavFeature] = {
    f.key: f
    for f in [
        NavFeature("ai-copilot",         "AI Copilot",             "sparkles",       "drawer",  "any",       "topbar"),
        NavFeature("ai-walkthrough",     "AI walkthrough coach",   "graduation-cap", "drawer",  "presenter", "topbar"),
        NavFeature("assumption-health",  "Assumption health",      "activity",       "page",    "any",       "drawer-header:ai-copilot"),
        NavFeature("boardroom",          "Boardroom mode",         "presentation",   "toggle",  "presenter", "topbar:overflow"),
        NavFeature("capture-note",       "Capture note",           "sticky-note",    "popup",   "presenter", "topbar"),
        NavFeature("family",             "Family",                 "users",          "drawer",  "any",       "sidecar"),
        NavFeature("flag-page",          "Flag page",              "flag",           "popup",   "tester",    "topbar"),
        NavFeature("hamburger",          "Menu",                   "menu",           "menu",    "any",       "topbar"),
        NavFeature("help",               "Help",                   "help-circle",    "page",    "any",       "topbar"),
        NavFeature("ideas",              "Ideas",                  "lightbulb",      "drawer",  "any",       "sidecar"),
        NavFeature("kiosk",              "Kiosk mode",             "monitor",        "toggle",  "presenter", "sidecar"),
        NavFeature("llm-indicator",      "LLM status",             "cpu",            "popup",   "any",       "drawer-header:ai-copilot"),
        NavFeature("logout",             "Sign out",               "log-out",        "action",  "any",       "sidecar"),
        NavFeature("notes",              "Notes",                  "file-text",      "drawer",  "any",       "sidecar"),
        NavFeature("notifications",      "Notifications",          "bell",           "drawer",  "any",       "topbar"),
        NavFeature("pipeline",           "Pipeline",               "target",         "drawer",  "any",       "sidecar"),
        NavFeature("presenter-notes",    "Presenter notes",        "notebook-pen",   "drawer",  "presenter", "topbar"),
        NavFeature("record",             "Record session",         "circle-dot",     "toggle",  "presenter", "sidecar"),
        NavFeature("search",             "Search",                 "search",         "palette", "any",       "sidecar"),
        NavFeature("settings",           "Settings",               "settings",       "drawer",  "any",       "topbar"),
        NavFeature("share",              "Share view",             "share-2",        "popup",   "any",       "sidecar"),
        NavFeature("sidecar-apps",       "Sidecar apps",           "layout-grid",    "menu",    "any",       "sidecar"),
        NavFeature("voice",              "Voice command",          "mic",            "toggle",  "any",       "sidecar"),
        NavFeature("walkthroughs",       "Walkthroughs",           "route",          "menu",    "any",       "topbar"),
    ]
}


# Convenience — read-only view for template context / resolver use.
def get(key: str) -> NavFeature | None:
    """Look up a feature by key. Returns None (never raises) for unknown
    keys so a stale placement row can't crash the render — the resolver
    logs + skips instead."""
    return NAV_FEATURES.get(key)


def all_features() -> list[NavFeature]:
    """Ordered list of every registered feature. Deterministic (dict-key
    insertion order preserved since Python 3.7) so tests + admin UI show
    features in a stable sequence."""
    return list(NAV_FEATURES.values())


# Exported for the migration seed script and the admin UI so both point at
# the same set of known slot names when validating input.
KNOWN_SLOTS: tuple[Slot, ...] = (
    "topbar",
    "topbar:overflow",
    "sidecar",
    "hamburger",
    "hamburger:apps",
    "hamburger:actions",
    "drawer-header:ai-copilot",
    "footer",
    "hidden",
)


# Guardrail set — features that MUST always render in the topbar (never
# demoted to hidden, never moved off the main topbar). Enforced by the
# admin UI (v2·9) so a config edit can't orphan critical wayfinding.
LOCKED_TO_TOPBAR: frozenset[str] = frozenset({"hamburger", "help"})
