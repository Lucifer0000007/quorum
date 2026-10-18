# Quorum status

This file was generated on 2026-10-18 17:11 UTC by `python -m scripts.render_status` (`make status`). Every figure below was read from the live system when it was generated, and each is followed by the command that reproduces it. Nothing here is typed by hand.

## What `scripts/dev_up.ps1` does

It is an optional helper for a Windows machine where Docker runs inside WSL2, which is how this project was built. On Linux or macOS you do not need it. Step by step, it does the following. First, it finds the repository root and refuses to continue if `.venv` does not exist. Second, it checks whether a hidden `wsl.exe -d Debian -u root -- sleep infinity` process is already running, and starts one if not, because WSL stops idle distributions and would take Docker and FalkorDB down with it. Third, it converts the repository path with `wslpath` and runs `docker compose up -d falkordb` inside WSL. Fourth, it runs `scripts/wait_falkordb.py`, which waits up to a minute for FalkorDB to answer PING. Fifth, it runs `scripts.check_graph` quietly; if the graph is missing or not ready it fetches the pinned corpus commit from `CORPUS_COMMIT` when `data/enhancements` is absent, and runs `ingest.load`. Sixth, it prints the last line of `scripts.check_graph`. Finally, unless `-NoServer` was passed, it serves the app with uvicorn on 127.0.0.1:8000 in the foreground, so the window must stay open.

## Phase 0 facts, as they stand now

FalkorDB answers PING: `PING localhost:6379 -> PONG; modules=['graph@42201', 'vectorset@1']` (reproduce with `python scripts/wait_falkordb.py`). The test suite reports `207 passed in 85.60s (0:01:25)` (reproduce with `python -m pytest -o addopts= -q`). Ruff reports `All checks passed!` (`python -m ruff check .`), and the Cypher lint reports `OK: 16 templates are read-only and LIMIT-capped; no write paths outside episodic.py` (`python -m scripts.lint_cypher`). At the end of Phase 0 the same checks gave a PONG with the graph module loaded, one passing smoke test and a clean ruff run; the suite has grown since.

## The live graph

The graph `company_brain` is ready (`Meta.ready` is true) and was built from kubernetes/enhancements commit `5b503a75369bdfdd494a2c2c72107a41bf6dd1a8` with data hash `32731b67d488d88eab9806365fa0dc0ea80ae23869e75cee0445f8492dd6a54c`. It holds 648 KEPs, 693 people and 32 SIGs, and 5726 relationships (`Meta.edge_count` says 5726): 1203 APPROVED, 1100 AUTHORED, 648 OWNED_BY, 615 PARTICIPATES, 9 REPLACES, 1837 REVIEWED, 314 SEE_ALSO. All of these come from `python -m scripts.check_graph`, which prints one `node:` or `edge:` line per label and relationship type and the data hash on its last line. A fresh clone that follows the README reaches the same hash, because the corpus commit is pinned.

Running the bailiff over all 648 KEPs gives 339 CLEAN, 187 AT_RISK and 122 HUMAN_GATE_REQUIRED (`python -m scripts.verdict_report`, which writes `data/verdict_report.json`). These thresholds were tuned on this corpus and are not validated against ground truth.

On the multi-hop questions the query layer answered 29 of 29 correctly and the one-pass keyword search 9; on the lookup baseline they answered 33 and 32 of 33 (`make eval`, which writes `evals/results.json`).

## The language-model path, and its caveat

The optional LLM path uses the OpenAI-compatible chat-completions API with tool calling. The provider tested is **OpenRouter** and the model is `nvidia/nemotron-3-super-120b-a12b:free`. The smoke test (`python -m scripts.llm_smoke`) was run against the real API on 2026-10-18T16:41:15+00:00 and gave 13 passed, 0 failed and 0 rate limited out of 13 questions; the evidence is in `evals/llm_smoke.json`.

The caveat is this. That run was a single run against a single free-tier model, using an OpenRouter key that is to be rotated, so it shows that this model chose the right tool and parameters for those questions on that day and nothing more. It does not show that other models or other phrasings work, and free-tier models can be rate limited. Quorum therefore uses the LLM only when all three variables are set and the smoke test has passed for exactly that provider and model; in every other case, including a fresh clone without a key, it runs in Keywords only mode and the UI header says so and why. The LLM never sets a verdict and never writes Cypher, and the test suite cannot see the real key.

## MCP servers and subagents that were configured but never loaded

`.mcp.json` configures three MCP servers: the FalkorDB server `@falkordb/mcpserver@1.3.0`, Context7 `@upstash/context7-mcp@4.4.2` and Playwright `@playwright/mcp@0.0.83`. Their package names and versions were checked against the npm registry, but Claude Code only loads project MCP servers at session start, after approval, and the session that built this project began before the file existed, so none of them was ever loaded or called. In their place the work used the FalkorDB Python client directly, Python Playwright for browser checks (`scripts/ui_smoke.py`), and ordinary documentation lookups.

The three subagent definitions in `.claude/agents` (`parser-agent`, `cypher-reviewer`, `eval-agent`) were likewise not loadable in that session, which reported the agent types as not found. Generic subagents were instead told to follow the definitions of `cypher-reviewer` and `eval-agent`; `parser-agent` was never used, because the parser and its tests were written by the main assistant. The definitions will load in a new session.

## Reproduce every number

| What | Command |
|---|---|
| PING | `python scripts/wait_falkordb.py` |
| Node and relationship counts, data hash | `python -m scripts.check_graph` |
| Verdict counts, rules, hop sensitivity | `python -m scripts.verdict_report` |
| Eval results (both suites, both systems) | `make eval` |
| LLM smoke test | `python -m scripts.llm_smoke` |
| Tests | `python -m pytest -o addopts= -q` |
| Lint | `python -m ruff check .` and `python -m scripts.lint_cypher` |
| README figures | `make readme` |
| This file | `make status` |
