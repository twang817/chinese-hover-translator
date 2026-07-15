// Content script: on hover over Chinese text, show a popup with pinyin,
// translation, notes, and a play button. Rendered in a Shadow DOM so the
// page's CSS can't break it (and ours can't leak onto the page).

const HAN = /[㐀-䶿一-鿿]/;
const SPLIT = /[。！？!?；;\n\r\t]/;   // sentence boundaries
const VOICE = "zf_xiaoxiao";
const HOVER_DELAY = 400;              // ms mouse must rest before firing
const MAX_LEN = 200;                  // don't translate huge blobs

let host, shadow, box, currentText = null;
let moveTimer = null, hideTimer = null, lastXY = { x: 0, y: 0 };

const CSS = `
.tip{ position:fixed; max-width:360px; background:#fff; color:#1a1a1a;
  border:1px solid #e2e2e2; border-radius:10px; box-shadow:0 6px 24px rgba(0,0,0,.18);
  padding:10px 12px; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
.seg{ margin-bottom:8px } .seg:last-child{ margin-bottom:0 }
.o{ font-size:16px } .p{ color:#8a6d3b; font-size:13px; margin-top:1px }
.t{ margin-top:2px } .n{ margin-top:5px; font-size:12.5px; background:#fff8e6;
  border:1px solid #f0d98a; color:#7a5b00; border-radius:6px; padding:4px 7px }
.play{ cursor:pointer; margin-left:4px; padding:0 5px; font-size:15px; opacity:.55; user-select:none } .play:hover{ opacity:1 }
.msg{ color:#888; font-size:13px } .err{ color:#c0392b; font-size:12.5px }`;

function ensureTip() {
  if (host) return;
  host = document.createElement("div");
  host.id = "__zh_hover_tip__";
  host.style.cssText = "all:initial; position:fixed; z-index:2147483647; top:0; left:0;";
  shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = CSS;
  box = document.createElement("div");
  box.className = "tip";
  box.style.display = "none";
  shadow.append(style, box);
  (document.documentElement || document.body).appendChild(host);
  // Keep the tooltip open while the pointer is on it (so you can reach the 🔊).
  host.addEventListener("mouseenter", cancelHide);
  host.addEventListener("mouseleave", scheduleHide);
}

// Position so the tooltip's first line sits over the hovered text line
// (PADX/PADY match .tip padding, so the content origin aligns with the text).
function place(rect) {
  box.style.display = "block";
  const r = box.getBoundingClientRect();
  const PADX = 12, PADY = 10, edge = 6;
  let bx = rect.left - PADX;
  let by = rect.top - PADY;
  if (bx + r.width > innerWidth - edge) bx = innerWidth - edge - r.width;
  if (bx < edge) bx = edge;
  if (by + r.height > innerHeight - edge) by = innerHeight - edge - r.height;
  if (by < edge) by = edge;
  box.style.left = bx + "px";
  box.style.top = by + "px";
}

function hide() { if (box) box.style.display = "none"; currentText = null; }
function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 500); }
function cancelHide() { clearTimeout(hideTimer); }

// After the extension is reloaded/updated, content scripts already injected in
// open tabs are orphaned and chrome.runtime throws. Detect that and go quiet.
function extAlive() {
  try { return !!(chrome.runtime && chrome.runtime.id); } catch (e) { return false; }
}
function teardown() {
  try {
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("scroll", hide, true);
  } catch (e) { /* ignore */ }
  hide();
}
function browserSpeak(text) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN";
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  } catch (e) { /* best effort */ }
}

