/* Meals · Tracker page (logic unchanged from the pre-DDUUI index.html; markup
 * classes are token-styled in /static/app.css). */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
  var CC = window.CC || {};
  var api = CC.api || function (p, o) { return fetch(p, o); };

  /* ── state ── */
  var viewDate = new Date();
  var qd = new URLSearchParams(location.search).get("date");
  if (qd && /^\d{4}-\d{2}-\d{2}$/.test(qd)) viewDate = new Date(qd + "T12:00");
  var today = new Date(); today.setHours(0, 0, 0, 0);
  function pad(n) { return String(n).padStart(2, "0"); }
  function isoDate(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function friendlyDate(d) {
    var t = new Date(); t.setHours(0, 0, 0, 0);
    var dd = new Date(d); dd.setHours(0, 0, 0, 0);
    var diff = Math.round((t - dd) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    return dd.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  }

  /* ── tabs (roving tabindex, arrow keys) ── */
  var tabs = $$(".cc-tab");
  function selectTab(btn, focus) {
    tabs.forEach(function (b) {
      var on = b === btn;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      document.getElementById("tab-" + b.dataset.tab).hidden = !on;
    });
    if (focus) btn.focus();
  }
  tabs.forEach(function (b, i) {
    b.addEventListener("click", function () { selectTab(b); });
    b.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        var n = (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
        selectTab(tabs[n], true);
      }
    });
  });

  /* ── result card ── */
  var resultTimer = null;
  function showResult(data) {
    var area = $("#result-area");
    var items = (data.items || []).map(function (it) {
      return '<li><span>' + esc(it.name) + ' <span class="cc-faint">' + esc(it.quantity || "") + "</span></span><b>" + esc(it.calories) + " cal</b></li>";
    }).join("");
    var conf = ["high", "medium", "low"].indexOf(data.confidence) >= 0 ? data.confidence : "medium";
    area.innerHTML = '<div class="cc-result"><div class="cc-result__head"><div class="cc-result__total">' + Math.round(data.total_calories || 0) +
      ' <small>calories</small></div><span class="cc-chip cc-chip--' + conf + '">' + conf + " confidence</span></div>" +
      '<ul class="cc-result__items">' + items + "</ul>" +
      '<p class="cc-macros">Protein <b>' + Math.round(data.total_protein_g || 0) + "g</b> · Carbs <b>" + Math.round(data.total_carbs_g || 0) +
      "g</b> · Fat <b>" + Math.round(data.total_fat_g || 0) + "g</b> · Fiber <b>" + Math.round(data.total_fiber_g || 0) + "g</b></p>" +
      (data.notes ? '<p class="cc-faint cc-small">' + esc(data.notes) + "</p>" : "") +
      (data.ai && data.ai.tier ? '<p class="cc-faint cc-small">Estimated by ' + (data.ai.tier === "granite-inproc" ? "IBM Granite in Meals" : "Gudmundr Gateway") +
        (data.ai.model ? " · " + esc(data.ai.model) : "") + "</p>" : "") + "</div>";
    clearTimeout(resultTimer);
    resultTimer = setTimeout(function () { area.innerHTML = ""; }, 10000);
  }
  function showLoading() { $("#result-area").innerHTML = '<p class="cc-loading"><span class="cc-spinner" aria-hidden="true"></span> Analyzing food…</p>'; }
  function showError(msg) { $("#result-area").innerHTML = '<div class="cc-result cc-result--error" role="alert"><b>Error</b><p>' + esc(msg) + "</p></div>"; }

  async function postEstimate(desc, type) {
    var resp = await api("/api/estimate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ description: desc, input_type: type }) });
    var data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Request failed");
    return data;
  }

  /* ── text ── */
  async function estimateText() {
    var input = $("#food-input"); var desc = input.value.trim(); if (!desc) { input.focus(); return; }
    var btn = $("#btn-estimate"); btn.disabled = true; btn.textContent = "Estimating…"; showLoading();
    try { var data = await postEstimate(desc, "text"); showResult(data); input.value = ""; refreshAll(); }
    catch (e) { showError(e.message); }
    finally { btn.disabled = false; btn.textContent = "Estimate calories"; }
  }
  $("#btn-estimate").addEventListener("click", estimateText);
  $("#food-input").addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); estimateText(); } });

  /* ── voice (server-side transcription) ── */
  (function () {
    var recBtn = $("#btn-record"), subBtn = $("#btn-voice-submit"), out = $("#transcript");
    var mediaRecorder, chunks = [], recording = false;
    function label(t) { recBtn.querySelector("span:last-child").textContent = t; }
    recBtn.addEventListener("click", async function () {
      if (recording) { mediaRecorder.stop(); return; }
      try {
        var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
        chunks = [];
        mediaRecorder.ondataavailable = function (e) { chunks.push(e.data); };
        mediaRecorder.onstop = async function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          recBtn.classList.remove("is-recording"); recBtn.setAttribute("aria-pressed", "false"); label("Record"); recording = false;
          out.textContent = "Transcribing…";
          var form = new FormData(); form.append("audio", new Blob(chunks, { type: "audio/webm" }), "recording.webm");
          try {
            var resp = await api("/api/transcribe", { method: "POST", body: form });
            var data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || "Transcription failed");
            out.textContent = data.text; subBtn.hidden = false;
          } catch (e) { out.textContent = "Error: " + e.message; }
        };
        mediaRecorder.start(); recording = true;
        recBtn.classList.add("is-recording"); recBtn.setAttribute("aria-pressed", "true"); label("Stop");
        out.textContent = "Listening…"; subBtn.hidden = true;
      } catch (e) { out.textContent = "Microphone access denied"; }
    });
    subBtn.addEventListener("click", async function () {
      var text = out.textContent.trim(); if (!text) return;
      subBtn.disabled = true; subBtn.textContent = "Estimating…"; showLoading();
      try { var data = await postEstimate(text, "voice"); showResult(data); out.textContent = ""; subBtn.hidden = true; refreshAll(); }
      catch (e) { showError(e.message); }
      finally { subBtn.disabled = false; subBtn.textContent = "Log this"; }
    });
  })();

  /* ── photo ── */
  (function () {
    var zone = $("#upload-zone"), input = $("#photo-input"), preview = $("#photo-preview");
    zone.addEventListener("dragover", function (e) { e.preventDefault(); zone.classList.add("is-over"); });
    zone.addEventListener("dragleave", function () { zone.classList.remove("is-over"); });
    zone.addEventListener("drop", function (e) { e.preventDefault(); zone.classList.remove("is-over"); if (e.dataTransfer.files.length) handlePhoto(e.dataTransfer.files[0]); });
    input.addEventListener("change", function () { if (input.files.length) handlePhoto(input.files[0]); });
    async function handlePhoto(file) {
      if (!file.type.startsWith("image/")) return;
      var reader = new FileReader();
      reader.onload = function (e) { preview.innerHTML = '<img alt="Food photo being analyzed" src="' + e.target.result + '">'; };
      reader.readAsDataURL(file);
      showLoading();
      var form = new FormData(); form.append("file", file);
      try {
        var resp = await api("/api/estimate-photo", { method: "POST", body: form });
        var data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "Upload failed");
        showResult(data); preview.innerHTML = ""; input.value = ""; refreshAll();
      } catch (e) { showError(e.message); }
    }
  })();

  /* ── blood sugar ── */
  function bsClass(v) { return v < 100 ? "normal" : v < 140 ? "elevated" : "high"; }
  $("#bs-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    var val = $("#bs-value").value; if (!val) return;
    try {
      var resp = await api("/api/blood-sugar", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value: parseFloat(val), context: $("#bs-context").value }) });
      if (!resp.ok) { var d = await resp.json(); throw new Error(d.detail || "Failed"); }
      $("#bs-value").value = ""; refreshAll();
    } catch (err) { showError(err.message); }
  });
  async function loadBloodSugar() {
    try { var resp = await api("/api/blood-sugar?days=1"); renderBloodSugar(await resp.json()); } catch (e) { /* keep last */ }
  }
  function renderBloodSugar(readings) {
    var el = $("#bs-readings");
    if (!readings.length) { el.innerHTML = '<p class="cc-empty">No readings today.</p>'; return; }
    el.innerHTML = '<ul class="cc-list">' + readings.map(function (r) {
      var v = r.value, cls = bsClass(v);
      var time = new Date(r.measured_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
      return '<li class="cc-item"><div><span class="cc-chip">' + esc(String(r.context).replace("_", " ")) + '</span> <b class="cc-bs cc-bs--' + cls + '">' + Math.round(v) +
        ' mg/dL</b> <span class="cc-chip cc-chip--' + cls + '">' + cls + '</span></div><div class="cc-item__meta"><span>' + time +
        '</span><button type="button" class="cc-btn cc-btn--icon" data-del-bs="' + esc(r.id) + '" aria-label="Delete reading ' + Math.round(v) + ' at ' + time + '">✕</button></div></li>';
    }).join("") + "</ul>";
  }

  /* ── daily log ── */
  async function loadEntries() {
    try { var resp = await api("/api/entries?date_str=" + isoDate(viewDate)); renderLog(await resp.json()); }
    catch (e) { $("#log-entries").innerHTML = '<p class="cc-empty">Failed to load entries.</p>'; }
  }
  function renderLog(entries) {
    var el = $("#log-entries");
    if (!entries.length) { el.innerHTML = '<p class="cc-empty">No entries for this day.</p>'; return; }
    var groups = {}, order = ["breakfast", "lunch", "dinner", "snack"];
    entries.forEach(function (e) { var m = order.indexOf(e.meal_type) >= 0 ? e.meal_type : "snack"; (groups[m] = groups[m] || []).push(e); });
    var html = "";
    order.forEach(function (meal) {
      var list = groups[meal]; if (!list) return;
      var total = list.reduce(function (s, e) { return s + e.total_calories; }, 0);
      html += '<div class="cc-meal"><h3 class="cc-eyebrow">' + meal + " · " + Math.round(total) + ' cal</h3><ul class="cc-list">';
      list.forEach(function (e) {
        var time = new Date(e.created_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
        var itemStr = (e.items || []).map(function (i) { return i.name + " (" + i.calories + ")"; }).join(", ");
        var type = ["text", "voice", "photo"].indexOf(e.input_type) >= 0 ? e.input_type : "text";
        html += '<li class="cc-item cc-entry"><div class="cc-entry__main"><p><span class="cc-chip cc-chip--' + type + '">' + type + "</span> " + esc(e.raw_input || "") +
          "</p>" + (itemStr ? '<p class="cc-faint cc-small">' + esc(itemStr) + "</p>" : "") +
          '<p class="cc-item__meta"><span>' + time + '</span><button type="button" class="cc-btn cc-btn--link" data-edit-time="' + esc(e.id) + '" data-created="' + esc(e.created_at) +
          '">Edit time</button></p></div><div class="cc-entry__side"><b class="cc-entry__cal">' + Math.round(e.total_calories) +
          '</b><button type="button" class="cc-btn cc-btn--icon" data-del-entry="' + esc(e.id) + '" aria-label="Delete entry ' + esc((e.raw_input || "").slice(0, 40)) + '">✕</button></div></li>';
      });
      html += "</ul></div>";
    });
    el.innerHTML = html;
  }

  document.addEventListener("click", async function (e) {
    var t = e.target.closest("[data-del-entry],[data-del-bs],[data-edit-time]");
    if (!t) return;
    if (t.dataset.delEntry) { await api("/api/entries/" + encodeURIComponent(t.dataset.delEntry), { method: "DELETE" }).catch(function () {}); refreshAll(); }
    else if (t.dataset.delBs) { await api("/api/blood-sugar/" + encodeURIComponent(t.dataset.delBs), { method: "DELETE" }).catch(function () {}); refreshAll(); }
    else if (t.dataset.editTime) { editEntryTime(t.dataset.editTime, t.dataset.created, t); }
  });

  // Retroactively re-slot an entry: entry_date drives the reporting day,
  // created_at the displayed time; both are updated together.
  function editEntryTime(id, currentIso, opener) {
    var dlg = $("#time-dialog"), input = $("#time-input");
    var cur = currentIso ? new Date(currentIso) : new Date();
    input.value = cur.getFullYear() + "-" + pad(cur.getMonth() + 1) + "-" + pad(cur.getDate()) + "T" + pad(cur.getHours()) + ":" + pad(cur.getMinutes());
    dlg.onclose = async function () {
      if (opener) opener.focus();
      if (dlg.returnValue !== "save" || !input.value) return;
      var d = new Date(input.value);
      if (isNaN(d.getTime())) return;
      var entry_date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
      var res = await api("/api/entries/" + encodeURIComponent(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entry_date: entry_date, created_at: d.toISOString() }) });
      if (!res.ok) { CC.toast && CC.toast("Update failed"); return; }
      refreshAll();
    };
    dlg.returnValue = "";
    dlg.showModal();
  }

  function setDayLabel() {
    var vd = new Date(viewDate); vd.setHours(0, 0, 0, 0);
    $("#btn-next").disabled = vd >= today;
    $("#day-h").textContent = friendlyDate(viewDate);
  }
  function changeDay(delta) { viewDate.setDate(viewDate.getDate() + delta); setDayLabel(); loadEntries(); }
  $("#btn-prev").addEventListener("click", function () { changeDay(-1); });
  $("#btn-next").addEventListener("click", function () { changeDay(1); });

  /* ── summary + history ── */
  async function loadSummary() {
    try {
      var s = await (await api("/api/summary")).json();
      $("#kpi-today").textContent = s.today.calories ? Math.round(s.today.calories).toLocaleString() : "0";
      $("#kpi-week").textContent = s.week.total ? Math.round(s.week.total).toLocaleString() : "0";
      $("#kpi-avg").textContent = s.week.daily_avg ? Math.round(s.week.daily_avg).toLocaleString() : "0";
      $("#kpi-bs").textContent = s.blood_sugar && s.blood_sugar.length ? Math.round(s.blood_sugar[0].value) : "--";
      renderHistory(s.week.days);
    } catch (e) { /* keep last */ }
  }
  function renderHistory(days) {
    var el = $("#history-chart");
    var map = {}; (days || []).forEach(function (d) { map[d.date] = d.calories; });
    var all = [];
    for (var i = 6; i >= 0; i--) { var dd = new Date(); dd.setDate(dd.getDate() - i); var ds = isoDate(dd); all.push({ date: ds, calories: map[ds] || 0 }); }
    var max = Math.max.apply(null, all.map(function (d) { return d.calories; }).concat([1]));
    var todayStr = isoDate(new Date());
    el.setAttribute("aria-label", "Calories per day, last seven days: " + all.map(function (d) { return d.date + " " + Math.round(d.calories); }).join(", "));
    el.innerHTML = all.map(function (d) {
      var pct = Math.round((d.calories / max) * 100) || 0;
      var lab = new Date(d.date + "T12:00").toLocaleDateString("en-US", { weekday: "short" });
      return '<div class="cc-bar"><span class="cc-bar__k">' + lab + '</span><span class="cc-bar__track"><span class="cc-bar__fill' + (d.date === todayStr ? " is-today" : "") +
        '" style="width:' + pct + '%"></span></span><span class="cc-bar__v">' + (d.calories ? Math.round(d.calories).toLocaleString() : "—") + "</span></div>";
    }).join("");
  }

  function refreshAll() { loadEntries(); loadSummary(); loadBloodSugar(); }
  setDayLabel();
  refreshAll();
})();
