// Voice for the supplier portal (web/console.html), using the ElevenLabs Agents SDK.
//
// Voice is only ears and a mouth. Tracer's brain stays where it was:
//   ask_tracer  → the console's own send(), i.e. POST /chat (agent/intake.py). Saying
//                 "yes, go ahead" goes through exactly the same gate as typing it.
//   lookups     → read-only routes in web/voice.py: one unit, order, shipment or
//                 customer, read live from the warehouse, order and carrier systems.
// The tools run HERE in the browser ("client tools"), so ElevenLabs never gets our
// API key or a route into our server. Tool names must match scripts/setup_voice_agent.py.
//
// This is a module, but the console's top-level `send`, `push`, `items`, `rid` and
// `busy` (declared in its classic <script>) are visible here as globals.

const SDK_URLS = [
  "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.36.1/+esm",
  "https://esm.sh/@elevenlabs/client@1.36.1",
];

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const enc = encodeURIComponent;
let conversation = null;
let Conversation = null;
let lastHeard = "";        // the supplier's latest spoken sentence
let showNextReply = false; // a lookup answer is spoken by the agent: copy it into the chat

async function loadSdk() {
  if (Conversation) return Conversation;
  for (const url of SDK_URLS) {
    try { ({ Conversation } = await import(url)); return Conversation; } catch (e) { /* next CDN */ }
  }
  throw new Error("couldn't load the ElevenLabs SDK (offline?)");
}

function setStatus(text) { $("vstatus").textContent = text; }

// The supplier said something for Tracer: put it through the console's chat, return the reply.
async function askTracer({ message }) {
  const text = (message || lastHeard || "").trim();
  if (!text) return "I didn't catch that. Please say it again.";
  for (let i = 0; busy && i < 40; i++) await sleep(250);   // a typed message is still in flight
  const before = items.length;
  showNextReply = false;                                    // send() shows both sides itself
  setStatus("Tracer is thinking…");
  await send(text);
  const reply = items.slice(before).filter((i) => i.kind === "tracer" || i.kind === "error").pop();
  return reply ? reply.text : "Tracer didn't answer just now. Please try again.";
}

// Read-only lookups. Each returns a JSON string the agent reads as the tool result.
async function read(path, label) {
  setStatus(label);
  if (lastHeard) push({ kind: "supplier", text: lastHeard });
  showNextReply = true;
  try {
    const r = await fetch(path);
    const body = await r.json().catch(() => ({}));
    return JSON.stringify(r.ok ? body : { error: body.detail || `HTTP ${r.status}` });
  } catch (e) {
    return JSON.stringify({ error: "the portal's server is not reachable" });
  }
}

const clientTools = {
  ask_tracer: askTracer,
  lookup_warehouse_unit: ({ unit_id }) =>
    read(`/voice/lookup/unit/${enc(unit_id)}`, `Asking the warehouse about ${unit_id}…`),
  lookup_order: ({ order_id }) =>
    read(`/voice/lookup/order/${enc(order_id)}`, `Asking the order system about ${order_id}…`),
  lookup_shipment: ({ shipment_id }) =>
    read(`/voice/lookup/shipment/${enc(shipment_id)}`, `Asking the carrier about ${shipment_id}…`),
  check_customer_notices: ({ customer_id }) =>
    read(`/voice/lookup/notices/${enc(customer_id)}${rid ? `?recall_id=${enc(rid)}` : ""}`,
         `Counting the notices sent to ${customer_id}…`),
};

function onMessage({ message, role, source }) {
  if ((role || source) === "user") { lastHeard = message; return; }
  if (showNextReply) { push({ kind: "tracer", rid, text: message }); showNextReply = false; }
}

