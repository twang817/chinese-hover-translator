// Background service worker: does the cross-origin fetch to the Mac server
// (avoids page CORS/CSP) and caches results per phrase.
//
// Two-layer cache: an in-memory Map for the current worker lifetime, backed by
// chrome.storage.local so results survive the MV3 worker being suspended and
// even a browser restart.

const DEFAULT_SERVER = "http://MLTX-TWANG.local:5001";
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

async function clearCache() {
  mem.clear();
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((key) => key.startsWith("c:"));
  if (keys.length) await chrome.storage.local.remove(keys);
  return keys.length;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "translate") {
    translate(msg.text)
      .then(sendResponse)
      .catch((e) => sendResponse({ error: String(e.message || e) }));
    return true; // async reply
  }
  if (msg && msg.type === "clearCache") {
    clearCache()
      .then((n) => sendResponse({ cleared: n }))
      .catch((e) => sendResponse({ error: String(e.message || e) }));
    return true;
  }
});
