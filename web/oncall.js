// On-call Agent console: hash-routed pages, live status polling, voice → screen events.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const inr = (x) => (x == null ? "–" : `₹${Math.round(x).toLocaleString("en-IN")}`);
const view = $("#view");

const FAIL = new Set([5350, 5180, 4590, 4170, 4340, 4160]);
const NOT_ANSWERED = new Set([4410, 6030, 4550]);
const CARRIERS = ["TATA", "Airtel", "Jio"];
const CODES = [[5350, "service_unavailable_by_carrier"], [5180, "cps_limit_reached"], [4590, "domestic_anchored_terms_not_met"],
  [4170, "request_timeout_customer"], [4340, "temporary_unavailable"], [4160, "request_timeout_carrier"],
  [3000, "normal_hangup"], [4410, "user_busy"], [6030, "alloted_timeout"], [4550, "user_cancelled"]];
const STATE_LABEL = {
  IDLE: "Idle", MONITORING: "Monitoring", SUSPECTED: "Investigating", ALERTED: "Placing call", CALLING: "Calling",
  RETRY: "Retrying call", IN_CALL: "On call", UNSURE_TOLD: "On call", WAITING_FOR_EVIDENCE: "Investigating",
  CONFIRM_ACTIONS: "Awaiting approval", EXECUTING: "Applying fix", VERIFYING: "Verifying", RESOLVED: "Resolved",
  TELEGRAM_SENT: "Call ended", AUTO_RECOVERED: "Recovered",
};
const STEPS = [["MONITORING", "Monitoring"], ["SUSPECTED", "Investigating"], ["CALLING", "Calling"], ["IN_CALL", "On call"],
  ["CONFIRM_ACTIONS", "Fix proposed"], ["EXECUTING", "Applying"], ["VERIFYING", "Verifying"], ["RESOLVED", "Resolved"]];
const STEP_INDEX = Object.fromEntries(STEPS.map(([k], i) => [k, i]));
Object.assign(STEP_INDEX, { ALERTED: 2, RETRY: 2, UNSURE_TOLD: 3, WAITING_FOR_EVIDENCE: 1, TELEGRAM_SENT: 3, AUTO_RECOVERED: 1 });
const FAULT_NAME = {
  carrier_reject: "Carrier rejecting calls", cps_burst: "Dialer exceeding CPS limit", acl_drop: "New SBC IP not on the trunk allow-list",
  silent_route: "Silent calls on one route", anchoring_4590: "India domestic-anchoring rejections", pbx_timeout: "Customer PBX not answering",
};

const S = {
  status: null, logs: { account: "", status: "all", carrier: "", code: "", q: "" }, live: true,
  insightsBy: "carrier", calls: {}, history: [], timers: [],
};

// ── router ────────────────────────────────────────────────────────────
function parseRoute() {
  const h = location.hash.replace(/^#/, "") || "/";
  const [path, query] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(query || ""));
  if (!parts.length) return { name: "overview", params };
  if (parts[0] === "logs" && parts[1]) return { name: "call", uuid: decodeURIComponent(parts[1]), params };
  return { name: parts[0], params };
}
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }

