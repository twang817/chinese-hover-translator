"""
Chinese-English translation web app (FastAPI / ASGI, shared multi-device session).

- Serves a single-page ChatGPT-style UI.
- All connected devices share ONE synchronized feed: a message sent from any
  device (e.g. dictated on a phone) is processed once and broadcast to every
  device (e.g. shown on the PC, where you copy + paste it).
- The LLM (LM Studio, OpenAI-compatible) parses the pasted chat log into
  segments and translates each; pinyin is added deterministically with pypinyin.
- A message starting with ">" is a question to the assistant (chat mode) rather
  than text to translate.

Run:
    ./venv/bin/uvicorn server:app --reload --host 0.0.0.0 --port 5001
  or
    DEV=1 ./venv/bin/python server.py          # reload on
    ./venv/bin/python server.py                # reload off
"""
import asyncio
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from pypinyin import Style, pinyin

# ---- config ---------------------------------------------------------------
LLM_BASE = os.environ.get("LLM_BASE", "http://localhost:1234/v1")
LLM_MODEL_FALLBACK = os.environ.get("LLM_MODEL", "qwen/qwen3.6-35b-a3b")
# Reasoning models (e.g. Qwen3.5-9B) otherwise emit a long hidden "thinking" phase
# before any output -> big first-token delay. Translation needs no chain-of-thought.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "none")   # translation: fast, no thinking
CHAT_REASONING = os.environ.get("CHAT_REASONING", "high")       # compose/answer: think for accuracy
TTS_BASE = os.environ.get("TTS_BASE", "http://127.0.0.1:5060")  # Kokoro sidecar (ml-venv)
HOST = os.environ.get("HOST", "0.0.0.0")   # reachable from PC/phone; set 127.0.0.1 to lock to this Mac
PORT = int(os.environ.get("PORT", "5001"))
DEV = os.environ.get("DEV", "").lower() in ("1", "true", "yes")  # DEV=1 -> uvicorn auto-reload

HERE = os.path.dirname(os.path.abspath(__file__))
BOOT_ID = str(int(time.time() * 1000))     # changes on every (re)start -> reload indicator

app = FastAPI()
# The browser extension calls /translate and /tts cross-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

SYSTEM_PROMPT = """You are an expert Chinese-English translator and interpreter.

You receive text pasted from a chat window. It may contain multiple lines and \
multiple speakers, mixing Chinese, English, emojis, and occasionally other languages.

Split the text into segments - one per message. Keep the original reading order. \
If a message has no speaker label, use null for speaker (or repeat the previous \
speaker if the line is clearly a continuation of it).

For EACH segment, output a SINGLE compact JSON object on its own line with exactly these keys:
- "speaker": the speaker label as a string, or null
- "original": the message text, verbatim, including emojis and punctuation
- "translation": a natural, fluent English translation. If the text is already English, restate it naturally.
- "notes": a SHORT note ONLY when there is an idiom, slang, internet slang, cultural \
reference, pun, or non-obvious tone worth explaining; otherwise the empty string "".

Output ONLY the JSON objects, one per line, in order. Do NOT wrap them in an array. \
Do NOT use markdown code fences. Do NOT include pinyin. Do NOT add any other commentary."""

