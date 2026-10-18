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
