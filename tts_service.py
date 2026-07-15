"""
ML sidecar: local neural TTS (Kokoro) as a small HTTP service.

Runs on the Python 3.12 venv (ml-venv) because the ML stack won't build on 3.14.
The main app (server.py, 3.14) proxies /tts here; this stays localhost-only.

Run:
    ./ml-venv/bin/uvicorn tts_service:app --host 127.0.0.1 --port 5060
  or
    ./ml-venv/bin/python tts_service.py
"""
import io
import os

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Response
from starlette.concurrency import run_in_threadpool

VOICE_DEFAULT = os.environ.get("TTS_VOICE", "zf_xiaoxiao")
HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "5060"))
SR = 24000  # Kokoro output sample rate

app = FastAPI()

# Loaded once at import (startup). First ever run downloads ~330MB from HF.
from kokoro import KPipeline  # noqa: E402
_pipe = KPipeline(lang_code="z", repo_id="hexgrad/Kokoro-82M")


def _synth(text: str, voice: str) -> bytes | None:
    chunks = [a for _, _, a in _pipe(text, voice=voice)]
    if not chunks:
        return None
    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="WAV")
    return buf.getvalue()


@app.get("/health")
async def health():
    return {"ok": True, "voice": VOICE_DEFAULT}


@app.get("/tts")
async def tts(text: str, voice: str = VOICE_DEFAULT):
    text = (text or "").strip()
    if not text:
        return Response(status_code=204)
    data = await run_in_threadpool(_synth, text, voice)
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    print(f"TTS sidecar on http://{HOST}:{PORT}  (voice: {VOICE_DEFAULT})")
    uvicorn.run(app, host=HOST, port=PORT)
