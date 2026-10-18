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
  master tarball in memory to measure field coverage (646 kep.yaml; see-also/replaces resolvability,
  dirty status values, duplicate kep-numbers, invalid dates). These numbers drove the parser design.
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
- An **eval-agent subagent** wrote `evals/make_gold.py`, an independent YAML-only gold generator, and its tests.
- A **cypher-reviewer subagent** audited the templates against the live graph. It recomputed the blast radius
  for all 647 numbered KEPs with its own BFS (exact match) and reported 7 issues, all fixed:
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
- The eval-agent added blast-radius-count and approver questions, making 38 in total.
- The README's numbers and Cypher sections are generated from data and code by `scripts/render_readme.py`.
- It also wrote the Dockerfile and DEMO.md.

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
