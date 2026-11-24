/* Meals · host glue for the Gateway DDUUI kit 1.0.2 (chrome.js owns the
 * rails, both pins and the viewer's appearance; this file only uses its
 * public runtime API).
 *
 *  - DDUUI.setUser (turns on per-user sync) · setContext · config.onSignOut
 *  - High-contrast preference (a11y · per viewer · localStorage): not one of
 *    the kit's six appearance axes yet, so it stays app-side.
 */
(function () {
  "use strict";
  var root = document.documentElement;
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { return null; } }
  function readJSON(id) { try { return JSON.parse(document.getElementById(id).textContent || "null"); } catch (_) { return null; } }

  if (store("cc.hc") === "1") root.classList.add("cc-hc");

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

  // AI route for the topbar context slot (kit 1.0.2 setContext).
  function aiContext(ai) {
    var granite = ai.granite === "ready" ? "Granite in app" : ai.granite === "downloading" || ai.granite === "loading" ? "Granite starting" : "";
    var parts = [];
    if (granite) parts.push(granite);
    if (ai.gateway) parts.push("Gateway");
    return parts.length ? "AI: " + parts.join(" + ") : "AI not available";
  }

  var D = window.DDUUI;
  if (D) {
    D.config.onSignOut = signOut;
    if (CC.user) D.setUser({ name: CC.user.name, email: CC.user.email, idp: CC.user.idp });
    else D.setUser(null);
    if (CC.user && D.setContext) D.setContext(aiContext(CC.ai));
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
})();
