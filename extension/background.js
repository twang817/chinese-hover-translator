// Background service worker: does the cross-origin fetch to the Mac server
// (avoids page CORS/CSP) and caches results per phrase.
//
// Two-layer cache: an in-memory Map for the current worker lifetime, backed by
// chrome.storage.local so results survive the MV3 worker being suspended and
// even a browser restart.

const DEFAULT_SERVER = "http://localhost:5001";
const mem = new Map();               // text -> {segments}  (fast, ephemeral)
const KEY = (text) => "c:" + text;   // storage key namespace

async function getServer() {
  const { server } = await chrome.storage.local.get("server");
  return (server || DEFAULT_SERVER).replace(/\/+$/, "");
}

async function translate(text) {
  if (mem.has(text)) return mem.get(text);

  const k = KEY(text);
  const stored = await chrome.storage.local.get(k);
  if (stored && stored[k]) {          // persisted from a previous session
    mem.set(text, stored[k]);
    return stored[k];
  }

  const server = await getServer();
  const r = await fetch(`${server}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error(`server ${r.status}`);
  const data = await r.json();

  mem.set(text, data);
  chrome.storage.local.set({ [k]: data }).catch(() => {});   // write-through
  return data;
}

function abToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

// Fetch TTS audio in the background (extension context, no mixed-content), then
// play it via an offscreen document so the visited page's CSP can't block it.
async function ttsDataUrl(text, voice) {
  const server = await getServer();
  const url = `${server}/tts?voice=${encodeURIComponent(voice)}&text=${encodeURIComponent(text)}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`tts ${r.status}`);
  const buf = await r.arrayBuffer();
  return "data:audio/wav;base64," + abToBase64(buf);
}

async function ensureOffscreen() {
  try {
    if (chrome.offscreen.hasDocument && (await chrome.offscreen.hasDocument())) return;
  } catch (e) { /* fall through to create */ }
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["AUDIO_PLAYBACK"],
      justification: "Play Chinese text-to-speech audio.",
    });
  } catch (e) { /* already exists / race */ }
}

async function playTts(text, voice) {
  const dataUrl = await ttsDataUrl(text, voice);
  await ensureOffscreen();
  chrome.runtime.sendMessage({ target: "offscreen", type: "play", dataUrl }).catch(() => {});
}

async function clearCache() {
  mem.clear();
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((key) => key.startsWith("c:"));
  if (keys.length) await chrome.storage.local.remove(keys);
  return keys.length;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.target === "offscreen") return;   // handled by the offscreen doc
  if (msg && msg.type === "translate") {
    translate(msg.text)
      .then(sendResponse)
      .catch((e) => sendResponse({ error: String(e.message || e) }));
    return true; // async reply
  }
  if (msg && msg.type === "tts") {
    playTts(msg.text, msg.voice || "zf_xiaoxiao")
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ error: String(e.message || e) }));
    return true;
  }
  if (msg && msg.type === "clearCache") {
    clearCache()
      .then((n) => sendResponse({ cleared: n }))
      .catch((e) => sendResponse({ error: String(e.message || e) }));
    return true;
  }
});
