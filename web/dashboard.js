"use strict";

const $ = (id) => document.getElementById(id);
const LIVE_STATES = ["SUSPECTED", "ALERTED", "CALLING", "IN_CALL", "RETRY", "TELEGRAM_SENT",
  "UNSURE_TOLD", "WAITING_FOR_EVIDENCE", "CONFIRM_ACTIONS", "EXECUTING"];

let lastConf = -1;

function post(path) {
  return fetch(path, { method: "POST" }).then((r) => r.json());
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function pct(x) { return Math.round((x || 0) * 100) + "%"; }

function render(cf) {
  // Status line
  $("incident-id").textContent = cf.incident_id || "—";
  $("customer").textContent = cf.alert ? cf.alert.customer_name : "—";
  const badge = $("state-badge");
  badge.textContent = cf.state;
  badge.className = "badge" + (cf.state === "RESOLVED" ? " resolved" : LIVE_STATES.includes(cf.state) ? " live" : "");
  $("failrate").textContent = cf.alert ? pct(cf.alert.failure_rate) : "—";

  // Confidence gauge
  const conf = cf.top_confidence || 0;
  const noIncident = !cf.alert;
  const recovered = cf.verdict === "recovered";
  const resolved = cf.state === "RESOLVED";
  $("conf-title").textContent = resolved ? "Diagnosis at resolution — carrier rejection"
                                         : "Confidence — carrier rejection";
  $("card-confidence").classList.toggle("dim", noIncident || recovered);
  const hideNum = noIncident || recovered;  // a share-of-failures % means nothing when nothing is failing
  $("gauge-fill").style.width = (hideNum ? 0 : Math.round(conf * 100)) + "%";
  $("conf-value").textContent = hideNum ? "—" : pct(conf);
  const pill = $("verdict-pill");
  pill.textContent = noIncident ? "no active incident" : cf.verdict;
  pill.className = "pill " + (noIncident ? "idle" : cf.verdict);
  $("conf-note").textContent = noIncident ? "Watching traffic. Confidence appears once an alert fires."
    : recovered ? `Traffic recovered: ${pct(cf.recent_fail_rate)} failing over the last 20 s. No live cause to report.`
    : resolved ? "Frozen at the moment the incident was resolved."
    : "";
  if (!hideNum && Math.round(conf * 100) !== Math.round(lastConf * 100)) {
    const tick = document.createElement("span");
    tick.textContent = pct(conf);
    $("conf-history").appendChild(tick);
    lastConf = conf;
  }

  // Hypotheses
  const hs = $("hypotheses");
  hs.innerHTML = "";
  (cf.hypotheses || []).forEach((h) => {
    const li = document.createElement("li");
    const ruled = h.status === "ruled_out";
    const confTxt = (h.status === "leading" || h.status === "confirmed") ? " " + pct(h.confidence) : "";
    li.innerHTML =
      `<div class="htitle ${ruled ? "ruled" : ""}">${h.title}` +
      `<span class="status s-${h.status}">${h.status.replace("_", " ")}${confTxt}</span></div>` +
      (h.reason ? `<div class="reason">${h.reason}</div>` : "");
    hs.appendChild(li);
  });

  // Milestones
  const m = cf.milestones || {};
  document.querySelectorAll("#milestones li").forEach((li) => {
    const k = li.getAttribute("data-k");
    const b = li.querySelector("b");
    b.textContent = fmtTime(m[k]);
    b.className = m[k] ? "done" : "";
  });

  // Live call
  const calls = cf.calls || [];
  const active = [...calls].reverse().find((a) => ["ringing", "answered"].includes(a.state));
  const cs = $("call-state");
  if (active) {
    cs.textContent = (active.purpose === "callback" ? "Callback — " : "Call — ") + active.state;
    cs.className = "call-state " + active.state;
    $("call-detail").textContent =
      `purpose: ${active.purpose}` + (active.verdict_told !== "none" ? `, told: ${active.verdict_told}` : "");
  } else if (cf.state === "SUSPECTED") {
    cs.textContent = "Suspected outage — confirming…";
    cs.className = "call-state ringing";
    $("call-detail").textContent = "Calling only if failures persist (a blip just gets a Telegram note).";
  } else if (cf.state === "AUTO_RECOVERED") {
    const n = [...(cf.actions || [])].reverse().find((a) => a.kind === "telegram_notice");
    cs.textContent = "Mild outage — recovered on its own";
    cs.className = "call-state answered";
    $("call-detail").textContent = n ? (n.status === "done" ? "Telegram sent: " : "Telegram failed: ") + (n.payload.text || n.error || "") : "Sending Telegram summary…";
  } else if (cf.state === "WAITING_FOR_EVIDENCE") {
    cs.textContent = "Waiting for more evidence…";
    cs.className = "call-state";
    $("call-detail").textContent = "Will call back once confident.";
  } else {
    cs.textContent = calls.length ? "Call ended" : "No active call";
    cs.className = "call-state";
    $("call-detail").textContent = "";
  }

  // Transcript (what was said on the phone, via the proxy)
  const tr = cf.transcript || [];
  $("transcript").innerHTML = tr.slice(-8).map((t) =>
    `<div class="turn ${t.role}"><b>${t.role === "agent" ? "Agent" : "You"}:</b> ${
      String(t.text).replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]))}</div>`
  ).join("");

  // Actions
  const ticket = (cf.actions || []).find((a) => a.kind === "ticket");
  const tc = $("ticket-card");
  if (!ticket) {
    tc.className = "action muted"; tc.textContent = "No ticket yet";
  } else if (ticket.status === "done") {
    tc.className = "action done";
    tc.innerHTML = `Ticket <b>${ticket.external_id}</b> raised ` +
      `(attempt ${ticket.attempts}) — <a href="${ticket.external_url}" target="_blank">open in Linear</a>`;
  } else if (ticket.status === "failed") {
    tc.className = "action failed";
    tc.innerHTML = `Ticket failed: ${ticket.error || ""} — retrying…`;
  } else {
    tc.className = "action"; tc.textContent = `Ticket ${ticket.status} (attempts: ${ticket.attempts})`;
  }
  $("email-draft").textContent = cf.email_draft || "—";
  $("email-draft").className = "email" + (cf.email_draft ? "" : " muted");

  // Evidence
  const ev = $("evidence");
  ev.innerHTML = "";
  (cf.evidence || []).forEach((e) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="chk">${e.check}</span><br>${e.summary}`;
    ev.appendChild(li);
  });
}

document.getElementById("hangup")?.addEventListener("click", () => post("/demo/hangup"));

function connect() {
  const es = new EventSource("/state/stream");
  es.onmessage = (e) => render(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(connect, 1500); };
}

function clearHistory() { lastConf = -1; $("conf-history").innerHTML = ""; }
$("trigger").onclick = () => { clearHistory(); post("/demo/trigger"); };
$("reset").onclick = () => { clearHistory(); post("/demo/reset"); };
$("sim-start").onclick = () => { if (["IDLE", "RESOLVED"].includes($("state-badge").textContent)) clearHistory(); post("/sim/start"); };
$("sim-stop").onclick = () => post("/sim/stop");
$("sim-pass").onclick = () => post("/sim/mode?m=pass");
$("sim-fail").onclick = () => post("/sim/mode?m=fail");

connect();
fetch("/state").then((r) => r.json()).then(render);