function route() {
  clearTimers();
  const r = parseRoute();
  const last = S.history[S.history.length - 1];
  if (last !== location.hash) S.history.push(location.hash || "#/");
  document.querySelectorAll(".nav nav a").forEach((a) => a.classList.toggle("active",
    a.dataset.route === (r.name === "call" ? "logs" : r.name)));
  const pages = { overview: Overview, incident: Incident, report: Report, flow: Flow, logs: Logs, call: CallDetail, insights: Insights, accounts: Accounts };
  (pages[r.name] || Overview)(r);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

function crumbs(items) {
  $("#crumbs").innerHTML = items.map((it, i) => (i < items.length - 1 && it[1]
    ? `<a href="${it[1]}">${esc(it[0])}</a><span class="sep">/</span>` : `<span>${esc(it[0])}</span>`)).join("");
}

// ── live status ───────────────────────────────────────────────────────
async function poll() {
  try { S.status = await (await fetch("/public/status", { cache: "no-store" })).json(); } catch (e) { /* retry */ }
  renderSession();
  const r = parseRoute();
  if (r.name === "incident") Incident(r, true);
  if (r.name === "accounts") Accounts(r, true);
  setTimeout(poll, S.status && S.status.busy ? 1200 : 4000);
}

function renderSession() {
  const s = S.status, el = $("#session");
  if (!s || !s.session) { el.innerHTML = `<span class="state"><span class="dot"></span>No active demo</span>`; return; }
  const se = s.session;
  const label = se.ended ? (se.outcome === "resolved" ? "Resolved" : "Ended") : (STATE_LABEL[s.state] || s.state);
  const dot = se.ended ? (se.outcome === "resolved" ? "ok" : "") : (["CALLING", "IN_CALL", "CONFIRM_ACTIONS", "UNSURE_TOLD"].includes(s.state) ? "live" : "busy");
  el.innerHTML = `<span>${se.mode === "call" ? "Calling " + esc(se.number) : "Simulated phone"}</span>
    <span>${se.elapsed_s}s</span><span class="state"><span class="dot ${dot}"></span>${esc(label)}</span>`;
}

// ── voice → screen ────────────────────────────────────────────────────
let voiceTimer = null;
function showVoice(said, what) {
  const b = $("#voicebar");
  b.innerHTML = `Voice command <b>“${esc(said)}”</b>${what ? ` · ${esc(what)}` : ""}`;
  b.hidden = false;
  clearTimeout(voiceTimer); voiceTimer = setTimeout(() => { b.hidden = true; }, 6000);
}
function onScreenEvent(ev) {
  if (ev.type === "notify") { notice(ev); return; }
  const what = { open_call: "opened a call log", filter: "filtered call logs", chart: "opened insights",
                 scroll: "switched page", close: "went back", flow: "opened the call flow", report: "opened the incident report" }[ev.type];
  if (ev.said) showVoice(ev.said, what);
  if (ev.type === "open_call") { S.calls[ev.call.call_uuid] = ev.call; go(`#/logs/${encodeURIComponent(ev.call.call_uuid)}?tab=flow`); }
  else if (ev.type === "filter") {
    const f = ev.filter || {};
    S.logs = { account: S.status?.incident?.account_id || "", status: f.status || "all", carrier: f.carrier || "",
               code: f.code ? String(f.code) : "", q: "" };
    go("#/logs");
  } else if (ev.type === "flow") { go("#/flow"); }
  else if (ev.type === "report") { go("#/report"); }
  else if (ev.type === "chart") { S.insightsBy = ev.by || "carrier"; S.lastChart = ev; go("#/insights"); }
  else if (ev.type === "scroll") {
    go({ investigator: "#/incident", call: "#/incident", outcome: "#/incident", accounts: "#/accounts", logs: "#/logs" }[ev.section] || "#/incident");
  } else if (ev.type === "close") {
    S.history.pop();
    const prev = S.history.pop();
    go(prev && !prev.startsWith("#/logs/") ? prev : "#/incident");
  }
}
function notice(ev) {
  const box = $("#notices");
  const el = document.createElement("div");
  el.className = `notice ${ev.kind || ""}`;
  const kind = { ticket: "Linear", telegram: "Telegram", error: "Error" }[ev.kind] || "Update";
  el.innerHTML = `<button class="x" title="dismiss">✕</button><div class="k">${esc(kind)}</div><div class="h">${esc(ev.title)}</div>
    <div class="b">${esc(ev.body || "")}</div>${ev.ref && /^https?:/.test(ev.ref) ? `<a href="${esc(ev.ref)}" target="_blank" rel="noopener">Open in Linear</a>` : ""}`;
  el.querySelector(".x").addEventListener("click", () => el.remove());
  box.prepend(el);
  setTimeout(() => el.remove(), ev.kind === "telegram" ? 14000 : 10000);
}

function connectScreen() {
  const es = new EventSource("/public/ui/stream");
  es.onmessage = (m) => { try { onScreenEvent(JSON.parse(m.data)); } catch (e) { /* ignore */ } };
  es.onerror = () => { es.close(); setTimeout(connectScreen, 2000); };
}

// ── pages ─────────────────────────────────────────────────────────────
function Overview() {
  crumbs([["Overview"]]);
  view.innerHTML = `
    <section class="hero">
      <h1>An on-call engineer that phones you when a customer's calls start failing.</h1>
      <p>It watches every account by revenue, notices failures that dashboards miss, works out who owns the problem,
      and calls the engineer. While you talk it keeps investigating, shows you the evidence on this screen, proposes
      a fix with its cost, applies it when you say yes, and confirms the traffic recovered.</p>
    </section>
    <div class="card"><div class="card-h"><h2>Try it</h2><span class="muted">Your phone rings only after a customer starts failing</span></div>
      <div class="card-b">
        <form id="tryform" class="try" autocomplete="off">
          <label class="field">Name (optional)<input id="f-name" maxlength="24" placeholder="Priya"></label>
          <label class="field">Indian mobile number<input id="f-phone" inputmode="tel" placeholder="98765 43210"></label>
          <label class="field">Access code<input id="f-code" placeholder="From the submission notes"></label>
          <button class="btn primary" id="b-call" type="submit">Call me</button>
          <button class="btn" id="b-watch" type="button">Watch without a call</button>
        </form>
        <div class="msg" id="f-msg"></div>
        <p class="muted" style="margin:6px 0 0;font-size:13px">This puts you on the rotation. Nothing rings yet. When a high-spend account starts failing and stays failing, your phone rings about 40 seconds later. Pick up, ask what's going on, ask it to
        open a call log or compare carriers, and say "yes" when it proposes the fix. Calls are limited to 5 minutes and one per number.</p>
      </div></div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>How it works</h2></div><div class="card-b">
      <ol class="steps">
        <li><b>Detect</b><span>Per-account checks on failure rate, silent calls (answered with no audio) and volume drops.</span></li>
        <li><b>Investigate</b><span>An LLM investigator runs queries over call records, edge logs and account config the moment something looks wrong.</span></li>
        <li><b>Call</b><span>High-revenue accounts get a phone call through Agora Conversational AI with a Murf Falcon voice; smaller ones get a message.</span></li>
        <li><b>Show</b><span>While you talk, ask it to open a call log, filter by carrier or compare routes. This screen follows your voice.</span></li>
        <li><b>Verify</b><span>"Found it" is only allowed after code re-checks the conclusion against raw data. Wrong guesses are rejected.</span></li>
        <li><b>Fix and follow up</b><span>Applies the fix on your "yes", watches recovery, files the ticket and drafts the customer email.</span></li>
      </ol></div></div>`;
  const msg = $("#f-msg");
  let inflight = false;
  async function start(mode) {
    if (inflight) return; inflight = true;
    $("#b-call").disabled = $("#b-watch").disabled = true;
    msg.className = "msg"; msg.textContent = "Starting…";
    try {
      const r = await fetch("/public/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, phone: $("#f-phone").value, code: $("#f-code").value, name: $("#f-name").value }) });
      const j = await r.json();
      if (!r.ok) { msg.className = "msg err"; msg.textContent = j.error || "Something went wrong."; if (j.busy) setTimeout(() => go("#/incident"), 1500); return; }
      msg.className = "msg ok";
      msg.textContent = j.waiting
        ? `${j.number} is on the rotation. The phone stays quiet until a high-spend account is broken, then rings about 40 seconds later.`
        : (mode === "call" ? `Calling ${j.number} in about ${j.eta_s} seconds from a US number.` : "Started.");
      setTimeout(() => go("#/incident"), 900);
    } catch (e) { msg.className = "msg err"; msg.textContent = "Network error, please try again."; }
    finally { setTimeout(() => { inflight = false; $("#b-call") && ($("#b-call").disabled = $("#b-watch").disabled = false); }, 2500); }
  }
  $("#tryform").addEventListener("submit", (e) => { e.preventDefault(); start("call"); });
  $("#b-watch").addEventListener("click", () => start("watch"));
}

function Incident(r, refresh) {
  crumbs([["Live incident"]]);
  const s = S.status;
  if (!s || !s.session) {
    if (!refresh) view.innerHTML = `<div class="card"><div class="empty">No demo has run yet.<br><a class="btn primary" href="#/">Start one</a></div></div>`;
    return;
  }
  const se = s.session, inc = s.incident || {}, state = s.state || "MONITORING";
  const cur = STEP_INDEX[state] ?? 0, ended = se.ended;
  const acct = (s.accounts || []).find((a) => a.id === inc.account_id);
  const sigLabel = { failure_rate: "failing", silent_rate: "silent", volume_drop: "of normal volume" }[inc.signal] || "";
  const keep = { t: $("#transcript")?.scrollTop, l: $("#stepslog")?.scrollTop };
  const f = inc.fix;
  view.innerHTML = `
    <div class="page-head"><div><h1>${esc(inc.account || "Waiting for an incident")}</h1>
      <p>${se.waiting ? "Healthy traffic. The phone rings about 40 seconds after a high-spend account starts failing." : (inc.id ? esc(inc.id) + " · " : "") + (se.hidden_fault ? esc(FAULT_NAME[se.hidden_fault] || se.hidden_fault) : "Hidden fault: not identified yet")}</p></div>
      <span class="badge ${ended ? (se.outcome === "resolved" ? "ok" : "") : "info"}">${esc(ended ? (se.outcome === "resolved" ? "Resolved" : "Ended: " + se.outcome) : STATE_LABEL[state] || state)}</span></div>
    <div class="card"><div class="card-b">
      <div class="timeline">${STEPS.map(([k, l], i) => `<div class="${i < cur || (ended && state === "RESOLVED") ? "done" : i === cur ? "now" : ""}">${l}</div>`).join("")}</div>
      <div class="stat-row">
        <div class="stat"><div class="v">${inc.signal_value != null ? pct(inc.signal_value) : "–"}</div><div class="l">calls ${esc(sigLabel || "affected")}</div></div>
        <div class="stat"><div class="v">${acct && acct.at_risk ? inr(acct.at_risk) : "–"}</div><div class="l">revenue at risk per hour</div></div>
        <div class="stat"><div class="v">${inc.confidence ? pct(inc.confidence) : "–"}</div><div class="l">diagnosis confidence</div></div>
        <div class="stat"><div class="v">${se.elapsed_s}s</div><div class="l">since the fault started</div></div>
      </div></div></div>
    <div class="card" style="margin-top:16px" id="tl-card">${S.timeline && S.timeline.account_id === inc.account_id ? timelineHtml(S.timeline) : ""}</div>
    <div class="grid-2" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>Call</h2><span class="muted">${esc(callLine(inc.call, se))}</span></div>
        <div class="card-b"><div class="transcript" id="transcript">${transcriptHtml(inc.transcript || [])}</div></div></div>
      <div class="card"><div class="card-h"><h2>Investigation</h2><span class="muted">LLM tool calls, checked by code</span></div>
        <div class="card-b">${hypsHtml(inc.hypotheses || [])}<ol class="steps-log" id="stepslog">${stepsHtml(inc.log || [])}</ol></div></div>
    </div>
    ${f ? resolutionHtml(f, inc, state) : ""}
    ${state === "RESOLVED" ? `<div style="margin-top:16px"><a class="btn primary" href="#/report">Open the incident report</a></div>` : ""}`;
  loadTimeline();
  const t = $("#transcript"), l = $("#stepslog");
  if (t) t.scrollTop = keep.t != null && refresh ? Math.max(keep.t, t.scrollHeight) : t.scrollHeight;
  if (l) l.scrollTop = l.scrollHeight;
}
let tlBusy = false, tlAt = 0;
async function loadTimeline() {
  if (tlBusy || Date.now() - tlAt < 2000) return;
  tlBusy = true; tlAt = Date.now();
  try {
    const j = await (await fetch("/public/timeline", { cache: "no-store" })).json();
    S.timeline = j;
    const el = $("#tl-card"); if (el) el.innerHTML = j.account_id ? timelineHtml(j) : "";
  } catch (e) { /* keep */ } finally { tlBusy = false; }
}
const dur = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);
function timelineHtml(j) {
  const pts = j.points.filter((p) => p.v != null);
  if (pts.length < 2) return "";
  const W = 1100, H = 190, L = 44, R = 16, T = 34, B = 26;
  const t0 = Math.min(pts[0].t, 0), t1 = Math.max(j.now_t, pts[pts.length - 1].t);
  const vol = j.signal === "volume_drop";
  const vmax = vol ? Math.max(1.2, ...pts.map((p) => p.v)) : Math.max(0.3, ...pts.map((p) => p.v)) * 1.1;
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - v / vmax) * (H - T - B);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts[pts.length - 1].t).toFixed(1)},${y(0)} L${x(pts[0].t).toFixed(1)},${y(0)} Z`;
  const thr = j.threshold != null ? `<line x1="${L}" x2="${W - R}" y1="${y(j.threshold)}" y2="${y(j.threshold)}" stroke="#c2410c" stroke-dasharray="4 4" stroke-width="1"/>
    <text x="${L + 6}" y="${y(j.threshold) - 5}" font-size="11" fill="#c2410c">${vol ? "alert below" : "alert above"} ${pct(j.threshold)}</text>` : "";
  const ticks = [0, 0.5, 1].map((f) => { const v = vmax * f; return `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#9ca3af">${pct(v)}</text>
    <line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#f1f3f6"/>`; }).join("");
  let lastX = -999, row = 0;
  const marks = j.marks.map((m) => {
    const mx = x(m.t); row = mx - lastX < 92 ? (row + 1) % 2 : 0; lastX = mx;
    const fixish = /Fix|Recovered/.test(m.label), bad = /Fault/.test(m.label);
    const col = bad ? "#b91c1c" : fixish ? "#15803d" : "#3355d8";
    return `<line x1="${mx}" x2="${mx}" y1="${T - 4}" y2="${H - B}" stroke="${col}" stroke-width="1" stroke-dasharray="2 3"/>
      <text x="${mx + 3}" y="${12 + row * 13}" font-size="11" font-weight="500" fill="${col}">${esc(m.label)} · ${dur(m.t)}</text>`;
  }).join("");
  const xt = [0, Math.round(t1 / 2), Math.round(t1)].map((t) => `<text x="${x(t)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#9ca3af">+${dur(t)}</text>`).join("");
  const M = j.metrics;
  const stat = (v, l) => `<div class="stat"><div class="v">${dur(v)}</div><div class="l">${l}</div></div>`;
  const label = { failure_rate: "Failure rate", silent_rate: "Silent-call rate", volume_drop: "Call volume vs normal" }[j.signal] || "Signal";
  return `<div class="card-h"><h2>${label} since the fault</h2><span class="muted">5-second buckets from raw call records</span></div>
    <div class="card-b hscroll"><svg class="tline" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${ticks}
      <path d="${area}" fill="${vol ? "#eef2ff" : "#fef2f2"}"/><path d="${line}" fill="none" stroke="${vol ? "#3355d8" : "#b91c1c"}" stroke-width="2"/>${thr}${marks}${xt}</svg>
      <div class="stat-row">${stat(M.detect_s, "to detect")}${stat(M.call_s, "to phone the engineer")}${stat(M.diagnose_s, "to a verified diagnosis")}${stat(M.fix_s, "to the fix")}${stat(M.recover_s, "to recovery")}
      ${j.manual_baseline_s ? `<div class="stat"><div class="v">${dur(j.manual_baseline_s)}</div><div class="l">typical manual response</div></div>` : ""}</div></div>`;
}

