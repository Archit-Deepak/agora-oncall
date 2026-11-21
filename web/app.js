const $ = (id) => document.getElementById(id);
const AGENT_UID = 1001;

let client = null;
let micTrack = null;
let session = null;
let pollTimer = null;

function setStatus(text, cls) {
  $("status").textContent = text;
  $("status").className = `badge ${cls}`;
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json();
}

async function startCall() {
  $("start").disabled = true;
  setStatus("Connecting…", "connecting");
  try {
    session = await post("/api/session");
    client = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });

    client.on("user-published", async (user, mediaType) => {
      await client.subscribe(user, mediaType);
      if (mediaType === "audio") user.audioTrack.play();
    });
    client.enableAudioVolumeIndicator();
    client.on("volume-indicator", (vols) => {
      const agent = vols.find((v) => v.uid === AGENT_UID);
      $("avatar").classList.toggle("speaking", !!agent && agent.level > 5);
    });
    client.on("user-left", (user) => {
      if (user.uid === AGENT_UID) setStatus("Agent left", "idle");
    });

    await client.join(session.app_id, session.channel, session.token, session.uid);
    micTrack = await AgoraRTC.createMicrophoneAudioTrack({ AEC: true, ANS: true, AGC: true });
    await client.publish([micTrack]);

    const res = await post("/api/agent/start", { channel: session.channel });
    if (!res.agent_id) throw new Error(`Agent failed to start: ${JSON.stringify(res)}`);

    setStatus("Live", "live");
    $("hint").textContent = `Channel ${session.channel} · agent ${res.agent_id}`;
    $("mute").disabled = false;
    $("hangup").disabled = false;
    pollTimer = setInterval(refreshTurns, 1000);
  } catch (e) {
    console.error(e);
    setStatus("Error", "error");
    $("hint").textContent = e.message;
    await cleanup();
    $("start").disabled = false;
  }
}

async function cleanup() {
  clearInterval(pollTimer);
  if (session) await post("/api/agent/stop", { channel: session.channel }).catch(() => {});
  if (micTrack) { micTrack.close(); micTrack = null; }
  if (client) { await client.leave().catch(() => {}); client = null; }
}

async function hangup() {
  $("hangup").disabled = true;
  $("mute").disabled = true;
  await cleanup();
  setStatus("Idle", "idle");
  $("start").disabled = false;
}

function toggleMute() {
  if (!micTrack) return;
  const muted = !micTrack.enabled;
  micTrack.setEnabled(muted);
  $("mute").textContent = muted ? "Mute" : "Unmute";
}

async function refreshTurns() {
  const turns = await (await fetch("/api/turns")).json();
  $("turns").innerHTML = turns.slice().reverse().map((t) => `
    <tr>
      <td class="num">${t.turn_id ?? ""}</td>
      <td>${escape(t.user)}</td>
      <td>${escape(t.agent)}${t.error ? ` <em style="color:#ff8a8a">${escape(t.error)}</em>` : ""}</td>
      <td class="num">${t.ttft_ms ?? "…"} ms</td>
      <td class="num">${t.total_ms ?? "…"} ms</td>
    </tr>`).join("");
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("start").onclick = startCall;
$("hangup").onclick = hangup;
$("mute").onclick = toggleMute;
refreshTurns();
