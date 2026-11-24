"""Meals (calorie_counter) · DDUUI chrome + Gateway SSO end-to-end checks.

Run (macOS 12):
  CC_BASE=http://localhost:18734 CC_E2E_EMAIL=... CC_E2E_PIN=... \
  uv run --no-project --with playwright==1.44.0 python tests/e2e/dduui_chrome_e2e.py

Env:
  CC_BASE             app origin (local or https://meals.gudmundr.com)
  CC_E2E_EMAIL/PIN    an allowed account + the CC_FALLBACK_PIN (read from
                      Keychain by the caller; never printed)
  CC_E2E_GATEWAY_DEV  1 = the Gateway behind the app runs its dev sign-in
                      form, so the SSO round trip is completed end to end;
                      otherwise the test follows SSO to the Gateway and the
                      Gateway's Google hop (no Google automation).
  CC_E2E_BROWSERS     comma list: chromium,webkit (default both)
  CC_E2E_SHOTS        directory for screenshots (optional)

Checks per page × {desktop Chromium 1440x900, iPhone 14 WebKit} × {light, dark}:
axe 0 serious/critical · horizontal overflow 0px · phone tap targets ≥44px ·
no console errors. Plus: unauthenticated 401/redirect, SSO round trip, rail
pin persists across reload, every original feature reachable from the menu.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CC_BASE", "http://localhost:18734").rstrip("/")
EMAIL = os.environ.get("CC_E2E_EMAIL", "")
PIN = os.environ.get("CC_E2E_PIN", "")
GW_DEV = os.environ.get("CC_E2E_GATEWAY_DEV") == "1"
BROWSERS = [b.strip() for b in os.environ.get("CC_E2E_BROWSERS", "chromium,webkit").split(",") if b.strip()]
SHOTS = os.environ.get("CC_E2E_SHOTS", "")
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"

PAGES = [("tracker", "/"), ("journal", "/journal"), ("report", "/report"), ("search", "/search?q=salmon"), ("help", "/help")]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" · {detail}" if detail and not ok else ""), flush=True)


def ignorable_console(msg: str) -> bool:
    # Media permission prompts are not errors of the page itself.
    return "NotAllowedError" in msg


def overflow_px(page) -> int:
    return page.evaluate("""() => {
      const d = document.scrollingElement || document.documentElement;
      const m = document.querySelector('.cs-main');
      const a = d.scrollWidth - d.clientWidth;
      const b = m ? m.scrollWidth - m.clientWidth : 0;
      return Math.max(a, b, 0);
    }""")


def small_targets(page) -> list[str]:
    return page.evaluate("""() => {
      const sel = 'button, input:not([type=hidden]), select, textarea, summary, [role=tab], a';
      const out = [];
      for (const el of document.querySelectorAll(sel)) {
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (el.closest('.cc-sr') || el.classList.contains('cc-sr')) continue;
        // WCAG 2.5.8 inline exception: links inside running text.
        if (el.tagName === 'A' && cs.display === 'inline') continue;
        if (el.closest('dialog:not([open])')) continue;
        if (Math.round(r.height) < 44 || Math.round(r.width) < 44)
          out.push((el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 30) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
      return out;
    }""")


def axe_serious(page) -> list[str]:
    if not page.evaluate("() => !!window.axe"):
        page.add_script_tag(url=AXE)
    res = page.evaluate("""async () => {
      const r = await axe.run(document, { resultTypes: ['violations'] });
      return r.violations.filter(v => ['serious','critical'].includes(v.impact))
        .map(v => v.id + ' x' + v.nodes.length + ' ' + (v.nodes[0] && v.nodes[0].target.join(' ')));
    }""")
    return res


def set_mode(page, mode: str) -> None:
    page.evaluate("m => { localStorage.setItem('dduui.mode', m); }", mode)
    page.reload(wait_until="networkidle")


def sign_in_pin(page) -> None:
    page.goto(BASE + "/login", wait_until="networkidle")
    page.locator("details.cc-fallback summary").click()
    page.fill("input[name=email]", EMAIL)
    page.fill("input[name=pin]", PIN)
    page.click("text=Sign in with PIN")
    page.wait_for_url(BASE + "/", timeout=20000)


def unauth_checks() -> None:
    for path, want in (("/api/entries", 401), ("/api/summary", 401), ("/api/export", 401), ("/api/ecosystem/my-apps", 401)):
        try:
            urllib.request.urlopen(BASE + path, timeout=15)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check(f"unauth {path} -> {want}", code == want, f"got {code}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    for path in ("/", "/journal", "/report", "/search", "/help"):
        try:
            r = op.open(BASE + path, timeout=15)
            code, loc = r.status, ""
        except urllib.error.HTTPError as e:
            code, loc = e.code, e.headers.get("location", "")
        check(f"unauth page {path} -> redirect /login", code in (302, 303) and loc.endswith("/login"), f"{code} {loc}")
    try:
        body = urllib.request.urlopen(BASE + "/healthz", timeout=15).read().decode()
        check("/healthz public", '"ok"' in body)
    except Exception as e:  # noqa: BLE001
        check("/healthz public", False, str(e))


def run_browser(pw, kind: str) -> None:
    if kind == "chromium":
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        label = "desktop-chromium"
    else:
        browser = pw.webkit.launch()
        ctx = browser.new_context(**pw.devices["iPhone 14"])
        label = "iphone14-webkit"
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not ignorable_console(m.text) else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # ── SSO round trip (signed out → Gateway → back signed in)
    page.goto(BASE + "/login", wait_until="networkidle")
    check(f"{label} login renders in DDUUI chrome", page.locator(".dd-app .cs-topbar").count() == 1)
    page.click("[data-testid=gateway-login]")
    if GW_DEV:
        page.wait_for_selector("form[action='/auth/callback']", timeout=20000)
        page.fill("input[name=sub]", "ssotester")
        page.click("button[type=submit]")
        page.wait_for_url(BASE + "/", timeout=20000)
        who = page.evaluate("() => (window.CC && CC.user && CC.user.idp) || ''")
        check(f"{label} SSO round trip signed in via gateway", who == "gateway", who)
        page.goto(BASE + "/api/me")
        check(f"{label} SSO session /api/me 200", '"idp":"gateway"' in page.content())
        # sign out (ends the Gateway session too) → back at /login
        page.goto(BASE + "/", wait_until="networkidle")
        page.evaluate("() => DDUUI.openPanel('account')")
        page.click("[data-dd-signout]")
        page.wait_for_url("**/login*", timeout=20000)
        check(f"{label} sign-out returns to /login", "/login" in page.url)
    else:
        page.wait_for_url("**accounts.google.com/**", timeout=30000)
        check(f"{label} SSO hands off to Gateway → Google", "accounts.google.com" in page.url)
        page.goto(BASE + "/login", wait_until="networkidle")

    if not (EMAIL and PIN):
        check(f"{label} PIN credentials provided", False, "CC_E2E_EMAIL/CC_E2E_PIN unset")
        browser.close()
        return
    sign_in_pin(page)
    check(f"{label} signed in (PIN fallback)", page.evaluate("() => !!(window.CC && CC.user)"))

    # ── menu: every original feature reachable
    menu = page.locator(".cs-rail-left [data-dduui-menu] > a")
    hrefs = [menu.nth(i).get_attribute("href") for i in range(menu.count())]
    for want in ("/", "/journal", "/report", "/search", "/api/export?days=30", "/help"):
        check(f"{label} menu has {want}", want in hrefs, str(hrefs))
    for sel in ("#tabbtn-text", "#tabbtn-voice", "#tabbtn-photo", "#bs-form", "#btn-prev", "#history-chart", "#food-input"):
        check(f"{label} tracker has {sel}", page.locator(sel).count() == 1)
    page.click("#tabbtn-photo")
    check(f"{label} photo tab shows upload", page.locator("#upload-zone").is_visible())
    page.click("#tabbtn-text")
    page.wait_for_selector("#log-entries .cc-item, #log-entries .cc-empty", timeout=15000)
    check(f"{label} daily log renders", page.locator("#log-entries .cc-item").count() > 0 or page.locator("#log-entries .cc-empty").count() == 1)
    if kind == "webkit":
        check(f"{label} phone dock shows 4 + More", page.locator(".cs-rail-left [data-dd-dock]").count() == 4 and page.locator(".dd-more").is_visible())

    # ── per page × mode
    for name, path in PAGES:
        page.goto(BASE + path, wait_until="networkidle")
        if name == "report":
            page.wait_for_selector(".cc-report__head, .cc-error", timeout=15000)
        for mode in ("light", "dark"):
            set_mode(page, mode)
            if name == "report":
                page.wait_for_selector(".cc-report__head, .cc-error", timeout=15000)
            page.wait_for_timeout(400)
            v = axe_serious(page)
            check(f"{label} {name} {mode} axe 0 serious/critical", not v, "; ".join(v))
            ov = overflow_px(page)
            check(f"{label} {name} {mode} horizontal overflow 0px", ov == 0, f"{ov}px")
            if kind == "webkit":
                st = small_targets(page)
                check(f"{label} {name} {mode} tap targets ≥44px", not st, "; ".join(st[:6]))
            if SHOTS:
                os.makedirs(SHOTS, exist_ok=True)
                page.screenshot(path=os.path.join(SHOTS, f"after_{label}_{name}_{mode}.png"), full_page=False)
    set_mode(page, "auto")

    # ── rail pin persists across reload (tablet/desktop only)
    if kind == "chromium":
        page.goto(BASE + "/", wait_until="networkidle")
        before = page.evaluate("() => document.documentElement.classList.contains('cc-left-pinned')")
        page.click("[data-cc-pin='left']")
        page.reload(wait_until="networkidle")
        after = page.evaluate("() => document.documentElement.classList.contains('cc-left-pinned')")
        w = page.evaluate("() => Math.round(document.querySelector('.cs-rail-left').getBoundingClientRect().width)")
        check(f"{label} left rail pin toggles + persists across reload", before != after, f"{before}->{after} width {w}")
        main_left = page.evaluate("() => Math.round(document.querySelector('.cs-main').getBoundingClientRect().left)")
        check(f"{label} view pane starts at rail edge (left-justified)", abs(main_left - w) <= 1, f"main {main_left} rail {w}")
        page.click("[data-cc-pin='left']")
        page.click("[data-cc-pin='right']")
        page.reload(wait_until="networkidle")
        rhidden = page.evaluate("() => getComputedStyle(document.querySelector('.cs-rail-right')).display === 'none'")
        check(f"{label} right rail unpin persists across reload", rhidden)
        page.click("[data-cc-pin='right-restore']")
        page.reload(wait_until="networkidle")
        check(f"{label} right rail re-pinned", page.evaluate("() => getComputedStyle(document.querySelector('.cs-rail-right')).display !== 'none'"))
        page.evaluate("() => { DDUUI.openPanel('eco'); }")
        page.wait_for_timeout(2500)
        apps = page.locator("[data-dd-apps] li a").count()
        check(f"{label} ecosystem panel lists per-user apps via catalogUrl", apps > 0, f"{apps} apps")

    check(f"{label} no console errors", not errors, "; ".join(errors[:5]))
    browser.close()


def main() -> int:
    unauth_checks()
    with sync_playwright() as pw:
        for b in BROWSERS:
            run_browser(pw, b)
    passed = sum(1 for _, ok, _ in results if ok)
    print(json.dumps({"base": BASE, "passed": passed, "total": len(results),
                      "by_browser": {lab: [sum(1 for n, ok, _ in results if lab in n and ok), sum(1 for n, _, _ in results if lab in n)]
                                     for lab in ("desktop-chromium", "iphone14-webkit")}}))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