function callLine(c, se) {
  if (!c) return "Not placed yet: the agent waits ~25 s to confirm it isn't a blip";
  return { placing: "Dialling", ringing: "Ringing", answered: "Connected", ended: "Ended", failed: "Failed" }[c.state] || c.state;
}
function transcriptHtml(tr) {
  if (!tr.length) return `<p class="muted" style="margin:0">The conversation appears here.</p>`;
  return tr.map((t) => {
    const agent = t.role !== "engineer";
    return `<div class="turn ${t.via === "speak" ? "update" : ""}"><div class="who ${agent ? "agent" : ""}">${agent ? (t.via === "speak" ? "Agent · update" : "Agent") : "Engineer"}</div><p>${esc(t.text)}</p></div>`;
  }).join("");
}
function hypsHtml(h) {
  if (!h.length) return "";
  return `<div class="hyps">${h.map((x) => `<div class="hyp ${x.status}"><span>${esc(x.title || x.id)}</span>
    <span class="badge ${x.status === "confirmed" ? "ok" : x.status === "leading" ? "warn" : ""}">${esc(x.status.replace("_", " "))}${x.status === "leading" || x.status === "confirmed" ? " · " + pct(x.confidence) : ""}</span></div>`).join("")}</div>`;
}
function stepsHtml(log) {
  if (!log.length) return `<li><span></span><span></span><span class="x muted">Waiting for an alert.</span></li>`;
  return log.map((e) => {
    const m = e.model ? ` <span class="model">${esc(String(e.model).replace("azure:", "").replace("openai/", ""))}</span>` : "";
    let k = "Note", cls = "", x = "";
    if (e.kind === "tool") { k = e.triage ? "Triage" : "Query"; x = `${esc(e.tool)}(${esc(Object.entries(e.args || {}).filter(([a]) => a !== "account_id").map(([a, v]) => `${a}=${v}`).join(", "))})${m}`; }
    else if (e.kind === "hypothesis") { k = "Hypothesis"; x = `${esc(e.id)} → ${esc(e.status)}: ${esc((e.evidence || "").slice(0, 180))}${m}`; }
    else if (e.kind === "verify") { k = e.ok ? "Verified" : "Rejected"; cls = e.ok ? "ok" : "no"; x = `${esc(e.signature)}: ${esc(e.reason)}`; }
    else if (e.kind === "finding") { k = "Found"; cls = "ok"; x = `${esc(e.summary)}`; }
    else if (e.kind === "answer") { k = "Answer"; x = esc(e.answer); }
    else if (e.kind === "screen") { k = "Voice"; cls = "voice"; x = `${esc(e.action.replace("_", " "))} · “${esc(e.said || "")}”`; }
    else if (e.kind === "error") { k = "Error"; cls = "no"; x = esc(e.text); }
    else { x = esc(e.text || ""); }
    return `<li><span class="t">${esc(e.at)}</span><span class="k ${cls}">${k}</span><span class="x">${x}</span></li>`;
  }).join("");
}
function resolutionHtml(f, inc, state) {
  const v = f.verification;
  const ticket = (inc.actions || []).find((a) => a.kind === "ticket");
  const params = f.params && Object.keys(f.params).length ? " (" + Object.entries(f.params).map(([k, x]) => `${k}: ${x}`).join(", ") + ")" : "";
  return `<div class="card" style="margin-top:16px"><div class="card-h"><h2>${state === "RESOLVED" ? "Resolution" : "Diagnosis"}</h2>
    <span class="badge ${f.status === "verified" ? "ok" : "info"}">${esc(f.status)}</span></div><div class="card-b"><dl class="kv">
    <dt>Cause</dt><dd>${esc(f.spoken_summary || f.evidence)}</dd>
    <dt>Evidence</dt><dd>${esc(f.evidence)}</dd>
    <dt>Found by</dt><dd>${f.source === "rules" ? "Rules fallback" : "LLM investigator"}, verified by code</dd>
    <dt>Owner</dt><dd>${esc(f.owner)}</dd>
    <dt>Fix</dt><dd>${esc(f.fix.replace(/_/g, " "))}${esc(params)}${f.cost ? " · " + esc(f.cost) : ""}</dd>
    ${v ? `<dt>Recovery</dt><dd><b>${pct(v.before)} → ${pct(v.after)}</b> within ${v.seconds} s</dd>` : ""}
    ${ticket ? `<dt>Ticket</dt><dd>${esc(ticket.id || ticket.status)} · ${ticket.attempts} attempt${ticket.attempts === 1 ? "" : "s"} (the first one fails on purpose to show the retry)</dd>` : ""}
    </dl>${inc.email ? `<details style="margin-top:12px"><summary class="muted">Customer email draft</summary><pre class="email">${esc(inc.email)}</pre></details>` : ""}</div></div>`;
}

