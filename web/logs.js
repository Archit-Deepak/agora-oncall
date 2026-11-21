"use strict";
// Live CDR log panel: polls /cdr once a second.

(() => {
  const el = (id) => document.getElementById(id);
  const NON_FAIL_SIP = new Set([200, 486, 487]);
  const CARRIER_LABEL = { carrier_x: "Carrier X", carrier_y: "Carrier Y", carrier_z: "Carrier Z" };
  const pct = (x) => Math.round((x || 0) * 100) + "%";
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function spark(points, thr) {
    const W = 360, H = 70, max = 1.0;
    const n = points.length || 1;
    const y = (v) => H - 4 - (v / max) * (H - 8);
    const x = (i) => (i / Math.max(1, n - 1)) * W;
    let d = "";
    points.forEach((v, i) => { if (v == null) return; d += (d ? " L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); });
    const ty = y(thr).toFixed(1);
    el("spark").innerHTML =
      `<line x1="0" x2="${W}" y1="${ty}" y2="${ty}" stroke="#da3633" stroke-dasharray="4 4" stroke-width="1" opacity=".8"/>` +
      `<text x="${W - 4}" y="${ty - 3}" fill="#ff7b72" font-size="9" text-anchor="end">alert ${pct(thr)}</text>` +
      `<path d="${d}" fill="none" stroke="#58a6ff" stroke-width="2" vector-effect="non-scaling-stroke"/>` +
      `<text x="4" y="11" fill="#8b949e" font-size="9">failure rate · 5 s buckets · last 3 min</text>`;
  }

  function render(data) {
    const s = data.stats, thr = data.threshold || 0.2;
    el("sim-status").textContent = s.running ? "streaming" : "stopped";
    el("sim-status").className = "pill " + (s.running ? "sim-on" : "sim-off");
    el("k-rate").textContent = s.calls ? pct(s.fail_rate) : "—";
    el("k-rate").className = "kbig " + (!s.calls ? "" : s.fail_rate >= thr ? "bad" : s.fail_rate >= thr / 2 ? "warn" : "good");
    el("k-calls").textContent = s.calls;
    el("k-failed").textContent = s.failed;
    el("k-thr").textContent = pct(thr);
    el("k-mode").textContent = s.mode === "fail" ? "failing" : "healthy";
    el("k-mode").className = "pill mode-" + s.mode;
    spark(s.spark || [], thr);

    el("carriers").innerHTML = Object.entries(s.per_carrier || {}).map(([k, v]) =>
      `<div class="carrier">${CARRIER_LABEL[k] || k} · ${v.calls} calls<br><b>${pct(v.fail_rate)}</b> failing` +
      `<div class="bar"><i style="width:${Math.min(100, v.fail_rate * 100)}%"></i></div></div>`).join("");

    el("causes").innerHTML = Object.entries(s.causes || {}).slice(0, 8).map(([k, n]) => {
      const code = parseInt(k, 10);
      const bad = [5350, 4340, 4160].includes(code);
      return `<span class="${bad ? "f" : ""}">${esc(k)} · ${n}</span>`;
    }).join("");

    if (!data.rows.length) return;
    el("cdr-body").innerHTML = data.rows.map((r) => {
      const cls = !NON_FAIL_SIP.has(r.sip) ? "fail" : r.sip !== 200 ? "soft" : "";
      return `<tr class="${cls}"><td>${r.initiation_time}</td><td title="${esc(r.call_uuid)}">${esc(r.call_uuid.slice(0, 8))}…</td>` +
        `<td>${esc(r.from)}</td><td>${esc(r.to)}</td><td>${CARRIER_LABEL[r.carrier] || r.carrier}</td>` +
        `<td>${r.duration}s</td><td>${r.sip}</td><td>${r.code} ${esc(r.cause)}</td><td>${esc(r.source)}</td></tr>`;
    }).join("");
  }

  async function poll() {
    try {
      const r = await fetch("/cdr?limit=80");
      render(await r.json());
    } catch (e) { /* server restarting; keep polling */ }
    setTimeout(poll, 1000);
  }
  poll();
})();
