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

.PHONY: venv up down ping clone-data report ingest verdicts readme test test-fast ui-smoke lint run eval check

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

# Browser click-through on a throwaway server and graph. One-time: pip install -r requirements-dev.txt && playwright install chromium
ui-smoke:
	$(PY) -m scripts.ui_smoke

# Verdict distribution over every KEP (data/verdicts.json) and regeneration of the README's numbers and Cypher.
verdicts:
	$(PY) -m scripts.verdict_distribution

readme:
	$(PY) -m scripts.render_readme

lint:
	$(PY) -m ruff check .
	$(PY) -m scripts.lint_cypher

run:
	$(PY) -m uvicorn agent.api:app --host 127.0.0.1 --port 8000

eval:
	$(PY) -m evals.make_gold
	$(PY) -m evals.run_eval

check:
	$(PY) -m scripts.check_graph
	$(PY) -m scripts.lint_cypher