// ── Incident report ───────────────────────────────────────────────────
async function Report() {
  crumbs([["Incident report"]]);
  view.innerHTML = `<div class="card"><div class="empty">Loading…</div></div>`;
  let r; try { r = await (await fetch("/public/report", { cache: "no-store" })).json(); } catch (e) { r = {}; }
  if (!r.incident_id) { view.innerHTML = `<div class="card"><div class="empty">No incident yet.<br><a class="btn primary" href="#/">Start a demo</a></div></div>`; return; }
  const i = r.impact, rc = r.root_cause, rs = r.resolution, fu = r.follow_ups, inv = r.investigation;
  const v = rs.verification;
  const params = rs.params && Object.keys(rs.params).length ? " (" + Object.entries(rs.params).map(([k, x]) => `${k}: ${x}`).join(", ") + ")" : "";
  const sigWord = i.signal === "silent_rate" ? "silent" : i.signal === "volume_drop" ? "of normal volume" : "failing";
  view.innerHTML = `
    <div class="page-head"><div><h1>${esc(r.title)}</h1><p>${esc(r.incident_id)} · ${esc(r.account)}</p></div>
      <div style="display:flex;gap:10px;align-items:center"><span class="badge ${r.status === "Resolved" ? "ok" : "info"}">${esc(r.status)}</span>
      <a class="btn" href="/public/report.md" download>Download Markdown</a></div></div>
    <div class="card"><div class="card-h"><h2>Summary</h2></div><div class="card-b"><p class="story" style="margin:0">${esc(r.summary)}</p></div></div>
    <div class="card" style="margin-top:16px"><div class="card-b"><div class="stat-row" style="margin-top:0">
      <div class="stat"><div class="v">${esc(i.duration)}</div><div class="l">incident duration</div></div>
      <div class="stat"><div class="v">${i.affected_calls}</div><div class="l">calls ${esc(i.affected_what)}</div></div>
      <div class="stat"><div class="v">${pct(i.peak)}</div><div class="l">peak, calls ${sigWord}</div></div>
      <div class="stat"><div class="v">${inr(i.revenue_at_risk_per_hour)}</div><div class="l">revenue at risk per hour</div></div>
      <div class="stat"><div class="v">${inr(i.revenue_at_risk_total)}</div><div class="l">at risk over the incident</div></div></div></div></div>
    <div class="grid-2" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>Timeline</h2><span class="muted">from the fault</span></div>
        <div class="table-wrap"><table><thead><tr><th>Time</th><th class="num">Since fault</th><th>Event</th></tr></thead><tbody>
        ${r.timeline.map((t) => `<tr class="nolink"><td class="mono">${esc(t.at)}</td><td class="num">+${esc(t.t)}</td><td>${esc(t.label)}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card"><div class="card-h"><h2>Root cause</h2><span class="muted">${esc(rc.found_by || "")}${rc.found_by ? ", verified by code" : ""}</span></div><div class="card-b"><dl class="kv">
        <dt>Cause</dt><dd>${esc(rc.evidence || "Not determined yet")}</dd><dt>Owner</dt><dd>${esc(rc.owner || "–")}</dd>
        <dt>Code check</dt><dd>${esc(rc.check || "–")}</dd><dt>Queries run</dt><dd>${inv.queries} (${esc(inv.models.join(", ") || "rules only")})</dd>
        <dt>Ruled out</dt><dd>${esc(inv.ruled_out.join("; ") || "–")}</dd><dt>Rejected guesses</dt><dd>${inv.rejected_conclusions}</dd></dl></div></div>
    </div>
    <div class="grid-2" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>Resolution</h2></div><div class="card-b"><dl class="kv">
        <dt>Fix</dt><dd>${esc(rs.fix || "–")}${esc(params)}</dd><dt>Status</dt><dd>${esc(rs.status || "–")}</dd>
        <dt>Cost</dt><dd>${esc(rs.cost || "none")}</dd>
        ${v ? `<dt>Recovery</dt><dd><b>${pct(v.before)} → ${pct(v.after)}</b> within ${v.seconds}s</dd>` : ""}</dl></div></div>
      <div class="card"><div class="card-h"><h2>Follow-ups</h2></div><div class="card-b"><dl class="kv">
        <dt>Ticket</dt><dd>${fu.ticket ? esc(fu.ticket) : "Not filed"}</dd>
        <dt>Telegram summary</dt><dd>${fu.telegram_summary ? "Sent" : "Not sent"}</dd>
        <dt>Customer email</dt><dd>${fu.email ? "Drafted (below)" : "–"}</dd></dl></div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>Prevention</h2></div><div class="card-b">
      <ol style="margin:0;padding-left:20px">${r.prevention.map((p) => `<li style="margin:6px 0">${esc(p)}</li>`).join("") || "<li>–</li>"}</ol></div></div>
    ${fu.email ? `<div class="card" style="margin-top:16px"><div class="card-h"><h2>Customer email draft</h2></div><div class="card-b"><pre class="email" style="margin:0">${esc(fu.email)}</pre></div></div>` : ""}
    <div class="card" style="margin-top:16px"><details><summary class="card-h" style="cursor:pointer"><h2 style="display:inline">Call transcript</h2> <span class="muted">${r.transcript.length} turns</span></summary>
      <div class="card-b"><div class="transcript" style="max-height:none">${transcriptHtml(r.transcript)}</div></div></details></div>`;
}

// ── Call flow map ─────────────────────────────────────────────────────
function Flow() {
  crumbs([["Call flow"]]);
  view.innerHTML = `
    <div class="page-head"><div><h1 id="fl-title">Call flow</h1><p>How the incident account's calls travel, leg by leg, and what each leg answers. Last 30 seconds, live.</p></div>
      <div class="legend"><span><i style="background:#8fb59a"></i>200 OK</span><span><i style="background:#b91c1c"></i>Rejected</span><span><i style="background:#d97706"></i>Connected, no audio</span></div></div>
    <div class="card"><div class="card-b hscroll" id="fl-map"><div class="empty">Loading…</div></div></div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>What the map says</h2></div><div class="card-b"><p class="reading" id="fl-read"></p></div></div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>One call on the failing path</h2><a id="fl-open" class="muted" href="#/logs">Open in call logs</a></div>
      <div class="card-b hscroll" id="fl-ladder"><p class="muted" style="margin:0">Appears when a leg is failing.</p></div></div>`;
  loadFlow(true);
  S.timers.push(setInterval(() => loadFlow(false), 2000));
}
let flowSample = null;
async function loadFlow(first) {
  let m; try { m = await (await fetch("/public/flowmap", { cache: "no-store" })).json(); } catch (e) { return; }
  const el = $("#fl-map"); if (!el) return;
  if (!m.account_id) { el.innerHTML = `<div class="empty">No incident account yet. Start a demo from the Overview page.</div>`; return; }
  $("#fl-title").textContent = `${m.account} · call flow`;
  el.innerHTML = flowSvg(m);
  if (first && el.scrollWidth > el.clientWidth) el.scrollLeft = (el.scrollWidth - el.clientWidth) * 0.45;
  $("#fl-read").textContent = m.story;
  if (m.where === "acl") $("#fl-ladder").innerHTML = `<p class="muted" style="margin:0">Blocked INVITEs never become calls, so there is no CDR or SIP ladder for them: the trunk's edge log is the only evidence. That's why dashboards that watch the failure rate miss this one.</p>`;
  if (m.sample_uuid && (first || m.sample_uuid !== flowSample) && (first || !flowSample)) {
    flowSample = m.sample_uuid;
    try {
      const r = await fetch("/public/call/" + encodeURIComponent(m.sample_uuid));
      if (r.ok) { const d = await r.json(); S.calls[d.call_uuid] = d; $("#fl-ladder").innerHTML = seqHtml(d);
        $("#fl-open").href = `#/logs/${encodeURIComponent(d.call_uuid)}?tab=flow`; }
    } catch (e) { /* keep */ }
  }
}

