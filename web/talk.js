// Browser half of a demo call. The agent is already in an Agora channel once a
// failure has held; this window joins it with the microphone. Nothing here dials a phone.
"use strict";

const $ = (id) => document.getElementById(id);
let client = null;
let mic = null;
let joined = false;
let joining = false;
let stopped = false;
let hold = false;

function show(title, detail, kind) {
  $("title").textContent = title;
  $("detail").textContent = detail;
  $("orb").className = "orb" + (kind ? " " + kind : "");
}

async function connect(voice) {
  if (joining || joined || stopped) return;
  joining = true;
  show("Connecting", "Allow the microphone when the browser asks.", "");
  try {
    if (typeof AgoraRTC === "undefined") throw new Error("The voice library did not load. Check the network and reopen this window.");
    client = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
    client.on("user-published", async (user, mediaType) => {
      await client.subscribe(user, mediaType);
      if (mediaType === "audio" && user.audioTrack) user.audioTrack.play();
    });
    await client.join(voice.app_id, voice.channel, voice.token, Number(voice.uid));
    mic = await AgoraRTC.createMicrophoneAudioTrack({ AEC: true, ANS: true, AGC: true });
    await client.publish([mic]);
    joined = true;
    $("hangup").disabled = false;
    $("retry").hidden = true;
    show("Live", "You're talking to the on-call agent. Ask what's going on, then say yes when it proposes the fix.", "live");
  } catch (e) {
    hold = true;
    show("Microphone needed", e && e.message ? e.message : "Allow the microphone, then press the button.", "bad");
    $("retry").hidden = false;
    await dropLocal();
  } finally {
    joining = false;
  }
}

async function dropLocal() {
  joined = false;
  if (mic) { mic.close(); mic = null; }
  if (client) { const c = client; client = null; await c.leave().catch(() => {}); }
}

async function hangup() {
  stopped = true;
  $("hangup").disabled = true;
  $("retry").hidden = true;
  await dropLocal();
  await fetch("/public/voice/hangup", { method: "POST" }).catch(() => {});
  show("Hung up", "You can close this window.", "");
}

async function tick() {
  if (stopped || joined || joining || hold) return;
  let body;
  try {
    body = await (await fetch("/public/status")).json();
  } catch (e) {
    show("Reconnecting", "The page couldn't reach the demo. This keeps trying.", "bad");
    return;
  }
  const se = body.session;
  if (!se || se.mode !== "browser") {
    show("Waiting", "This window connects when you press Talk in the browser on the demo page.", "");
    return;
  }
  if (se.ended) {
    stopped = true;
    show("Ended", se.outcome || "The demo ended.", "");
    await dropLocal();
    return;
  }
  if (body.voice) {
    await connect(body.voice);
    return;
  }
  show("Waiting", "Nothing connects until a high-spend account starts failing. It connects about 15 seconds later. Keep this window open.", "");
}

$("hangup").onclick = hangup;
$("retry").onclick = () => { hold = false; stopped = false; tick(); };
setInterval(tick, 1000);
tick();
