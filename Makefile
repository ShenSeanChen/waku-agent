# waku-agent — one command per pillar.
#
# Make is not a framework — it's a 45-year-old command shortcut tool that
# ships with every Mac/Linux. Each target below is just the shell command
# you'd otherwise type. `make run` = "run the python below", nothing more.
#
# PY picks the project venv automatically so you never need to remember
# `source .venv/bin/activate` — both work, this is just fewer steps.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python)

.PHONY: run voice telegram brief dashboard trace eval eval-judge gate lint

run:            ## chat with Waku in the terminal
	$(PY) -m waku

voice:          ## talk to it — push-to-talk, or always-on with WAKU_WAKE_WORD
	$(PY) -m waku voice

telegram:       ## phone → laptop (needs TELEGRAM_BOT_TOKEN in .env)
	$(PY) -m waku telegram

brief:          ## morning briefing from calendar + mail + memory
	$(PY) -m waku brief

# The server holds dashboard.py in memory: static JS/CSS reload on refresh, but
# Python routes do NOT. After pulling a change that touches dashboard.py (or any
# imported module), stop this and re-run it, or the UI shows stale backend data.
dashboard:      ## everything on one page — http://localhost:7777 (restart after a backend pull)
	$(PY) -m waku.ops.dashboard

trace:          ## deep trace waterfalls (Phoenix) at http://localhost:6006
	$(PY) -m phoenix.server.main serve

eval:           ## deterministic evals (0/1, no judge involved)
	$(PY) -m pytest -q evals/deterministic

eval-judge:     ## LLM-as-judge evals (scored %, needs an API key)
	$(PY) -m pytest -q evals/judge

gate:           ## the release gate: deterministic must pass, judge must clear threshold
	$(PY) -m waku.ops.release_gate

shootout:       ## same tasks, different brains: make shootout RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8"
	$(PY) scripts/shootout.py $(RUNS)

shootout-coding: ## coding round via pi, scored by tests: make shootout-coding RUNS="kimi:kimi-k3 anthropic:claude-opus-4-8"
	$(PY) scripts/shootout.py $(RUNS) --coding

lint:
	$(PY) -m ruff check waku evals
