/* Meals · host glue for the Gateway DDUUI chrome (chrome.js is the kit; this
 * file only uses its public runtime API and adds app-side preferences).
 *
 *  - DDUUI.setUser / setStatus / config.onSignOut
 *  - High-contrast preference (a11y · per viewer · localStorage)
 *  - Rail pin (F2026-11-22-LEFTRAIL-PIN): left rail pinned = labelled rail
 *    that takes layout space; unpinned = icon rail. Right rail pinned =
 *    icon rail in layout; unpinned = collapsed (ecosystem stays reachable
 *    from the topbar). Persisted in localStorage, applied on every page.
 *    Kit request filed: native pin support in chrome.js.
 */
(function () {
  "use strict";
  var root = document.documentElement;
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { return null; } }
  function readJSON(id) { try { return JSON.parse(document.getElementById(id).textContent || "null"); } catch (_) { return null; } }

  // Preferences apply before first paint of the app content.
  if (store("cc.hc") === "1") root.classList.add("cc-hc");
  root.setAttribute("data-cc-left", store("cc.rail.left") || "auto");
  root.setAttribute("data-cc-right", store("cc.rail.right") || "pinned");

  var CC = window.CC = window.CC || {};
  CC.user = readJSON("cc-user");
  CC.ai = readJSON("cc-ai") || {};

  CC.api = function (path, opts) {
    return fetch(path, Object.assign({ credentials: "same-origin" }, opts || {})).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("Not signed in"); }
      return r;
    });
  };
  CC.toast = function (msg) {
    var t = document.getElementById("cc-toast"); if (!t) return;
    t.textContent = msg; t.hidden = false;
    clearTimeout(CC.toast._t); CC.toast._t = setTimeout(function () { t.hidden = true; }, 2600);
  };

  function signOut() {
    return fetch("/api/logout", { method: "POST", credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .catch(function () { return {}; })
      .then(function (d) {
        // Gateway sessions also end the ecosystem session (single sign-out).
        location.href = d && d.gateway_logout ? d.gateway_logout : "/login";
        return false;
      });
  }

  var D = window.DDUUI;
  if (D) {
    D.config.onSignOut = signOut;
    if (CC.user) D.setUser({ name: CC.user.name, email: CC.user.email, idp: CC.user.idp });
    else D.setUser(null);
    var route = { gateway: "AI via Gudmundr Gateway", "xai-fallback": "AI via fallback route", none: "AI not configured" }[CC.ai.route] || "";
    if (CC.user) D.setStatus(route);
  }

  // ── high contrast toggle(s)
  function syncHc() {
    var on = root.classList.contains("cc-hc");
    document.querySelectorAll("[data-cc-hc]").forEach(function (b) { b.setAttribute("aria-pressed", String(on)); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-cc-hc]");
    if (!b) return;
    var on = root.classList.toggle("cc-hc");
    store("cc.hc", on ? "1" : "0");
    syncHc();
  });
  syncHc();

  // ── rail pins
  var PIN_ICON = '<svg class="dd-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>';
  function leftPinned() {
    var v = root.getAttribute("data-cc-left");
    if (v === "pinned") return true;
    if (v === "unpinned") return false;
    return root.getAttribute("data-viewport") === "desktop";
  }
  function syncPins() {
    var lp = document.querySelector("[data-cc-pin='left']");
    var rp = document.querySelector("[data-cc-pin='right']");
    var lOn = leftPinned(), rOn = root.getAttribute("data-cc-right") !== "unpinned";
    root.classList.toggle("cc-left-pinned", lOn);
    root.classList.toggle("cc-left-unpinned", !lOn);
    if (lp) { lp.setAttribute("aria-pressed", String(lOn)); lp.setAttribute("aria-label", lOn ? "Unpin left rail" : "Pin left rail"); lp.querySelector(".dd-menu-label").textContent = lOn ? "Unpin rail" : "Pin rail"; }
    if (rp) { rp.setAttribute("aria-pressed", String(rOn)); rp.setAttribute("aria-label", rOn ? "Unpin right rail" : "Pin right rail"); }
  }
  function mountPins() {
    var left = document.querySelector(".cs-rail-left");
    var right = document.querySelector(".cs-rail-right");
    var top = document.querySelector(".cs-topbar__right");
    if (left && !left.querySelector("[data-cc-pin]")) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "cc-pin cc-pin--left"; b.setAttribute("data-cc-pin", "left");
      b.innerHTML = PIN_ICON + '<span class="dd-menu-label">Pin rail</span>';
      left.appendChild(b);
    }
    if (right && !right.querySelector("[data-cc-pin]")) {
      var r = document.createElement("button");
      r.type = "button"; r.className = "dd-ibtn cc-pin cc-pin--right"; r.setAttribute("data-cc-pin", "right");
      r.innerHTML = PIN_ICON;
      right.insertBefore(r, right.querySelector(".cs-rail-right__spacer"));
    }
    // Right rail collapsed: a topbar button restores it (ecosystem/appearance stay one tap away).
    if (top && !top.querySelector("[data-cc-pin='right-restore']")) {
      var t = document.createElement("button");
      t.type = "button"; t.className = "dd-ibtn cc-pin-restore"; t.setAttribute("data-cc-pin", "right-restore");
      t.setAttribute("aria-label", "Pin right rail");
      t.innerHTML = PIN_ICON;
      top.insertBefore(t, top.firstChild);
    }
    syncPins();
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-cc-pin]");
    if (!b) return;
    var which = b.getAttribute("data-cc-pin");
    if (which === "left") {
      var next = leftPinned() ? "unpinned" : "pinned";
      root.setAttribute("data-cc-left", next); store("cc.rail.left", next);
    } else {
      var cur = root.getAttribute("data-cc-right") !== "unpinned";
      var nv = which === "right-restore" ? "pinned" : (cur ? "unpinned" : "pinned");
      root.setAttribute("data-cc-right", nv); store("cc.rail.right", nv);
    }
    syncPins();
  });
  new MutationObserver(syncPins).observe(root, { attributes: true, attributeFilter: ["data-viewport"] });
  if (document.querySelector(".dd-app")) mountPins();
  else document.addEventListener("DOMContentLoaded", mountPins);
})();