function flowSvg(m) {
  const W = 1190, nodeW = 148, nodeH = 56;
  const carriers = m.carriers, nets = m.networks;
  const rows = Math.max(carriers.length, nets.length, 3);
  const H = 90 + rows * 92;
  const X = { cust: 80, acl: 320, trunk: 560, car: 830, net: 1090 };
  const yMid = 60 + (rows * 92) / 2;
  const ys = (n) => Array.from({ length: n }, (_, i) => 60 + (rows * 92) * (i + 0.5) / n);
  const cy = ys(carriers.length), ny = ys(nets.length);
  const maxCalls = Math.max(1, ...carriers.map((c) => c.calls), m.passed);
  const sw = (n) => (n ? Math.min(10.5, 1.5 + 9 * Math.sqrt(n / maxCalls)) : 1);
  const node = (x, y, t, sub, cls = "") => `<g class="node ${cls}"><rect x="${x - nodeW / 2}" y="${y - nodeH / 2}" width="${nodeW}" height="${nodeH}" rx="8"/>
    <text class="t" x="${x}" y="${y - 3}" text-anchor="middle">${esc(t)}</text><text class="s" x="${x}" y="${y + 14}" text-anchor="middle">${esc(sub)}</text></g>`;
  const curve = (x1, y1, x2, y2) => { const mx = (x1 + x2) / 2; return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`; };
  const label = (x, y, text, cls) => { const w = Math.max(40, text.length * 6.9 + 16);
    return `<g class="lbl ${cls}"><rect x="${x - w / 2}" y="${y - 10}" width="${w}" height="20" rx="4"/><text x="${x}" y="${y + 4}" text-anchor="middle">${esc(text)}</text></g>`; };
  let edges = "", labels = "", nodes = "";
  const trunkBad = m.trunk.rejects.length && m.where === "trunk";
  const aclBad = m.where === "acl";
  // leg A
  edges += `<path class="edge ok flow" d="M${X.cust + nodeW / 2},${yMid} L${X.acl - nodeW / 2},${yMid}" stroke-width="${sw(m.attempts)}"/>`;
  labels += label((X.cust + X.acl) / 2, yMid - 18, `INVITE ×${m.attempts}`, "");
  edges += `<path class="edge ${m.passed ? "ok flow" : "idle"}" d="M${X.acl + nodeW / 2},${yMid} L${X.trunk - nodeW / 2},${yMid}" stroke-width="${sw(m.passed)}"/>`;
  labels += label((X.acl + X.trunk) / 2, yMid - 18, `${m.passed} passed`, m.passed ? "ok" : "");
  if (m.acl.blocked && !m.acl.noise) {
    const by = yMid + 92;
    edges += `<path class="edge bad" d="M${X.acl},${yMid + nodeH / 2} L${X.acl},${by - 14}" stroke-width="${sw(m.acl.blocked)}"/>
      <path class="x" d="M${X.acl - 7},${by - 7} L${X.acl + 7},${by + 7} M${X.acl + 7},${by - 7} L${X.acl - 7},${by + 7}"/>`;
    labels += label(X.acl, by + 24, `${m.acl.blocked} blocked · ${m.acl.top_ip || "unknown IP"}`, "bad");
  }
  if (m.trunk.rejects.length) {
    let ry = yMid + 92;
    m.trunk.rejects.forEach((r) => {
      edges += `<path class="edge bad" d="M${X.trunk},${yMid + nodeH / 2} L${X.trunk},${ry - 14}" stroke-width="${sw(r.n)}"/>
        <path class="x" d="M${X.trunk - 7},${ry - 7} L${X.trunk + 7},${ry + 7} M${X.trunk + 7},${ry - 7} L${X.trunk - 7},${ry + 7}"/>`;
      labels += label(X.trunk, ry + 24, `${r.label} ×${r.n}`, "bad");
      ry += 56;
    });
  }
  // leg B: trunk → carriers
  carriers.forEach((c, i) => {
    edges += `<path class="edge ${c.status}${c.status === "ok" ? " flow" : ""}" d="${curve(X.trunk + nodeW / 2, yMid, X.car - nodeW / 2, cy[i])}" stroke-width="${sw(c.calls)}"/>`;
    if (c.status === "bad") edges += `<path class="x" d="M${X.car - nodeW / 2 - 16},${cy[i] - 7} l14,14 m0,-14 l-14,14"/>`;
    labels += label((X.trunk + nodeW / 2 + X.car - nodeW / 2) / 2, (yMid + cy[i]) / 2, c.calls ? c.label : "no traffic", c.status === "ok" ? "ok" : c.status);
    nodes += node(X.car, cy[i], c.name, `${c.calls} call${c.calls === 1 ? "" : "s"} · carrier`, c.status === "bad" ? "bad" : "");
  });
  // carriers → networks
  nets.forEach((n, j) => {
    n.links.forEach((l) => {
      const i = carriers.findIndex((c) => c.name === l.carrier); if (i < 0) return;
      edges += `<path class="edge ${l.status}${l.status === "ok" ? " flow" : ""}" d="${curve(X.car + nodeW / 2, cy[i], X.net - nodeW / 2, ny[j])}" stroke-width="${Math.max(1, sw(l.calls) * 0.6)}" opacity="${l.status === "warn" ? 1 : 0.45}"/>`;
      if (l.status === "warn") labels += label((X.car + X.net) / 2, (cy[i] + ny[j]) / 2 - 2, `200 OK · no audio ${l.silent}/${l.calls}`, "warn");
    });
    const warn = n.links.some((l) => l.status === "warn");
    nodes += node(X.net, ny[j], n.name, `${n.answered} answered · network`, warn ? "warn" : "");
  });
  nodes += node(X.cust, yMid, m.customer.label, m.customer.sub);
  nodes += node(X.acl, yMid, "IP allow-list", m.acl.enabled ? `${m.acl.entries} entries` : "n/a", aclBad ? "bad" : "");
  nodes += node(X.trunk, yMid, "Plivo Zentrunk", `CPS ${m.trunk.cps_limit} · HA ${m.trunk.ha ? "on" : "off"} · ${m.trunk.media_region}`, trunkBad ? "bad" : "");
  const legs = `<g class="leg"><line x1="${X.cust - nodeW / 2}" y1="22" x2="${X.trunk}" y2="22"/><text x="${(X.cust + X.trunk) / 2}" y="16" text-anchor="middle">LEG A · CUSTOMER → PLIVO</text>
    <line x1="${X.trunk}" y1="30" x2="${X.net + nodeW / 2}" y2="30"/><text x="${(X.trunk + X.net) / 2}" y="44" text-anchor="middle">LEG B · PLIVO → CARRIER → CALLEE</text></g>`;
  return `<svg class="fmap" viewBox="0 0 ${W} ${H}" role="img" aria-label="call flow map">${legs}${edges}${nodes}${labels}</svg>`;
}

// ── Call logs ─────────────────────────────────────────────────────────
function statusBadge(c) {
  if (c.energy != null && c.energy <= 5) return `<span class="badge warn">Silent</span>`;
  if (FAIL.has(c.code)) return `<span class="badge bad">Failed</span>`;
  if (NOT_ANSWERED.has(c.code)) return `<span class="badge">Not answered</span>`;
  return `<span class="badge ok">Completed</span>`;
}
function Logs() {
  crumbs([["Call logs"]]);
  const accts = (S.status?.accounts || []).map((a) => [a.id, a.name]);
  const opt = (v, l, cur) => `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(l)}</option>`;
  const f = S.logs;
  view.innerHTML = `
    <div class="page-head"><div><h1>Call logs</h1><p>Zentrunk call detail records, newest first. Click a call to see its details and call flow.</p></div></div>
    <div class="card">
      <form class="filters" id="lf" autocomplete="off">
        <label class="field">Account<select name="account">${opt("", "All accounts", f.account)}${accts.map(([v, l]) => opt(v, l, f.account)).join("")}</select></label>
        <label class="field">Status<select name="status">${[["all", "All"], ["failed", "Failed"], ["silent", "Silent"], ["answered", "Completed"]].map(([v, l]) => opt(v, l, f.status)).join("")}</select></label>
        <label class="field">Carrier<select name="carrier">${opt("", "All", f.carrier)}${CARRIERS.map((c) => opt(c, c, f.carrier)).join("")}</select></label>
        <label class="field">Hangup cause<select name="code">${opt("", "All", f.code)}${CODES.map(([c, n]) => opt(c, `${c} ${n}`, f.code)).join("")}</select></label>
        <label class="field grow">Search<input name="q" value="${esc(f.q)}" placeholder="Call UUID or number"></label>
        <label class="toggle"><input type="checkbox" id="live" ${S.live ? "checked" : ""}> Live</label>
      </form>
      <div class="table-wrap"><table><thead><tr><th>Start time</th><th>Call UUID</th><th>Account</th><th>From</th><th>To</th>
        <th>Route</th><th class="num">Duration</th><th>Hangup cause</th><th>Status</th></tr></thead><tbody id="lt"></tbody></table></div>
    </div>`;
  $("#lf").addEventListener("input", (e) => {
    const fd = new FormData($("#lf"));
    S.logs = { account: fd.get("account"), status: fd.get("status"), carrier: fd.get("carrier"), code: fd.get("code"), q: fd.get("q") };
    loadLogs();
  });
  $("#lf").addEventListener("submit", (e) => e.preventDefault());
  $("#live").addEventListener("change", (e) => { S.live = e.target.checked; });
  $("#lt").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-uuid]"); if (tr) go(`#/logs/${encodeURIComponent(tr.dataset.uuid)}`);
  });
  loadLogs(true);
  S.timers.push(setInterval(() => { if (S.live) loadLogs(); }, 2000));
}
async function loadLogs(first) {
  const f = S.logs;
  const q = new URLSearchParams({ status: f.status || "all", carrier: f.carrier || "", code: f.code || 0, account: f.account || "", q: f.q || "", limit: 60 });
  let j; try { j = await (await fetch("/public/cdr?" + q, { cache: "no-store" })).json(); } catch (e) { return; }
  const tb = $("#lt"); if (!tb) return;
  if (!j.rows.length) {
    tb.innerHTML = `<tr class="nolink"><td colspan="9"><div class="empty">${j.running ? "No calls match these filters." : "No live traffic. Start a demo from the Overview page."}</div></td></tr>`;
    return;
  }
  tb.innerHTML = j.rows.map((c) => `<tr data-uuid="${esc(c.uuid)}">
    <td>${esc(c.start)}</td><td class="mono">${esc(c.uuid.slice(0, 8))}…</td><td>${esc(c.account)}</td>
    <td class="mono">${esc(c.from)}</td><td class="mono">${esc(c.to)}</td><td>${esc(c.carrier)} → ${esc(c.to_carrier)}</td>
    <td class="num">${c.duration}s</td><td><span class="mono">${esc(c.code)}</span> ${esc(c.cause)}</td><td>${statusBadge(c)}</td></tr>`).join("");
}

