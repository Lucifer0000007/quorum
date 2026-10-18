# AI usage disclosure

This project was built during the Graph Hacks event with heavy use of an AI coding assistant
(Claude Code, model Claude Opus 5.5). A human (the team) wrote the product brief, the hard rules, the
graph model and the phase plan; the assistant wrote most of the code, tests and docs under those rules and
ran them against the live FalkorDB instance. Every number in the README/UI is computed by code from the data.

The runtime product can optionally call an LLM (Anthropic API) only to (1) pick a tool and fill typed,
validated parameters and (2) format a staged notice. Verdicts never come from the LLM.

## Log

### 1. Phase 0: setup (18 Oct, ~19:25 IST)
- Assistant explored the machine and the corpus before coding: streamed the `kubernetes/enhancements`
  master tarball in memory to measure field coverage (646 of 648 kep.yaml parsed with PyYAML's default loader;
  see-also/replaces resolvability, dirty status values, duplicate kep-numbers, invalid dates). The 2 failures
  and the other numbers drove the parser design (`yaml.BaseLoader`).
- Generated: repo skeleton, docker-compose (FalkorDB pinned `v4.22.1`), Makefile, CLAUDE.md, this file,
  README skeleton, `.mcp.json`, subagent definitions, post-edit hook.

### 2. Phase 1: data
- The assistant wrote `ingest/parse.py` (pure parser), its tests on 10 real kep.yaml fixtures, `ingest/load.py`
  (UNWIND/MERGE loader) and `scripts/data_report.py`.
- It ran ingest twice to prove the counts don't change.
- It found SIG spelling variants (`sig-scheduler`, `sig-arch`, …) in the real data and added an explicit alias table.

### 3. Phase 2: query layer
- The assistant wrote the guard, the 16 read-only templates, the RO runner and the query tools.
- It probed FalkorDB 4.22 Cypher support live before writing templates: pattern comprehension,
  `properties()`, path functions, `algo.pageRank`. It also confirmed that `RO_QUERY` rejects writes.
- Two **subagents** did independent work. They were general-purpose agents told to follow
  `.claude/agents/eval-agent.md` and `.claude/agents/cypher-reviewer.md`, because Claude Code only loads
  project-defined agents at session start (the definitions are in the repo and will work from the next session).
  - The eval agent wrote the independent YAML-only gold generators (now `evals/lookups/` and `evals/multihop/`, sharing `evals/yamlcorpus.py`), the raw-text keyword baseline `evals/baseline.py`, and their tests.
  - The reviewer audited the templates against the live graph. It recomputed the blast radius for every KEP with
    a unique number using its own BFS (exact match) and reported 7 issues, all fixed:
    - duplicate stale-ancestor and successor rows
    - unknown SIGs returning zeros instead of 404
    - GitHub team aliases (e.g. `@sig-node-tech-leads`) counted as people
    - Cypher runs missing from the evidence panel
    - float scores

### 4. Phase 3: bailiff
- The assistant wrote the deterministic rules R1–R4 and the approval-gate write path.
- The rule thresholds were chosen from a pure-Python simulation over the YAML. The live verdict distribution
  over all 648 KEPs matches that simulation exactly.

### 5. Phase 4: API and UI
- The assistant wrote the FastAPI app, the keyword router and the optional LLM router.
- It loaded the `claude-api` skill first and learned that Claude Opus 5.5 rejects forced `tool_choice`, so the
  router uses `auto` with strict tools.
- It wrote the single-page UI, after loading the frontend-design skill.
- It checked the UI with a Python Playwright click-through:
  - all three verdicts
  - approve, then the prior-precedent banner
  - error states
  - 390 px mobile (fixed a horizontal overflow)

### 6. Phases 5–6: eval and ship
- The assistant wrote `evals/run_eval.py`, which runs each question through the router and the live graph.
- The eval subagent added blast-radius-count and approver questions, making 38 in total.
- The README's numbers and Cypher sections are generated from data and code by `scripts/render_readme.py`.
- It also wrote the Dockerfile and DEMO.md.

### 7. Hardening pass (same day; the session's model was switched from Opus 5.5 to Sonnet 5.5 for this part)
Prompted by the dev machine running low on memory, which stopped the local server and the WSL keepalive. The
assistant then re-checked its own earlier work and fixed what it found:
- **Test pollution:** integration tests were writing precedents into the demo's real episodic graph. Graph names
  are now overridable (`QUORUM_BRAIN_GRAPH`, `QUORUM_EPISODIC_GRAPH`), the test session and `make ui-smoke` use
  throwaway graphs, and the polluted demo graph (only `test-*` and click-through sessions) was deleted after
  inspecting it.
