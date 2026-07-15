// Plays audio handed over by the background worker. Runs in the extension's own
// context, so page CSP / mixed-content restrictions don't apply.
let current = null;

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.target !== "offscreen") return;
  if (msg.type === "play") {
    try {
      if (current) { current.pause(); current = null; }
      current = new Audio(msg.dataUrl);
      current.play().catch(() => {});
    } catch (e) { /* ignore */ }
  } else if (msg.type === "stop") {
    try { if (current) { current.pause(); current = null; } } catch (e) {}
  }
});