// ── Call details ──────────────────────────────────────────────────────
async function CallDetail(r) {
  const short = r.uuid.slice(0, 8) + "…";
  crumbs([["Call logs", "#/logs"], [short]]);
  let d = S.calls[r.uuid];
  if (!d) {
    view.innerHTML = `<div class="card"><div class="empty">Loading…</div></div>`;
    try {
      const res = await fetch("/public/call/" + encodeURIComponent(r.uuid));
      if (!res.ok) throw new Error();
      d = S.calls[r.uuid] = await res.json();
    } catch (e) { view.innerHTML = `<div class="card"><div class="empty">This call is no longer in the live window.<br><a class="btn" href="#/logs">Back to call logs</a></div></div>`; return; }
  }
  const tab = r.params.tab || "overview";
  const failed = FAIL.has(d.code), silent = d.silent;
  const status = silent ? `<span class="badge warn">Silent</span>` : failed ? `<span class="badge bad">Failed</span>` : NOT_ANSWERED.has(d.code) ? `<span class="badge">Not answered</span>` : `<span class="badge ok">Completed</span>`;
  const base = `#/logs/${encodeURIComponent(d.call_uuid)}`;
  view.innerHTML = `
    <div class="page-head"><div><div class="detail-head"><h1>Call details</h1>${status}</div>
      <p class="mono">${esc(d.call_uuid)}</p></div>
      <a class="btn" href="#/logs?code=${d.code}" id="similar">Similar calls</a></div>
    <div class="card">
      <div class="tabs">${[["overview", "Overview"], ["flow", "Call flow"], ["analysis", "Analysis"]].map(([k, l]) =>
        `<a href="${base}?tab=${k}" class="${tab === k ? "on" : ""}">${l}</a>`).join("")}</div>
      <div class="card-b hscroll" id="tabbody"></div></div>`;
  $("#similar").addEventListener("click", (e) => { e.preventDefault(); S.logs = { account: "", status: "all", carrier: "", code: String(d.code), q: "" }; go("#/logs"); });
  const body = $("#tabbody");
  if (tab === "flow") body.innerHTML = seqHtml(d);
  else if (tab === "analysis") body.innerHTML = `<p class="story">${esc(d.story)}</p>
    <dl class="kv" style="margin-top:18px"><dt>Hangup cause</dt><dd>${esc(d.code)} ${esc(d.cause)}</dd><dt>Raised by</dt><dd>${esc(d.source)}</dd>
    <dt>SIP response</dt><dd>${esc(d.sip ?? "–")}</dd><dt>Callee audio energy</dt><dd>${d.audio_energy ?? "–"}${d.audio_energy != null ? " (normal is 40 to 90)" : ""}</dd></dl>`;
  else body.innerHTML = `<dl class="kv">
    <dt>Call UUID</dt><dd class="mono">${esc(d.call_uuid)}</dd><dt>Account</dt><dd>${esc(d.account)}</dd>
    <dt>Direction</dt><dd>${esc(d.direction)}</dd><dt>From</dt><dd class="mono">${esc(d.from)}</dd><dt>To</dt><dd class="mono">${esc(d.to)}</dd>
    <dt>Carrier</dt><dd>${esc(d.carrier)}</dd><dt>Destination network</dt><dd>${esc(d.to_network)}</dd><dt>Trunk</dt><dd class="mono">${esc(d.trunk)}</dd>
    <dt>Start time</dt><dd>${esc(d.initiated)}</dd><dt>Answered</dt><dd>${d.answered ? "Yes" : "No"}</dd>
    <dt>Duration</dt><dd>${d.duration_s}s</dd><dt>Billed duration</dt><dd>${d.bill_s}s</dd>
    <dt>Hangup cause</dt><dd>${esc(d.cause)}</dd><dt>Hangup code</dt><dd class="mono">${esc(d.code)}</dd>
    <dt>SIP response</dt><dd class="mono">${esc(d.sip ?? "–")}</dd><dt>Hangup source</dt><dd>${esc(d.source)}</dd>
    <dt>Callee audio energy</dt><dd>${d.audio_energy ?? "–"}</dd></dl>`;
}

