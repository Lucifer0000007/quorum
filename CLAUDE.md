# Quorum: project instructions

Quorum is a pre-flight check for engineering decisions, built for Graph Hacks (FalkorDB, Track 03: Company Brain).
The "company" is the public Kubernetes project: SIGs = teams, KEPs = decisions, GitHub issues = tickets,
people = authors/reviewers/approvers. A user can (a) ask institutional-memory questions and (b) pick a KEP and
"simulate superseding it". A deterministic graph rule returns a verdict with the blast radius (affected KEPs, SIGs,
people). The tool STAGES an impact notice behind a human approval gate and, on approval, writes a precedent to a
second graph. Every answer shows the graph path, the Cypher run, and source links.

## Hackathon rules
- FalkorDB is the primary DB and central. If the product works without FalkorDB, we fail.
- Only public data: github.com/kubernetes/enhancements (`keps/*/*/kep.yaml`).
- AI coding use is disclosed in AI_USAGE.md (keep it updated).

## Hard rules
- NEVER post anything to GitHub or any external service. The gate only displays staged text.
- All ingest writes use idempotent MERGE. Re-running ingest leaves the graph unchanged.
- Agent reads go through GRAPH.RO_QUERY with timeout <= 3000 ms and LIMIT <= 100, using parameterized query
  templates only (`agent/queries.py`, checked by `agent/guard.py`). The LLM never writes raw Cypher.
- Verdicts are computed by deterministic code/Cypher (`agent/bailiff.py`), NEVER by the LLM. The LLM may only
  (1) choose a tool and fill typed parameters validated by code, and (2) format staged notices.
- Never fake data or silently fall back. Fail loudly with a clear error. Missing LLM key: the UI says so and the
  deterministic tools still work.
- "No path found" is a real result and must be reported as such.
- Report only numbers computed from the data. Never hardcode counts or an eval score.
- "Recently active" is defined only from kep.yaml fields (latest-milestone, current reviewers/approvers).

## Graph model
Graph `company_brain` (read-only for the agent):
- Nodes: `KEP{id,path,title,status,status_raw,stage,latest_milestone,latest_minor,source_url,issue_url,creation_date,unresolved_refs}`,
  `Person{handle}`, `SIG{name}`, `Meta{key,data_hash,source_commit,max_minor,ready,...}`
- Edges: `(Person)-[:AUTHORED|REVIEWED|APPROVED]->(KEP)`, `(KEP)-[:OWNED_BY]->(SIG)`,
  `(KEP)-[:PARTICIPATES]->(SIG)` (read: the SIG participates in the KEP), `(KEP)-[:REPLACES]->(KEP)`, `(KEP)-[:SEE_ALSO]->(KEP)`
- `Meta.ready=true` only after a full successful load; the app refuses to run otherwise.

Graph `agent_episodic_memory` (written only by the approval gate):
- `Session{id}`, `Precedent{id,verdict,kep_id,created_at,staged_notice}`, `KEPRef{id}`
- `(Session)-[:CHECKED]->(Precedent)`, `(Precedent)-[:ABOUT]->(KEPRef)`

## Layout & commands
- `ingest/` parse (pure) + load (MERGE). `agent/` guard, queries, bailiff, router, api. `ui/` static SPA.
- `make up | clone-data | ingest | test | test-fast | lint | run | eval | check`
- Local machine: FalkorDB runs in Docker inside WSL Debian (`local.mk` sets `DOCKER`).
- Python venv: `.venv` (Python 3.12). Tests marked `integration` need the live graph.