- **LLM path:** the model name was read before `.env` was loaded. Now lazy. Added tests that run the real
  Anthropic SDK against a local stub server (request shape, refusal, API error, invalid parameters, invented
  entities). It has still never talked to the real API.
- **Bailiff:** crash if an active KEP had no owning SIG; duplicate queries in the evidence panel.
- **Single source of truth** for the "recent" window (it was defined in two places).
- **PageRank** (`algo.pageRank`) is now reachable from the Ask box and UI, not only a bare endpoint.
- **Local security:** FalkorDB's image runs Redis with `--protected-mode no`; compose now publishes ports on
  127.0.0.1 only and snapshots 15 s after a write so an approved precedent survives an abrupt stop.
- **Reproducibility:** `scripts/dev_up.ps1` (one-command Windows start), `scripts/ui_smoke.py` (17 browser
  assertions), a clean-clone verification of the README setup, README prose that hardcoded counts now defers to
  generated numbers.
- **A correction to the assistant's own earlier report:** it said the graph had 5,526 edges; the real number is
  5,726 (`Meta.edge_count`, and the sum of the per-type counts).

### 8. The five-item work order (same Sonnet 5.5 session)
The user pasted a five-item work order and an OpenRouter key (they said they would rotate it), and asked for
whichever LLM-path spec fits the project best. The assistant chose the provider-agnostic one (an OpenAI-compatible
adapter) because it works with the key at hand and with Gemini's compatible endpoint and does not tie a submission to one
vendor, and it folded in the other spec's extras (UI mode, Keywords only on no key or a smoke failure, the `.env` and
git-history checks). The key lives only in the git-ignored `.env`; it was never printed, committed or put in a command.
- **LLM path:** `agent/llm.py` replaced the Anthropic-specific code (env-only configuration, tool calling, bounded 429
  retry, loud fallback to Keywords only when a model lacks tool calling or the key is rejected, key scrubbing),
  `scripts/llm_smoke.py` runs 13 questions against the real API, and the app uses the LLM only after that passed for the
  configured provider and model. Verified once against OpenRouter, free tier, `nvidia/nemotron-3-super-120b-a12b:free`:
  13 of 13 passed. A free-tier model had to be used because the key has no credits; the first model tried
  (`google/gemma-4-31b-it:free`) was rate limited upstream and was reported as such.
- **Eval:** two suites (multi-hop, lookups) with gold from YAML only, a raw-text keyword-search baseline, and results
  per question for both systems. A subagent wrote the gold generators and the baseline.
- **Verdicts:** a second subagent added per-reason evidence (rule, query id, matched ids, exact Cypher) and the verdict
  report (first-match and all-match rule counts, hop sensitivity). The assistant wrote the README and UI parts.
- **Status and fresh clone:** `docs/STATUS.md` is generated from the live system. The README setup was verified by
  following only the README in a fresh clone on Debian 13 under WSL2 (Docker 26, Compose 2.26, Python 3.13): same data
  hash as the working copy, 207 tests, the eval, and the browser smoke check.
- A peer Claude Desktop session wrote `DEMO-2MIN.md` in the same repository at the same time. It is the user's file
  and was left untouched.

### Assumptions (stated instead of asking)
- **Docker:** not installed on the dev Windows machine (no admin), so FalkorDB runs in Docker Engine
  inside WSL2 Debian. `docker-compose.yml` is the standard one; `local.mk` (gitignored) points `DOCKER`
  at WSL.
- **FalkorDB version:** pinned `falkordb/falkordb:v4.22.1` (C engine). The `latest` tag now points to
  the new 6.x Rust engine, which was released three weeks ago; we chose the mature line.
- **Parsing:** `yaml.BaseLoader` (all scalars as strings), because 2 real kep.yaml files contain
  impossible dates that crash PyYAML's timestamp parser, and unquoted milestones like `1.20` would
  become floats.
- **Status normalization:** an explicit table (raw value kept in `status_raw`). For example,
  `imlpemented` → implemented and `superseded` → replaced.
- **KEP identity:** `id` = kep-number when valid and unique. Duplicates and `0` get `<sig>/<folder>`.
- **superseded-by:** modeled as `(successor)-[:REPLACES]->(this)`.
- **"Recently active":** the KEP's latest-milestone minor ≥ (max minor in corpus − 3). The max is
  computed at ingest.
- **prr-approvers:** empty in every kep.yaml (PRR data lives in `keps/prod-readiness/`). It is
  reported as 0 and not ingested.
- **Deploy:** none. We ship complete local setup instructions instead (team decision).
