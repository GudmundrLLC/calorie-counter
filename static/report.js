/* Meals · printable report (logic unchanged; charts styled by token classes). */
(function () {
  "use strict";
  var api = (window.CC && window.CC.api) || function (p) { return fetch(p); };
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
  function bsCls(v) { return v < 100 ? "normal" : v < 140 ? "elevated" : "high"; }
  var sel = document.getElementById("report-days");
  document.getElementById("btn-print").addEventListener("click", function () { window.print(); });
  sel.addEventListener("change", loadReport);

  async function loadReport() {
    var days = sel.value;
    try { renderReport(await (await api("/api/report-data?days=" + days)).json(), days); }
    catch (e) { document.getElementById("report-content").innerHTML = '<p class="cc-error" role="alert">Failed to load report: ' + esc(e.message) + "</p>"; }
  }

  function renderReport(data, days) {
    var el = document.getElementById("report-content");
    var daily = data.daily_summary || [], bs = data.blood_sugar || [];
    var now = new Date().toLocaleString("en-US", { dateStyle: "long", timeStyle: "short" });
    var sum = function (k) { return daily.reduce(function (s, d) { return s + (d[k] || 0); }, 0); };
    var n = daily.length || 1;
    var avgCal = Math.round(sum("cal") / n);
    var avgBS = bs.length ? Math.round(bs.reduce(function (s, r) { return s + r.value; }, 0) / bs.length) : 0;

    // calorie bars (SVG · colours from token classes)
    var maxCal = Math.max.apply(null, daily.map(function (d) { return d.cal; }).concat([1]));
    var W = 540, H = Math.max(daily.length * 28, 60);
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Daily calories: ' + esc(daily.map(function (d) { return d.entry_date + " " + Math.round(d.cal); }).join(", ")) + '">';
    daily.forEach(function (d, i) {
      var y = i * 28 + 4, w = Math.max((d.cal / maxCal) * (W - 110), 2);
      var lab = new Date(d.entry_date + "T12:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
      svg += '<text class="cc-svg-k" x="55" y="' + (y + 13) + '" text-anchor="end">' + lab + "</text>";
      svg += '<rect class="cc-svg-bar" x="62" y="' + y + '" width="' + w + '" height="18" rx="2"/>';
      svg += '<text class="cc-svg-v" x="' + (66 + w) + '" y="' + (y + 13) + '">' + Math.round(d.cal).toLocaleString() + "</text>";
    });
    svg += "</svg>";

    // blood-sugar line
    var bsSvg = "";
    if (bs.length > 1) {
      var bW = 540, bH = 120, pad = 40;
      var vals = bs.map(function (r) { return r.value; });
      var bMax = Math.max.apply(null, vals.concat([200])), bMin = Math.min.apply(null, vals.concat([60])), range = bMax - bMin || 1;
      var sorted = bs.slice().sort(function (a, b) { return a.measured_at.localeCompare(b.measured_at); });
      var pts = sorted.map(function (r, i) {
        return { x: pad + i * ((bW - 2 * pad) / (sorted.length - 1 || 1)), y: pad + (1 - (r.value - bMin) / range) * (bH - 2 * pad), v: r.value };
      });
      bsSvg = '<svg viewBox="0 0 ' + bW + " " + bH + '" role="img" aria-label="Blood sugar trend, ' + bs.length + ' readings, average ' + avgBS + ' mg/dL">';
      [70, 100, 140].forEach(function (v) {
        if (v >= bMin && v <= bMax) {
          var y = pad + (1 - (v - bMin) / range) * (bH - 2 * pad);
          bsSvg += '<line class="cc-svg-ref cc-svg--' + bsCls(v - 1) + '" x1="' + pad + '" y1="' + y + '" x2="' + (bW - pad) + '" y2="' + y + '"/>';
          bsSvg += '<text class="cc-svg-k" x="' + (pad - 4) + '" y="' + (y + 3) + '" text-anchor="end">' + v + "</text>";
        }
      });
      bsSvg += '<path class="cc-svg-line" d="' + pts.map(function (p, i) { return (i ? "L" : "M") + p.x + "," + p.y; }).join(" ") + '"/>';
      pts.forEach(function (p) { bsSvg += '<circle class="cc-svg-dot cc-svg--' + bsCls(p.v) + '" cx="' + p.x + '" cy="' + p.y + '" r="3"/>'; });
      bsSvg += "</svg>";
    }

    var dailyRows = daily.map(function (d) {
      var dt = new Date(d.entry_date + "T12:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
      return "<tr><td>" + dt + '</td><td class="cc-num">' + Math.round(d.cal).toLocaleString() + '</td><td class="cc-num">' + Math.round(d.protein || 0) +
        '</td><td class="cc-num">' + Math.round(d.carbs || 0) + '</td><td class="cc-num">' + Math.round(d.fat || 0) + '</td><td class="cc-num">' + d.cnt + "</td></tr>";
    }).join("");
    var bsRows = bs.slice(0, 30).map(function (r) {
      var dt = new Date(r.measured_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      var c = bsCls(r.value);
      return "<tr><td>" + dt + '</td><td class="cc-num"><b>' + Math.round(r.value) + '</b> <span class="cc-chip cc-chip--' + c + '">' + c + "</span></td><td>" + esc(r.context) + "</td><td>" + esc(r.notes || "") + "</td></tr>";
    }).join("");

    el.innerHTML =
      '<header class="cc-report__head"><p class="cc-eyebrow">Health tracking report</p><h2 class="cc-h2">Nutrition &amp; blood sugar</h2>' +
      '<p class="cc-faint cc-small"><b>Period:</b> ' + esc(days) + " days (" + esc(data.from_date) + " to " + esc(data.to_date) + ") · <b>Generated:</b> " + now + "</p></header>" +
      '<div class="cc-kpis cc-kpis--5">' +
        '<div class="cc-kpi"><div class="cc-kpi__v">' + avgCal.toLocaleString() + '</div><div class="cc-kpi__k">Avg daily cal</div></div>' +
        '<div class="cc-kpi"><div class="cc-kpi__v">' + Math.round(sum("protein") / n) + 'g</div><div class="cc-kpi__k">Avg protein</div></div>' +
        '<div class="cc-kpi"><div class="cc-kpi__v">' + Math.round(sum("carbs") / n) + 'g</div><div class="cc-kpi__k">Avg carbs</div></div>' +
        '<div class="cc-kpi"><div class="cc-kpi__v">' + Math.round(sum("fat") / n) + 'g</div><div class="cc-kpi__k">Avg fat</div></div>' +
        '<div class="cc-kpi"><div class="cc-kpi__v">' + (avgBS || "--") + '</div><div class="cc-kpi__k">Avg BG mg/dL</div></div></div>' +
      '<h3 class="cc-h3">Daily calorie intake</h3><div class="cc-chart">' + svg + "</div>" +
      '<div class="cc-tablewrap" tabindex="0" role="region" aria-label="Daily calorie table"><table class="cc-table"><thead><tr><th scope="col">Date</th><th scope="col" class="cc-num">Calories</th><th scope="col" class="cc-num">Protein (g)</th><th scope="col" class="cc-num">Carbs (g)</th><th scope="col" class="cc-num">Fat (g)</th><th scope="col" class="cc-num">Entries</th></tr></thead><tbody>' +
        (dailyRows || '<tr><td colspan="6" class="cc-faint">No calorie data for this period.</td></tr>') + "</tbody></table></div>" +
      (bs.length ? '<h3 class="cc-h3">Blood sugar trend</h3>' + (bsSvg ? '<div class="cc-chart">' + bsSvg + "</div>" : "") +
        '<div class="cc-tablewrap" tabindex="0" role="region" aria-label="Blood sugar readings table"><table class="cc-table"><thead><tr><th scope="col">Date/time</th><th scope="col" class="cc-num">Value (mg/dL)</th><th scope="col">Context</th><th scope="col">Notes</th></tr></thead><tbody>' + bsRows + "</tbody></table></div>" : "") +
      (avgBS ? '<p class="cc-callout">' + "<b>Blood sugar summary:</b> average " + avgBS + " mg/dL across " + bs.length + " readings. " +
        (avgBS < 100 ? "Within normal fasting range." : avgBS < 140 ? "Slightly elevated — monitor after meals." : "Elevated — consider consulting your healthcare provider.") + "</p>" : "") +
      '<footer class="cc-faint cc-small cc-report__foot">Generated ' + now + " · Calorie estimates are approximate, based on USDA FoodData Central. Blood sugar values are self-reported. This report is not medical advice.</footer>";
  }
  loadReport();
})();
