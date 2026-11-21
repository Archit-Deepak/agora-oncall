"use strict";
// AI On-Call Engineer — v2 console. Vanilla JS, no frameworks.
// Live mode: SSE /state/stream + polling /fleet and /cdr.  Fixture mode (?fixture=1): fixtures/*.json.

(() => {
  const FIXTURE = new URLSearchParams(location.search).get("fixture") === "1";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x) => (x == null || isNaN(x) ? "—" : Math.round(x * 100) + "%");
  const inr = (x) => "₹" + Math.round(x || 0).toLocaleString("en-IN");

  const FAULT_LABELS = {
    carrier_reject: "Carrier refusing calls",
    cps_burst: "Dialer CPS burst",
    silent_route: "Silent calls on a route",
    pbx_timeout: "Customer PBX not answering",
    anchoring_4590: "India anchoring (4590)",
    acl_drop: "Calls vanishing (IP ACL)",
  };
  const SOFT_CODES = new Set([4410, 6030, 4550, 4010, 4020]);  // busy / no answer / cancel — not failures
  const STEPS = ["Suspected", "Calling", "On call", "Found", "Fix applied", "Verified"];
  const BADGE = {
    IDLE: "quiet", MONITORING: "quiet", AUTO_RECOVERED: "warn", SUSPECTED: "warn", UNSURE_TOLD: "warn",
    WAITING_FOR_EVIDENCE: "warn", ALERTED: "hot", CALLING: "hot", RETRY: "hot", TELEGRAM_SENT: "hot",
    IN_CALL: "live", CONFIRM_ACTIONS: "live", EXECUTING: "live", VERIFYING: "live", RESOLVED: "done",
  };

  const S = { kase: null, fleet: null, cdr: [], selected: null, userPicked: false,
    logShown: 0, logKey: "", seenCdr: new Set(), fixtures: null };

  // ── network ────────────────────────────────────────────────────────────
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 2200);
  }

  async function getJSON(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error(path + " → " + r.status);
    return r.json();
  }

  async function post(path) {
    if (FIXTURE) { fixturePost(path); return; }
    try {
      const r = await fetch(path, { method: "POST" });
      if (!r.ok) toast("POST " + path.split("?")[0] + " failed: " + r.status);
    } catch (e) { toast("Backend unreachable"); }
  }

  // ── top bar ────────────────────────────────────────────────────────────
  function renderTop() {
    const c = S.kase || {}, st = c.state || "IDLE";
    const b = $("state-badge");
    b.textContent = st.replace(/_/g, " ");
    b.className = "badge " + (BADGE[st] || "quiet");
    const active = c.account_id && !["IDLE", "MONITORING"].includes(st);
    $("inc-id").textContent = active ? c.incident_id || "" : "no open incident";
    const acct = active && S.fleet && S.fleet.accounts.find((a) => a.id === c.account_id);
    $("inc-account").textContent = active ? (c.alert && c.alert.customer_name) || (acct && acct.name) || c.account_id : "";
    const running = S.fleet && S.fleet.running;
    $("live-dot").className = "dot" + (running ? " on" : "");
    $("fleet-status").textContent = running ? "streaming" : "stopped";
    $("fleet-status").className = "pill " + (running ? "on" : "off");
  }

  // ── accounts ───────────────────────────────────────────────────────────
  function sparkPath(points, W, H) {
    const n = points.length || 1;
    let d = "";
    points.forEach((v, i) => {
      if (v == null) return;
      const x = (i / Math.max(1, n - 1)) * W, y = H - 2 - Math.min(1, v) * (H - 4);
      d += (d ? " L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    });
    return d;
  }

  function accountCard(a) {
    const div = document.createElement("div");
    div.className = "acct";
    div.dataset.id = a.id;
    const opts = Object.keys(FAULT_LABELS).filter((f) => !S.fleet.faults || S.fleet.faults.includes(f))
      .map((f) => `<option value="${esc(f)}">${esc(FAULT_LABELS[f])}</option>`).join("");
    div.innerHTML = `<div class="acct-body"></div>
      <div class="acct-ctl"><select>${opts}</select><button class="danger inj">Inject</button><button class="ok heal">Healthy</button></div>`;
    div.addEventListener("click", (e) => {
      if (e.target.closest(".acct-ctl")) return;
      S.selected = a.id; S.userPicked = true; S.seenCdr.clear();
      renderAccounts(); refreshCdr();
    });
    div.querySelector(".inj").addEventListener("click", () => {
      const f = div.querySelector("select").value;
      post(`/fleet/fault?account=${encodeURIComponent(a.id)}&fault=${encodeURIComponent(f)}`);
      toast(`Injecting "${FAULT_LABELS[f]}" on ${a.name}`);
    });
    div.querySelector(".heal").addEventListener("click", () => {
      post(`/fleet/fault?account=${encodeURIComponent(a.id)}&fault=none`);
      toast(`${a.name} → healthy`);
    });
    return div;
  }

  function renderAccounts() {
    const F = S.fleet;
    if (!F) return;
    const box = $("accounts"), th = F.thresholds || {}, minSpend = F.call_min_spend_inr || 20000;
    const accts = [...F.accounts].sort((x, y) => y.spend_per_day_inr - x.spend_per_day_inr);
    if (!S.selected || !accts.some((a) => a.id === S.selected)) S.selected = (accts[0] || {}).id;
    const inc = S.kase && !["IDLE", "MONITORING"].includes(S.kase.state) ? S.kase.account_id : null;
    if (inc && !S.userPicked) S.selected = inc;
    accts.forEach((a, i) => {
      let card = box.querySelector(`.acct[data-id="${CSS.escape(a.id)}"]`) || accountCard(a);
      if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
      card.classList.toggle("selected", a.id === S.selected);
      card.classList.toggle("incident", a.id === inc);
      const phone = a.spend_per_day_inr >= minSpend;
      const fcls = a.fail_rate >= (th.fail_rate || 0.2) ? "bad" : a.fail_rate >= (th.fail_rate || 0.2) / 2 ? "warn" : "";
      const scls = a.silent_rate >= (th.silent_rate || 0.15) ? "bad" : "";
      const vcls = a.volume_ratio < (th.volume_ratio || 0.5) ? "bad" : "";
      card.querySelector(".acct-body").innerHTML = `
        <div class="acct-top"><span class="acct-name">${esc(a.name)}</span><span class="acct-spend">${inr(a.spend_per_day_inr)}<span class="muted small">/day</span></span></div>
        <div class="acct-use">${esc(a.use_case)}</div>
        <div class="acct-stats">
          <div>fail<b class="${fcls}">${a.calls ? pct(a.fail_rate) : "—"}</b></div>
          <div>silent<b class="${scls}">${a.answered ? pct(a.silent_rate) : "—"}</b></div>
          <div>volume<b class="${vcls}">${pct(a.volume_ratio)}</b></div>
        </div>
        <div class="acct-risk"><span>at risk <b class="${a.at_risk_inr_per_hour > 0 ? "bad" : "ok"}">${inr(a.at_risk_inr_per_hour)}/h</b></span>
          <span>${a.fault ? `<span class="chip fault" title="since ${esc(a.fault_since || "")}">⚠ ${esc(FAULT_LABELS[a.fault] || a.fault)}</span> ` : ""}<span class="chip ${phone ? "phone" : "tg"}">${phone ? "📞 phone tier" : "✈ Telegram tier"}</span></span></div>
        <svg class="mini" viewBox="0 0 280 28" preserveAspectRatio="none"><path d="${sparkPath(a.spark || [], 280, 28)}" fill="none" stroke="${fcls === "bad" || scls === "bad" ? "#ff7b72" : "#58a6ff"}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
    });
    [...box.children].forEach((c) => { if (!accts.some((a) => a.id === c.dataset.id)) c.remove(); });
  }

  // ── CDR column ─────────────────────────────────────────────────────────
  function renderSelected() {
    const F = S.fleet;
    const a = F && F.accounts.find((x) => x.id === S.selected);
    if (!a) { $("cdr-title").textContent = "Call logs"; return; }
    const th = F.thresholds || { fail_rate: 0.2, silent_rate: 0.15, volume_ratio: 0.5 };
    $("cdr-title").innerHTML = `Zentrunk call logs — ${esc(a.name)} <span class="muted small">${esc(a.plan || "")} · CPS ${esc(a.cps_limit)} · HA ${a.ha_enabled ? "on" : "off"}</span>`;
    const cls = (v, t, inv) => (inv ? v < t : v >= t) ? "bad" : (inv ? v < t * 1.4 : v >= t / 2) ? "warn" : "good";
    $("kpis").innerHTML = `
      <div><span class="label">Fail rate · 60 s</span><span class="kbig ${a.calls ? cls(a.fail_rate, th.fail_rate) : ""}">${a.calls ? pct(a.fail_rate) : "—"}</span></div>
      <div><span class="label">Silent answered</span><span class="kbig ${a.answered ? cls(a.silent_rate, th.silent_rate) : ""}">${a.answered ? pct(a.silent_rate) : "—"}</span></div>
      <div><span class="label">Volume vs normal</span><span class="kbig ${cls(a.volume_ratio, th.volume_ratio, true)}">${pct(a.volume_ratio)}</span></div>
      <div><span class="label">Calls / failed</span><span class="kmid">${a.calls} / ${a.failed}</span></div>
      <div><span class="label">At risk</span><span class="kmid ${a.at_risk_inr_per_hour > 0 ? "bad" : ""}">${inr(a.at_risk_inr_per_hour)}/h</span></div>`;
    const W = 600, H = 90, y = (v) => (H - 2 - v * (H - 4)).toFixed(1);
    const line = (t, label, dy) => `<line x1="0" x2="${W}" y1="${y(t)}" y2="${y(t)}" stroke="#da3633" stroke-dasharray="5 4" opacity=".7"/>` +
      `<text x="${W - 6}" y="${+y(t) + dy}" fill="#ff7b72" font-size="10" text-anchor="end">${label} ${pct(t)}</text>`;
    $("spark").innerHTML = line(th.fail_rate, "fail alert", -3) + line(th.silent_rate, "silent alert", 11) +
      `<path d="${sparkPath(a.spark || [], W, H)}" fill="none" stroke="#58a6ff" stroke-width="2.4" vector-effect="non-scaling-stroke"/>` +
      `<text x="6" y="13" fill="#8b949e" font-size="10">failed-or-silent share · 5 s buckets · last 3 min</text>`;
    $("causes").innerHTML = Object.entries(a.top_causes || {}).slice(0, 6).map(([k, n]) => {
      const code = parseInt(k, 10), bad = code && !SOFT_CODES.has(code) && code !== 3000 && code !== 3010;
      return `<span class="${bad ? "f" : ""}">${esc(k)} · ${esc(n)}</span>`;
    }).join("");
  }

  const num = (n) => String(n || "").replace(/^\+91(?=\d{10}$)/, "");  // drop +91 to save width (full in tooltip)

  function rowClass(r) {
    if (r.sip === 200 && r.audio_energy != null && r.audio_energy <= 5) return "silent";
    if (SOFT_CODES.has(r.code) || [486, 487, 480].includes(r.sip)) return "soft";
    if (r.sip != null && r.sip !== 200) return "fail";
    return "";
  }

  function renderCdr() {
    const rows = S.cdr || [];
    if (!rows.length) {
      $("cdr-body").innerHTML = `<tr><td colspan="11" class="empty">No calls yet — start the fleet.</td></tr>`;
      return;
    }
    const first = S.seenCdr.size === 0;
    $("cdr-body").innerHTML = rows.map((r) => {
      const c = rowClass(r), isNew = !first && !S.seenCdr.has(r.call_uuid);
      const audio = r.audio_energy == null ? "" : (c === "silent" ? "🔇 " : "") + Math.round(r.audio_energy);
      return `<tr class="${c}${isNew ? " new" : ""}"><td>${esc(r.initiation_time)}</td>` +
        `<td title="${esc(r.call_uuid)} · ${esc(r.direction)}">${r.direction === "inbound" ? "⇠" : "⇢"} ${esc(String(r.call_uuid).slice(0, 6))}…</td>` +
        `<td title="${esc(r.from)}">${esc(num(r.from))}</td><td title="${esc(r.to)}">${esc(num(r.to))}</td><td>${esc(r.carrier)}</td><td>${esc(r.to_carrier)}</td>` +
        `<td>${esc(r.duration)}s</td><td>${esc(r.sip)}</td><td class="cause" title="${esc(r.code)} ${esc(r.cause)}">${esc(r.code)} ${esc(r.cause)}</td><td class="src">${esc(r.source)}</td><td>${audio}</td></tr>`;
    }).join("");
    rows.forEach((r) => S.seenCdr.add(r.call_uuid));
  }

  // ── incident: stepper ──────────────────────────────────────────────────
  function stepIndex(c) {
    const st = c.state, fixes = c.fixes || [];
    if (["IDLE", "MONITORING"].includes(st)) return -1;
    if (st === "RESOLVED" || fixes.some((f) => f.status === "verified")) return 6;
    let i = 0;
    if (["ALERTED", "CALLING", "RETRY", "TELEGRAM_SENT"].includes(st)) i = 1;
    if (["IN_CALL", "UNSURE_TOLD", "WAITING_FOR_EVIDENCE"].includes(st)) i = 2;
    if (fixes.length || st === "CONFIRM_ACTIONS" || (c.investigator_log || []).some((e) => e.kind === "finding")) i = Math.max(i, 3);
    if (["EXECUTING", "VERIFYING"].includes(st) || fixes.some((f) => f.status === "applied")) i = 4;
    return i;
  }

  function renderStepper(c) {
    const idx = stepIndex(c);
    $("stepper").innerHTML = STEPS.map((s, i) =>
      `<li class="${i < idx ? "done" : i === idx ? "cur" : ""}">${esc(s)}</li>`).join("");
  }

  // ── incident: investigator trace ───────────────────────────────────────
  function fmtArgs(args) {
    if (!args || typeof args !== "object") return "";
    return Object.entries(args).filter(([k]) => k !== "account_id")
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
  }

  function traceEntry(e, c) {
    const conf = e.confidence != null ? ` <span class="muted">${pct(e.confidence)}</span>` : "";
    let body = "";
    switch (e.kind) {
      case "tool": {
        const res = typeof e.result === "string" ? e.result : JSON.stringify(e.result || "");
        body = `<span class="tool">${esc(e.tool)}(${esc(fmtArgs(e.args))})</span><span class="res">→ ${esc(res.slice(0, 260))}</span>`;
        break;
      }
      case "thought": body = `<span class="thought">${esc(e.text)}</span>`; break;
      case "hypothesis": {
        const h = (c.hypotheses || []).find((x) => x.id === e.id);
        body = `<span class="hpill ${esc(e.status)}">${esc((e.status || "open").replace("_", " "))}</span><b>${esc(h ? h.title : e.id)}</b>${conf}` +
          (e.evidence ? `<span class="res" title="${esc(e.evidence)}">${esc(e.evidence)}</span>` : "");
        break;
      }
      case "verify":
        body = `<span class="${e.ok ? "vok" : "vno"}">${e.ok ? "✓ verified" : "✗ not confirmed"}</span> <b>${esc(e.signature)}</b> <span class="muted">— ${esc(e.reason)}</span>`;
        break;
      case "finding":
        body = `<b>Found it:</b> ${esc(e.summary)}<br><span class="muted">fix</span> <span class="tool">${esc(e.fix)}</span> <span class="muted">· owner</span> ${esc(e.owner)}`;
        break;
      case "answer": {
        const q = (c.pending_questions || []).find((x) => x.id === e.question_id);
        body = `<span class="qa">${q ? `<b>Q:</b> ${esc(q.text)}<br>` : ""}<b>A:</b> ${esc(e.answer)}</span>`;
        break;
      }
      case "question": body = `<span class="qa"><b>Q:</b> ${esc(e.text)}</span>`; break;
      case "error": body = `<span class="txt">⚠ ${esc(e.text || e.reason || "error")}</span>`; break;
      default: body = esc(e.text || JSON.stringify(e));
    }
    const div = document.createElement("div");
    div.className = "tr k-" + esc(e.kind || "");
    div.innerHTML = `<span class="at">${esc(e.at || "")}</span><div>${body}</div>`;
    return div;
  }

  function renderTrace(c) {
    const box = $("trace"), log = c.investigator_log || [];
    const key = (c.incident_id || "") + "|" + (log[0] ? log[0].at + log[0].kind : "");
    if (!box.dataset.wired) {
      box.dataset.wired = "1";
      box.addEventListener("scroll", () => { S.traceStick = box.scrollHeight - box.scrollTop - box.clientHeight < 40; });
    }
    if (key !== S.logKey || log.length < S.logShown) {
      box.innerHTML = ""; S.logShown = 0; S.logKey = key;
    }
    box.querySelectorAll(".pending").forEach((n) => n.remove());
    for (let i = S.logShown; i < log.length; i++) {
      const n = traceEntry(log[i], c);
      if (S.logShown > 0) n.classList.add("new");
      box.appendChild(n);
    }
    S.logShown = log.length;
    (c.pending_questions || []).filter((q) => q.status !== "answered").forEach((q) => {
      const n = traceEntry({ kind: "question", at: (q.at || "").slice(11, 19) || q.at, text: q.text + "  (queued…)" }, c);
      n.classList.add("pending");
      box.appendChild(n);
    });
    if (!log.length) box.innerHTML = `<div class="empty">Investigator idle — it starts when an account looks wrong.</div>`, S.logShown = 0;
    if (S.traceStick !== false) box.scrollTop = box.scrollHeight;
  }

  // ── incident: hypotheses + fix ─────────────────────────────────────────
  function renderHyps(c) {
    const order = { confirmed: 0, leading: 1, open: 2, ruled_out: 3 };
    const hs = [...(c.hypotheses || [])].sort((a, b) => (order[a.status] - order[b.status]) || b.confidence - a.confidence);
    $("hypotheses").innerHTML = hs.length ? hs.map((h) => `<li class="${esc(h.status)}" title="${esc(h.reason)}">
      <div class="htop"><span class="t">${esc(h.title)}</span><span class="muted">${h.status === "ruled_out" ? "ruled out" : pct(h.confidence)}</span></div>
      <div class="cbar"><i style="width:${Math.round((h.status === "ruled_out" ? 1 : h.confidence || 0) * 100)}%"></i></div></li>`).join("")
      : `<li class="muted">No hypotheses yet.</li>`;
  }

  const rate = (v) => (typeof v === "number" && v <= 1 ? pct(v) : esc(v));

  function renderFix(c) {
    const fixes = c.fixes || [];
    const f = [...fixes].reverse().find((x) => x.status !== "rejected") || fixes[fixes.length - 1];
    if (!f) { $("fix").innerHTML = `<div class="muted">No fix proposed yet.</div>`; return; }
    const params = f.params && Object.keys(f.params).length ? ` <span class="muted">${esc(fmtArgs(f.params))}</span>` : "";
    const v = f.verification;
    $("fix").innerHTML = `<div class="fix-name">${esc(f.fix)}${params}</div>
      <div class="fix-sum">${esc(f.spoken_summary || f.evidence || "")}</div>
      <div class="kv"><span>status</span><b class="fstatus ${esc(f.status)}">${esc((f.status || "").replace("_", " "))}</b>
        <span>owner</span><b>${esc(f.owner)}</b><span>cost</span><b>${esc(f.cost || "none")}</b>
        <span>evidence</span><b>${f.evidence_score != null ? pct(f.evidence_score) : "—"}</b></div>
      ${v ? `<div class="verif">${rate(v.before)} → <span class="${f.status === "verified" ? "good" : "warn"}">${rate(v.after)}</span> <span class="muted small">in ${esc(v.seconds)} s</span></div>` : ""}
      ${f.result && f.result.message ? `<div class="muted small">${esc(f.result.message)}</div>` : ""}
      ${f.status === "proposed" ? `<button class="approve fix-btn" id="btn-approve">✓ Approve fix</button>` : ""}`;
    const b = $("btn-approve");
    if (b) b.addEventListener("click", () => { b.disabled = true; post("/incident/approve"); toast("Fix approved"); });
  }

  // ── incident: call + actions ───────────────────────────────────────────
  function renderCall(c) {
    const a = (c.calls || [])[c.calls ? c.calls.length - 1 : 0];
    const cs = $("call-state");
    cs.textContent = a ? `${a.state}${a.purpose && a.purpose !== "initial" ? " · " + a.purpose : ""}` : "no call";
    cs.className = "call-state " + (a ? esc(a.state) : "");
    $("btn-hangup").disabled = !a || ["ended", "failed"].includes(a.state);
    const box = $("transcript"), turns = c.transcript || [];
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = turns.length ? turns.map((t) => `<div class="turn ${t.role === "agent" ? "agent" : "engineer"}">` +
      `<b>${t.role === "agent" ? "Agent" + (t.via === "speak" ? " ⚡" : "") : "Engineer"}</b> ${esc(t.text)}</div>`).join("")
      : `<div class="muted">Transcript appears when the engineer picks up.</div>`;
    if (atBottom || turns.length < 4) box.scrollTop = box.scrollHeight;
  }

  function renderActions(c) {
    const acts = c.actions || [];
    const open = $("actions").querySelector("details[open]") != null;
    $("actions").innerHTML = acts.length ? acts.map((a) => {
      const st = `<span class="muted">${esc(a.status)}${a.attempts > 1 ? " · " + a.attempts + " tries" : ""}</span>`;
      const err = a.error ? `<span class="bad small">${esc(a.error)}</span>` : "";
      const p = a.payload || {};
      if (a.kind === "ticket") {
        const link = a.external_url ? `<a href="${esc(a.external_url)}" target="_blank" rel="noopener">${esc(a.external_id || "open")}</a>` : esc(a.external_id || "");
        return `<div class="action ${esc(a.status)}"><span class="k">🎫 Ticket</span>${link} ${esc(p.title || "")} ${st}${err}</div>`;
      }
      if (a.kind === "email_draft") {
        return `<div class="action ${esc(a.status)}"><span class="k">✉ Customer email</span>${st}${err}
          <details${open ? " open" : ""}><summary>show draft</summary><div class="email">${esc(p.body || p.text || c.email_draft || "")}</div></details></div>`;
      }
      const icon = a.kind === "telegram_notice" ? "✈ Telegram" : "📝 Summary";
      return `<div class="action ${esc(a.status)}"><span class="k">${icon}</span>${esc(p.text || p.body || "")} ${st}${err}</div>`;
    }).join("") : `<div class="muted">No actions yet.</div>`;
  }

  function renderCase() {
    const c = S.kase || {};
    renderTop(); renderStepper(c); renderTrace(c); renderHyps(c); renderFix(c); renderCall(c); renderActions(c);
    renderAccounts();
  }

  // ── live data loops ────────────────────────────────────────────────────
  async function refreshFleet() {
    try { S.fleet = FIXTURE ? S.fixtures.fleet : await getJSON("/fleet"); renderAccounts(); renderSelected(); renderTop(); }
    catch (e) { /* backend restarting */ }
  }
  async function refreshCdr() {
    if (!S.selected) return;
    try {
      S.cdr = FIXTURE ? (S.selected === S.fixtures.cdrAccount ? S.fixtures.cdr.rows : [])
        : (await getJSON(`/cdr?account=${encodeURIComponent(S.selected)}&limit=60`)).rows || [];
      renderCdr(); renderSelected();
    } catch (e) { /* keep last */ }
  }
  function startState() {
    const poll = async () => { try { S.kase = await getJSON("/state"); renderCase(); } catch (e) {} };
    poll();
    if (!window.EventSource) { setInterval(poll, 1000); return; }
    const es = new EventSource("/state/stream");
    es.onmessage = (m) => { try { S.kase = JSON.parse(m.data); renderCase(); } catch (e) {} };
    es.onerror = () => { es.close(); setTimeout(startState, 2000); };
  }

  // ── fixture mode ───────────────────────────────────────────────────────
  async function startFixture() {
    $("fixture-tag").classList.remove("hidden");
    const [state, fleet, cdr] = await Promise.all(["state", "fleet", "cdr"].map((n) => getJSON(`fixtures/${n}.json`)));
    S.fixtures = { fleet, cdr, cdrAccount: cdr.account || state.account_id };
    const log = state.investigator_log || [];
    const held = log.splice(log.length - 3);           // drip the last 3 entries in
    const heldTurns = (state.transcript || []).splice(-1);
    S.kase = state;
    renderCase(); await refreshFleet(); await refreshCdr();
    const timer = setInterval(() => {
      if (held.length) { log.push(held.shift()); S.kase.version++; }
      else if (heldTurns.length) S.kase.transcript.push(heldTurns.shift());
      else clearInterval(timer);
      renderCase();
    }, 2500);
  }

  function fixturePost(path) {
    const c = S.kase;
    toast("fixture mode — " + path.split("?")[0] + " not sent");
    if (path === "/incident/approve") {
      const f = c.fixes.find((x) => x.status === "proposed");
      if (!f) return;
      f.status = "applied"; c.state = "VERIFYING"; renderCase();
      setTimeout(() => {
        f.status = "verified"; f.verification = { before: 0.34, after: 0.03, seconds: 21 };
        c.state = "RESOLVED"; renderCase();
      }, 3000);
    } else if (path.startsWith("/incident/ask")) {
      const q = new URLSearchParams(path.split("?")[1]).get("q");
      c.pending_questions.push({ id: "q" + Date.now(), text: q, status: "pending", at: new Date().toISOString() });
      renderCase();
    }
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  $("btn-start").addEventListener("click", () => post("/fleet/start"));
  $("btn-stop").addEventListener("click", () => post("/fleet/stop"));
  $("btn-allpass").addEventListener("click", () => { post("/fleet/allpass"); toast("All calls pass"); });
  $("btn-reset").addEventListener("click", () => { S.userPicked = false; S.seenCdr.clear(); post("/demo/reset"); });
  $("btn-hangup").addEventListener("click", () => post("/demo/hangup"));
  $("ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("ask-input").value.trim();
    if (!q) return;
    post("/incident/ask?q=" + encodeURIComponent(q));
    $("ask-input").value = "";
    toast("Question sent to the investigator");
  });

  if (FIXTURE) { startFixture(); return; }
  startState();
  refreshFleet(); refreshCdr();
  setInterval(refreshFleet, 1000);
  setInterval(refreshCdr, 1000);
})();
