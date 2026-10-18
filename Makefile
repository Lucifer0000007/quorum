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

.PHONY: venv up down ping clone-data report ingest test test-fast lint run eval check

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

clone-data:
	@if [ -d data/enhancements/.git ]; then echo "data/enhancements already cloned"; \
	else git clone --depth 1 -c core.longpaths=true https://github.com/kubernetes/enhancements data/enhancements; fi
	git -C data/enhancements rev-parse HEAD

report:
	$(PY) -m scripts.data_report

ingest:
	$(PY) -m ingest.load

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest -m "not integration" -x

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
