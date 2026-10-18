# Quorum task runner. Works on Linux/macOS and on Windows (Git Bash + GNU make).
ifeq ($(OS),Windows_NT)
PY ?= .venv/Scripts/python.exe
else
PY ?= .venv/bin/python
endif
PYTHON ?= python3
DOCKER ?= docker
# Machine-local overrides (gitignored), e.g. DOCKER := wsl -d Debian -u root -- docker
-include local.mk

.PHONY: venv up down ping clone-data report ingest verdicts readme status test test-fast dev-deps ui-smoke lint run eval check

# On Windows the `python3` name is often a Store stub: make venv PYTHON=python
venv:
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -q -r requirements.txt

up:
	$(DOCKER) compose up -d falkordb
	$(PY) scripts/wait_falkordb.py

down:
	$(DOCKER) compose down

ping:
	$(PY) scripts/wait_falkordb.py

# The corpus is pinned (CORPUS_COMMIT) so everyone gets the same data, hash, README numbers and test expectations.
# For the latest upstream instead: make clone-data CORPUS_COMMIT=master   (tests that name specific KEPs may then differ)
CORPUS_URL ?= https://github.com/kubernetes/enhancements
CORPUS_COMMIT ?= $(shell cat CORPUS_COMMIT)

clone-data:
	@if [ -d data/enhancements/.git ]; then echo "data/enhancements already cloned"; else \
	git init -q data/enhancements && git -C data/enhancements config core.longpaths true && \
	git -C data/enhancements remote add origin $(CORPUS_URL) && \
	git -C data/enhancements fetch -q --depth 1 origin $(CORPUS_COMMIT) && \
	git -C data/enhancements checkout -q FETCH_HEAD; fi
	git -C data/enhancements rev-parse HEAD

report:
	$(PY) -m scripts.data_report

ingest:
	$(PY) -m ingest.load

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not integration" -x

# One-time setup for ui-smoke: Playwright plus Chromium (--with-deps also installs system libraries on Linux: needs root)
dev-deps:
	$(PY) -m pip install -q -r requirements-dev.txt
	$(PY) -m playwright install --with-deps chromium

# Browser click-through on a throwaway server and graph (run `make dev-deps` once first)
ui-smoke:
	$(PY) -m scripts.ui_smoke

# Verdict distribution over every KEP (data/verdicts.json) and regeneration of the README's numbers and Cypher.
verdicts:
	$(PY) -m scripts.verdict_report

readme:
	$(PY) -m scripts.render_readme

# docs/STATUS.md: facts from the live system, each with the command that reproduces it (runs the whole test suite)
status:
	$(PY) -m scripts.render_status

lint:
	$(PY) -m ruff check .
	$(PY) -m scripts.lint_cypher

run:
	$(PY) -m uvicorn agent.api:app --host 127.0.0.1 --port 8000

eval:
	$(PY) -m evals.lookups.make_gold
	$(PY) -m evals.multihop.make_gold
	$(PY) -m evals.run_eval

check:
	$(PY) -m scripts.check_graph
	$(PY) -m scripts.lint_cypher
