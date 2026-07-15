# Chinese Translator — dev tasks
#
#   make install   create venv + install deps
#   make dev       run with auto-reload      (http://localhost:5001)
#   make run       run without auto-reload
#   make stop      stop whatever is on PORT
#   make restart   stop, then dev
#   make ngrok     expose PORT via ngrok  (set NGROK_AUTH=user:pass to protect it)
#
# Override any variable, e.g.:  make dev PORT=5002   |   make run HOST=127.0.0.1

VENV := venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
MLVENV := ml-venv
HOST ?= 0.0.0.0
PORT ?= 5001
TTS_PORT ?= 5060
NGROK_AUTH ?=

.PHONY: help install install-ml dev run stop stop-tts restart tts ngrok

help:
	@echo "make install | install-ml | dev | run | stop | restart | tts | ngrok"
	@echo "  main app: HOST=$(HOST) PORT=$(PORT)   TTS sidecar: 127.0.0.1:$(TTS_PORT)"
	@echo "  run 'make tts' in one terminal and 'make dev' in another"

install:
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-ml:
	@test -d $(MLVENV) || uv venv $(MLVENV) --python 3.12
	uv pip install --python $(MLVENV)/bin/python -r requirements-ml.txt

tts:
	@test -d $(MLVENV) || $(MAKE) install-ml
	TTS_PORT=$(TTS_PORT) $(MLVENV)/bin/python tts_service.py

stop-tts:
	@lsof -ti tcp:$(TTS_PORT) | xargs kill 2>/dev/null && echo "stopped TTS port $(TTS_PORT)" || echo "nothing on port $(TTS_PORT)"

dev:
	@test -d $(VENV) || $(MAKE) install
	DEV=1 HOST=$(HOST) PORT=$(PORT) $(PY) server.py

run:
	@test -d $(VENV) || $(MAKE) install
	HOST=$(HOST) PORT=$(PORT) $(PY) server.py

stop:
	@lsof -ti tcp:$(PORT) | xargs kill 2>/dev/null && echo "stopped port $(PORT)" || echo "nothing running on port $(PORT)"

restart: stop dev

ngrok:
ifeq ($(strip $(NGROK_AUTH)),)
	@echo "⚠  No password set — this exposes the app publicly with NO auth."
	@echo "   The feed is shared, so anyone with the URL can read/inject. Protect it:"
	@echo "     make ngrok NGROK_AUTH=you:secret"
	@echo ""
	ngrok http $(PORT)
else
	ngrok http $(PORT) --basic-auth "$(NGROK_AUTH)"
endif