SYSTEM_CHAT = """You are a live chat helper for a NATIVE Chinese speaker who THINKS in Chinese \
but can barely read or write it (about a 3rd-grade vocabulary). You can see the recent conversation \
as context. Assume by default that they are composing THEIR OWN message to send - unless it is \
clearly a question to you, or foreign text they want to understand.

## Fixing their message - this is REPAIR, NOT translation
They already have the sentence in mind in Chinese. They type broken pinyin, mis-dictated Chinese, or \
drop in an English word for something they can't say - and often add an English note of what they \
MEAN. Fix what they wrote so it is correct and makes sense, while KEEPING THEIR OWN WORDING AND VOICE. \
Make the SMALLEST changes that work.

- PRESERVE their voice. Do NOT rewrite into more "proper", formal, or wholesale-different Chinese - \
that loses their voice. Keep their words, word order, and style; only repair what is broken.
- Their English is INTENT, not text to translate. Use it to understand what they mean and to fix the \
right word, then slot the natural Chinese into THEIR sentence. NEVER replace the whole sentence with a \
fresh translation of their English.
- Fix: mis-dictated homophones/characters, broken pinyin (-> the character they meant), and grammar \
that breaks the meaning.
- SENSE-CHECK every time: does the repaired sentence actually make sense? Dictation is often wrong. \
If a part is garbled, fix it; if you cannot tell what they meant, ask.
- TONE: assume a casual, friendly internet-chat tone unless they say otherwise. If their wording lands \
wrong for that tone, adjust it (and note it).
- STICKY: any name/word/character they specify - especially hanzi they typed (e.g. 維尼) - is FINAL; \
reuse it verbatim and never revert or re-transliterate it. Reuse terms already used earlier in the thread.
- WORD CHOICE: the English MEANING is the source of truth; their pinyin is a weak hint and is often \
wrong. NEVER pick a word just because it matches their pinyin. Example: "chong2 as in poor" means they \
want "poor" (穷 / 可怜), NOT 宠 or 重. On conflict or ambiguity, DON'T guess - ask ONE short question \
with 2-4 hanzi candidates + glosses, e.g. poor → 穷 (broke) · 可怜 (pitiful)?
- This is live chat: be fast and minimal, don't lecture.

Reply, tight:
- The corrected Chinese (what they copy), then pinyin, then a short English gloss.
- A one-line "Changed:" note of the fixes, so they can catch mistakes. Only ask a question if something \
is genuinely ambiguous.

## If it's a question, or foreign text to understand
Answer conversationally, or translate it to English. Cite Chinese with pinyin + a short gloss. Brief \
unless asked for more depth."""

# History is trimmed to a TOKEN budget (not a fixed message count) so it scales
# with the model's loaded context length. Reserve headroom for the system prompt,
# the current input, and the response. Raise this if you raise LM Studio's context
# (e.g. ~24000 suits a 32K context; ~50000 suits 64K).
HISTORY_TOKEN_BUDGET = int(os.environ.get("HISTORY_TOKENS", "24000"))
RECENT_MAX = 40    # cap exchanges kept for replay to newly-connected devices

# ---- pinyin ---------------------------------------------------------------
HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def has_han(text: str) -> bool:
    return bool(HAN_RE.search(text or ""))


def to_pinyin(text: str) -> str:
    """Tone-marked pinyin for Chinese runs; non-Chinese text is kept inline."""
    out, idx = [], 0
    for m in HAN_RE.finditer(text):
        if m.start() > idx:
            out.append(text[idx:m.start()])
        sylls = pinyin(m.group(), style=Style.TONE)
        out.append(" ".join(s[0] for s in sylls))
        idx = m.end()
    if idx < len(text):
        out.append(text[idx:])
    return "".join(out).strip()


# ---- streaming JSON extraction -------------------------------------------
def extract_json_objects(buf: str):
    """Pull complete top-level {...} objects out of a streaming buffer.

    Returns (list_of_object_strings, remaining_buffer). Robust to newlines,
    pretty-printing, array wrappers, and stray text between objects.
    """
    objs, depth, in_str, esc, start, last_end = [], 0, False, False, None, 0
    for i, ch in enumerate(buf):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(buf[start:i + 1])
                    last_end = i + 1
                    start = None
    return objs, buf[last_end:]


# ---- LLM (async httpx) ----------------------------------------------------
_model_cache = {}


async def get_model() -> str:
    if _model_cache.get("id"):
        return _model_cache["id"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LLM_BASE}/models")
            for m in r.json().get("data", []):
                mid = m.get("id", "")
                if mid and "embed" not in mid.lower():
                    _model_cache["id"] = mid
                    return mid
    except Exception:
        pass
    return LLM_MODEL_FALLBACK