async function start() {
  $("vbtn").disabled = true;
  try {
    setStatus("Asking for the microphone…");
    await navigator.mediaDevices.getUserMedia({ audio: true });
    setStatus("Connecting…");
    const [SDK, res] = await Promise.all([loadSdk(), fetch("/voice/signed-url")]);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    conversation = await SDK.startSession({
      signedUrl: body.signed_url,
      connectionType: "websocket",
      clientTools,
      onMessage,
      onModeChange: ({ mode }) => setStatus(mode === "speaking" ? "Tracer is speaking…" : "Listening…"),
      onStatusChange: ({ status }) => { if (status === "disconnected") stopped(); },
      onError: (message) => setStatus(`Voice error: ${message}`),
    });
    $("vbtn").textContent = "■ End call";
    $("vbtn").classList.add("live");
  } catch (e) {
    setStatus(e.name === "NotAllowedError" ? "The microphone is blocked for this page."
                                           : `Voice isn't available: ${e.message}`);
    conversation = null;
  } finally {
    $("vbtn").disabled = false;
  }
}

function stopped() {
  if (!conversation) return;
  conversation = null;
  $("vbtn").textContent = "🎙 Talk";
  $("vbtn").classList.remove("live");
  setStatus("Call ended.");
}

async function toggle() {
  if (conversation) { const c = conversation; stopped(); await c.endSession(); } else { await start(); }
}

// ---------- narrator: read Tracer's updates aloud as they appear in the chat ----------
// The console already writes each key moment in plain words, with the supplier's next
// step ("Press Restart…", "approve to start"). The narrator only speaks those: it adds
// no wording of its own. Off by default; silent during a voice call (the agent talks).

let narrating = false;
let seen = 0;            // items[0..seen) have been considered
const queue = [];
let speaking = false;
let player = null;

function spoken(item) {
  if (item.kind === "tracer" || item.kind === "error") return item.text;
  if (item.kind === "moment") return `${item.title}. ${item.text}`;
  if (item.kind === "searching") return "I'm finding everywhere this lot is. Nothing changes until you approve.";
  if (item.kind === "scope") return "I've found where the lot is, and my plan is on screen. Review it, then approve to start, or cancel.";
  if (item.kind === "report") return "The recall report is ready on screen. Check anything marked as needing you, then download it.";
  return null;   // the supplier's own messages and the progress card are not read out
}

async function drain() {
  if (speaking) return;
  speaking = true;
  while (narrating && queue.length) {
    const text = queue.shift();
    try {
      const r = await fetch("/voice/speak", { method: "POST", headers: { "content-type": "application/json" },
                                              body: JSON.stringify({ text }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      player = new Audio(url);
      await new Promise((done) => { player.onended = player.onerror = done; player.play().catch(done); });
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(`Narrator: ${e.message}`);
      queue.length = 0;
    }
  }
  player = null;
  speaking = false;
}

function watch() {
  if (items.length < seen) seen = items.length;          // the chat was reset
  const fresh = items.slice(seen);
  seen = items.length;
  if (!narrating || conversation) return;                // off, or the voice agent is talking
  for (const item of fresh) { const text = spoken(item); if (text) queue.push(text); }
  if (queue.length > 4) queue.splice(0, queue.length - 4);  // fallen behind: keep the latest
  drain();
}

function toggleNarrator() {
  narrating = !narrating;
  $("nbtn").setAttribute("aria-pressed", String(narrating));
  $("nbtn").textContent = narrating ? "🔊 Narrating" : "🔈 Narrate";
  seen = items.length;                                   // start from now, not from history
  if (narrating) { queue.push("Narration is on. I'll read out each update as it happens."); drain(); }
  else { queue.length = 0; if (player) player.pause(); setStatus(""); }
}

async function init() {
  $("vbtn").onclick = toggle;
  $("nbtn").onclick = toggleNarrator;
  setInterval(watch, 400);
  try {
    const cfg = await (await fetch("/voice/config")).json();
    if (!cfg.enabled) {
      $("vbtn").disabled = true;
      $("vbtn").title = `Voice is off: set ${cfg.missing.join(" and ")} in .env`;
    }
    if (!cfg.narrator) {
      $("nbtn").disabled = true;
      $("nbtn").title = "Narration is off: set ELEVENLABS_API_KEY in .env";
    }
  } catch (e) { $("vbtn").disabled = true; $("nbtn").disabled = true; }
}

init();