function sentenceAt(x, y) {
  let range = null;
  if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(x, y);
  else if (document.caretPositionFromPoint) {
    const p = document.caretPositionFromPoint(x, y);
    if (p) { range = document.createRange(); range.setStart(p.offsetNode, p.offset); }
  }
  if (!range) return null;
  const node = range.startContainer;
  if (!node || node.nodeType !== Node.TEXT_NODE) return null;
  const txt = node.textContent;
  if (!txt || !HAN.test(txt)) return null;
  let s = range.startOffset, e = s;
  while (s > 0 && !SPLIT.test(txt[s - 1])) s--;
  while (e < txt.length && !SPLIT.test(txt[e])) e++;
  const sent = txt.slice(s, e).trim();
  if (!sent || !HAN.test(sent)) return null;
  // rect of the hovered line of this sentence, so the tooltip can cover it
  let rect = null;
  try {
    const rr = document.createRange();
    rr.setStart(node, s); rr.setEnd(node, e);
    const rects = rr.getClientRects();
    for (const rc of rects) { if (y >= rc.top - 2 && y <= rc.bottom + 2) { rect = rc; break; } }
    if (!rect) rect = rects[0] || rr.getBoundingClientRect();
  } catch (err) { /* fall back to cursor */ }
  if (!rect) rect = { left: x, top: y, height: 18 };
  return { text: sent.slice(0, MAX_LEN), rect: { left: rect.left, top: rect.top, height: rect.height } };
}

function onMove(e) {
  if (host && e.target === host) { cancelHide(); clearTimeout(moveTimer); return; }  // over the tooltip
  lastXY = { x: e.clientX, y: e.clientY };
  clearTimeout(moveTimer);
  moveTimer = setTimeout(fire, HOVER_DELAY);
}

function fire() {
  const { x, y } = lastXY;
  const info = sentenceAt(x, y);
  if (!info) { scheduleHide(); return; }
  cancelHide();
  if (info.text === currentText) return;
  currentText = info.text;
  ensureTip();
  box.innerHTML = '<div class="msg">译…</div>';
  place(info.rect);
  const text = info.text, rect = info.rect;
  if (!extAlive()) { teardown(); return; }
  try {
    chrome.runtime.sendMessage({ type: "translate", text }, (resp) => {
      if (currentText !== text) return;              // moved on already
      if (chrome.runtime.lastError || !resp || resp.error) {
        const m = (resp && resp.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message) || "error";
        box.innerHTML = '<div class="err">⚠ ' + m + "</div>";
        place(rect);
        return;
      }
      render(resp.segments || [], rect);
    });
  } catch (e) { teardown(); }
}

function render(segs, rect) {
  box.innerHTML = "";
  if (!segs.length) { box.innerHTML = '<div class="err">no result</div>'; place(rect); return; }
  for (const s of segs) {
    const seg = document.createElement("div"); seg.className = "seg";
    const o = document.createElement("div"); o.className = "o"; o.textContent = s.original;
    if (s.pinyin) {
      const play = document.createElement("span"); play.className = "play"; play.textContent = "🔊";
      play.addEventListener("click", () => speak(s.original));
      o.appendChild(play);
    }
    seg.appendChild(o);
    if (s.pinyin) { const p = document.createElement("div"); p.className = "p"; p.textContent = s.pinyin; seg.appendChild(p); }
    if (s.translation) { const t = document.createElement("div"); t.className = "t"; t.textContent = s.translation; seg.appendChild(t); }
    if (s.notes) { const n = document.createElement("div"); n.className = "n"; n.textContent = "💡 " + s.notes; seg.appendChild(n); }
    box.appendChild(seg);
  }
  place(rect);
}

function speak(text) {
  if (!extAlive()) { browserSpeak(text); return; }
  // The background fetches the audio and plays it in an offscreen document, so
  // the page's CSP / mixed-content rules can't block it. Fall back to the
  // browser's built-in voice only if that whole path errors.
  try {
    chrome.runtime.sendMessage({ type: "tts", text, voice: VOICE }, (resp) => {
      if (chrome.runtime.lastError || !resp || resp.error) browserSpeak(text);
    });
  } catch (e) { browserSpeak(text); }
}

document.addEventListener("mousemove", onMove, true);
document.addEventListener("scroll", hide, true);
window.addEventListener("blur", hide);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