async def stream_completion(messages: list, temperature: float = 0.3, model: str = None,
                            effort: str = None):
    """Yield content deltas from the LLM as they arrive (non-blocking)."""
    eff = effort if effort is not None else REASONING_EFFORT
    payload = {
        "model": model or await get_model(),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if eff:
        payload["reasoning_effort"] = eff
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{LLM_BASE}/chat/completions", json=payload) as r:
            r.raise_for_status()
            buffer = ""
            async for chunk in r.aiter_bytes():
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content")
                    except Exception:
                        continue
                    if delta:
                        yield delta


def enrich(seg: dict) -> dict:
    original = (seg.get("original") or "").strip()
    return {
        "type": "segment",
        "speaker": seg.get("speaker"),
        "original": original,
        "pinyin": to_pinyin(original) if has_han(original) else "",
        "translation": (seg.get("translation") or "").strip(),
        "notes": (seg.get("notes") or "").strip(),
    }


_SPEAKER_RE = re.compile(r"^\s*[^\s:：]{1,24}[:：]")


def looks_like_transcript(text: str) -> bool:
    """A pasted chat log = 2+ non-empty lines with 2+ 'Name:' speaker labels.
    Everything else is treated as the user talking to the assistant (compose / ask)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    return sum(1 for ln in lines if _SPEAKER_RE.match(ln)) >= 2


def summarize_segments(segs: list) -> str:
    """Compact text form of a translation, for chat context."""
    lines = []
    for s in segs:
        prefix = f"{s['speaker']}: " if s.get("speaker") else ""
        line = prefix + s["original"]
        if s.get("translation"):
            line += f"  ->  {s['translation']}"
        lines.append(line)
    return "\n".join(lines)


# ---- shared session: registry + broadcast --------------------------------
_clients = set()
_clients_lock = asyncio.Lock()
_process_lock = asyncio.Lock()   # serialize exchanges so streams don't interleave
_history = []                    # shared LLM context (survives reconnects)
_recent = []                     # recent exchanges (list of event lists) for replay


async def broadcast(obj):
    payload = json.dumps(obj, ensure_ascii=False)
    async with _clients_lock:
        targets = list(_clients)
    dead = []
    for ws in targets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    if dead:
        async with _clients_lock:
            for ws in dead:
                _clients.discard(ws)


def _est_tokens(text: str) -> int:
    """Rough token estimate: CJK ~1.3 tok/char, other ~0.28, + per-message overhead."""
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return int(cjk * 1.3 + (len(text) - cjk) * 0.28) + 4


def _trim_history():
    """Keep the most recent turns that fit within HISTORY_TOKEN_BUDGET."""
    total, keep = 0, 0
    for msg in reversed(_history):
        total += _est_tokens(msg.get("content", ""))
        if total > HISTORY_TOKEN_BUDGET and keep > 0:
            break
        keep += 1
    if keep < len(_history):
        del _history[:len(_history) - keep]


def _remember(role, content):
    _history.append({"role": role, "content": content})
    _trim_history()


async def process(text: str, cid, model: str = None):
    async with _process_lock:
        events = []

        async def emit(obj):        # broadcast live AND store for replay
            events.append(obj)
            await broadcast(obj)

        try:
            # Infer intent from the message itself (no more ">" marker):
            # a multi-speaker paste -> translate; anything else -> assistant (compose/ask).
            is_translate = looks_like_transcript(text)
            await emit({"type": "user", "text": text,
                        "mode": "translate" if is_translate else "chat", "cid": cid})

            if not is_translate:
                # compose / repair / question — thinking ON, keeps the thread as context
                messages = ([{"role": "system", "content": SYSTEM_CHAT}]
                            + _history
                            + [{"role": "user", "content": text}])
                answer = ""
                async for delta in stream_completion(messages, temperature=0.4,
                                                     model=model, effort=CHAT_REASONING):
                    answer += delta
                    await broadcast({"type": "chat_delta", "text": delta})  # live only
                if answer:
                    _remember("user", text)
                    _remember("assistant", answer)
                    events.append({"type": "chat_full", "text": answer})  # for replay
                term = {"type": "chat_done", "cid": cid}
                events.append(term)
                await broadcast(term)
            else:
                # pasted chat log -> line-by-line translation (structured, thinking OFF, fast)
                messages = [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text}]
                buf, segs = "", []
                async for delta in stream_completion(messages, model=model):
                    buf += delta
                    objs, buf = extract_json_objects(buf)
                    for o in objs:
                        try:
                            seg = enrich(json.loads(o))
                        except Exception:
                            continue
                        segs.append(seg)
                        await emit(seg)
                objs, _ = extract_json_objects(buf)  # flush trailing object
                for o in objs:
                    try:
                        seg = enrich(json.loads(o))
                        segs.append(seg)
                        await emit(seg)
                    except Exception:
                        pass
                term = {"type": "done", "cid": cid}
                events.append(term)
                await broadcast(term)
                if segs:
                    _remember("user", "[Pasted chat log for translation]")
                    _remember("assistant", summarize_segments(segs))
        except httpx.HTTPError as e:
            err = {"type": "error", "cid": cid,
                   "message": f"Can't reach the LLM at {LLM_BASE}. "
                              f"Is LM Studio's server running? ({e})"}
            events.append(err)
            await broadcast(err)
        except Exception as e:
            err = {"type": "error", "cid": cid, "message": str(e)}
            events.append(err)
            await broadcast(err)

        _recent.append(events)
        del _recent[:-RECENT_MAX]


# ---- routes ---------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))


class TranslateReq(BaseModel):
    text: str


@app.post("/translate")
async def translate_http(req: TranslateReq):
    """Non-streaming translate for the browser extension. Returns enriched segments."""
    text = (req.text or "").strip()
    if not text:
        return {"segments": []}
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}]
    buf = ""
    async for delta in stream_completion(messages):
        buf += delta
    objs, _ = extract_json_objects(buf)
    segs = []
    for o in objs:
        try:
            segs.append(enrich(json.loads(o)))
        except Exception:
            continue
    return {"segments": segs}


@app.get("/tts")
async def tts(text: str, voice: str = "zf_xiaoxiao"):
    """Proxy to the Kokoro sidecar so the browser only talks to one origin.
    Returns 502 if the sidecar is down -> client falls back to browser TTS."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(f"{TTS_BASE}/tts",
                                 params={"text": text, "voice": voice})
        if r.status_code == 200:
            return Response(content=r.content, media_type="audio/wav")
        return Response(status_code=r.status_code)
    except Exception:
        return Response(status_code=502)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    # Send hello (boot id) + replay the recent feed BEFORE registering for live
    # broadcasts, all under the process lock, so a newly-opened/reconnected
    # device can't receive a live event before it has caught up.
    skip_replay = ws.query_params.get("replay") == "0"   # reconnecting client keeps its feed
    async with _process_lock:
        await ws.send_text(json.dumps({"type": "hello", "boot": BOOT_ID}))
        if not skip_replay:
            had_recent = bool(_recent)
            for exchange in _recent:
                for ev in exchange:
                    await ws.send_text(json.dumps(ev, ensure_ascii=False))
            if had_recent:
                await ws.send_text(json.dumps({"type": "replay_done"}))
        async with _clients_lock:
            _clients.add(ws)
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except Exception:
                continue
            if data.get("type") == "ping":            # heartbeat from the client
                await ws.send_text(json.dumps({"type": "pong"}))
                continue
            if data.get("action") == "clear":
                async with _process_lock:
                    _recent.clear()
                    _history.clear()
                await broadcast({"type": "clear"})
                continue
            text = (data.get("text") or "").strip()
            if not text:
                continue
            await process(text, data.get("cid"), data.get("model") or None)
    except WebSocketDisconnect:
        pass
    finally:
        async with _clients_lock:
            _clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    mode = "  [auto-reload]" if DEV else ""
    print(f"Translator on http://{HOST}:{PORT}  (LLM: {LLM_BASE}){mode}")
    uvicorn.run("server:app", host=HOST, port=PORT, reload=DEV)