function seqHtml(d) {
  const laneOf = (n) => (/callee/i.test(n) ? d.carrier : /^plivo$/i.test(n) ? "Zentrunk" : n);
  const lanes = [];
  d.ladder.forEach((s) => [s.from, s.to].forEach((n) => { if (n !== "both") { const l = laneOf(n); if (!lanes.includes(l)) lanes.push(l); } }));
  const order = ["Customer SBC", "Customer PBX", "Zentrunk", d.carrier];
  lanes.sort((a, b) => (order.indexOf(a) + 10) % 10 - (order.indexOf(b) + 10) % 10);
  const N = lanes.length, center = (i) => ((i + 0.5) / N) * 100;
  const sub = { "Customer SBC": "customer", "Customer PBX": "customer", Zentrunk: "Plivo SIP trunk" };
  const head = `<div class="lanes" style="grid-template-columns:repeat(${N},1fr);margin-left:76px">${lanes.map((l) => `<div class="lane-h">${esc(l)}<small>${esc(sub[l] || "carrier")}</small></div>`).join("")}</div>`;
  const vlines = lanes.map((_, i) => `<div class="vline" style="left:calc(76px + (100% - 76px) * ${center(i) / 100})"></div>`).join("");
  const rows = d.ladder.map((s, k) => {
    const delay = `animation-delay:${(k * 0.14).toFixed(2)}s`;
    const ts = `<span class="ts">+${s.t.toFixed(3)}s</span>`;
    if (s.from === "both" || s.kind === "media") {
      return `<div class="row">${ts}<div class="note" style="left:calc(76px + (100% - 76px) * 0.5);${delay}">${esc(s.msg)}</div></div>`;
    }
    const a = lanes.indexOf(laneOf(s.from)), b = lanes.indexOf(laneOf(s.to));
    const lo = Math.min(a, b), hi = Math.max(a, b);
    const left = `calc(76px + (100% - 76px) * ${center(lo) / 100})`, width = `calc((100% - 76px) * ${(center(hi) - center(lo)) / 100})`;
    return `<div class="row">${ts}<div class="arrow ${b > a ? "r" : "l"} ${s.kind}" style="left:${left};width:${width};${delay}"><span class="lbl">${esc(s.msg)}</span></div></div>`;
  }).join("");
  return `<div class="seq">${head}<div class="rows">${vlines}${rows}</div></div>
    <p class="muted" style="margin:8px 0 0 76px;font-size:13px">${esc(d.story)}</p>`;
}

