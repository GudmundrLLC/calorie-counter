# Nav v2 · Phase 2.6 · calorie-counter · S3 · 2026-09-25

Sync of corrected primitives from `bp-demo` @ `8ad143cd`.
(Full hash: `8ad143cde40e18dfc7fb466dd093b4a48c80f5bd`)

## Files synced verbatim

- `templates/partials/ai_copilot_header.html`
- `templates/partials/dock_left.html`
- `templates/partials/global_selectors.html`
- `templates/partials/floating_chat.html`
- `templates/partials/main_menu_styles.html`

## Flagged for manual review

_(none)_

## Manual base.html port required

`templates/base.html` was NOT auto-synced. Each companion has its own
base.html shape that Phase 3 already wired to respect. Apply the following
changes from `bp-demo/templates/base.html` by hand:

- Topbar chip order (CAW spec): `capture-note · flag · presenter ·
  walkthrough-coach · … · ai-copilot · settings · help · hamburger`
- LLM/assumption chips removed from topbar (they now live in the
  `drawer-header:ai-copilot` slot, rendered by
  `partials/ai_copilot_header.html`)
- `.company-profile-bar` class added to the profile row (matching
  `partials/main_menu_styles.html`'s new padding-left rule)
- Topbar cluster iterates `nav_slots.topbar` in slot_order

Use judgment on companions whose base shape diverges (e.g. calorie-counter
has no shared base; who-am-i has orbs + hc-toggle preserved).

## Cardinal rules

- localhost only (Railway freeze still in effect)
- No `git push` from this sync
- Deploy is Phase 3.1 after CAW blesses close-out