// ── Insights ──────────────────────────────────────────────────────────
function Insights() {
  crumbs([["Insights"]]);
  view.innerHTML = `
    <div class="page-head"><div><h1>Insights</h1><p id="ins-sub">Last minute of the incident account, computed from raw call records.</p></div>
      <div class="seg" id="seg">${[["carrier", "By carrier"], ["to_network", "By destination network"], ["hangup_cause", "By hangup cause"]].map(([k, l]) =>
        `<button data-by="${k}" class="${S.insightsBy === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
    <div class="card"><div class="card-b"><div class="bars" id="bars"></div><div class="conclusion" id="concl" hidden></div></div></div>`;
  $("#seg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-by]"); if (!b) return;
    S.insightsBy = b.dataset.by; S.lastChart = null;
    document.querySelectorAll("#seg button").forEach((x) => x.classList.toggle("on", x === b));
    loadInsights();
  });
  if (S.lastChart && S.lastChart.by === S.insightsBy) drawBars(S.lastChart); else loadInsights();
  S.timers.push(setInterval(loadInsights, 3000));
}
async function loadInsights() {
  let j; try { j = await (await fetch(`/public/breakdown?by=${S.insightsBy}`, { cache: "no-store" })).json(); } catch (e) { return; }
  drawBars(j);
}
function drawBars(ev) {
  const el = $("#bars"); if (!el) return;
  if (!ev.bars || !ev.bars.length) { el.innerHTML = `<div class="empty">No traffic for the incident account yet. Start a demo from the Overview page.</div>`; $("#concl").hidden = true; return; }
  const vol = ev.metric === "volume";
  $("#ins-sub").textContent = `${ev.account || "All accounts"} · ${vol ? "calls received vs normal, last 30 seconds" : (ev.metric === "silent" ? "silent calls" : "failures") + " in the last minute"}, computed from raw call records.`;
  const max = vol ? Math.max(1, ...ev.bars.map((b) => b.rate)) : Math.max(0.05, ...ev.bars.map((b) => b.rate));
  const fresh = !el.children.length || el.dataset.by !== ev.by;
  el.dataset.by = ev.by;
  el.innerHTML = ev.bars.map((b) => {
    const hot = vol ? b.rate <= 0.4 : b.rate >= 0.3;
    const n = vol ? `${b.total} of ~${b.expected} calls` : `${pct(b.rate)} of ${b.total}`;
    return `<div class="bar"><span>${esc(b.label)}</span><div class="track"><div class="fill ${hot ? "hot" : ""}" style="width:${fresh ? 0 : (b.rate / max) * 100}%" data-w="${(b.rate / max) * 100}"></div></div>
    <span class="n">${n}</span></div>`;
  }).join("");
  if (fresh) requestAnimationFrame(() => el.querySelectorAll(".fill").forEach((f) => { f.style.width = f.dataset.w + "%"; }));
  const c = $("#concl"); c.hidden = !ev.conclusion; c.textContent = ev.conclusion || "";
}

// ── Accounts ──────────────────────────────────────────────────────────
function spark(values, hot) {
  const v = (values || []).filter((x) => x != null);
  if (v.length < 2) return "";
  const max = Math.max(0.1, ...v), w = 120, h = 24;
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * w},${h - (x / max) * (h - 3) - 1.5}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline fill="none" stroke="${hot ? "#b91c1c" : "#94a3b8"}" stroke-width="1.5" points="${pts}"/></svg>`;
}
function Accounts(r, refresh) {
  crumbs([["Accounts"]]);
  const rows = (S.status?.accounts || []).slice().sort((a, b) => (b.spend || 0) - (a.spend || 0));
  const incId = S.status?.incident?.account_id;
  const body = rows.length ? rows.map((a) => {
    const fr = a.fail_rate || 0, sr = a.silent_rate || 0, vr = a.volume_ratio ?? 1;
    const bad = a.id === incId && !S.status?.session?.ended;
    const st = bad ? `<span class="badge bad">Incident</span>` : (fr >= 0.1 || sr >= 0.1 || vr <= 0.6) ? `<span class="badge warn">Degraded</span>` : `<span class="badge ok">Healthy</span>`;
    return `<tr class="nolink"><td><b style="font-weight:500">${esc(a.name)}</b></td><td class="num">${inr(a.spend)}</td><td class="num">${a.calls ?? 0}</td>
      <td class="num">${pct(fr)}</td><td class="num">${pct(sr)}</td><td class="num">${pct(vr)}</td><td>${spark(a.spark, bad)}</td><td>${st}</td></tr>`;
  }).join("") : `<tr class="nolink"><td colspan="8"><div class="empty">No live traffic. Start a demo from the Overview page.</div></td></tr>`;
  if (refresh && $("#acct-body")) { $("#acct-body").innerHTML = body; return; }
  view.innerHTML = `<div class="page-head"><div><h1>Accounts</h1><p>Every account on the platform, ordered by daily spend. Last minute of traffic.</p></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Account</th><th class="num">Spend / day</th><th class="num">Calls / min</th>
      <th class="num">Failure rate</th><th class="num">Silent rate</th><th class="num">Volume vs normal</th><th>Failure trend</th><th>Status</th></tr></thead>
      <tbody id="acct-body">${body}</tbody></table></div></div>`;
}

// ── boot ──────────────────────────────────────────────────────────────
// Deep link into logs with a code filter (from "Similar calls")
(function initFromHash() {
  const r = parseRoute();
  if (r.name === "logs" && r.params.code) S.logs.code = r.params.code;
})();
route();
poll();
connectScreen();
