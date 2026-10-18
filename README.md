# Quorum

**Graph Hacks · FalkorDB · Track 03: Company Brain**

Quorum is a pre-flight check for engineering decisions. Before you replace an old decision, it tells you
what else is linked to it (reachable via see-also and replaces), which teams own those decisions, and who actually
knows the area. Every answer shows the graph path, the exact Cypher that ran, and a link to the source.

The "company" is the public Kubernetes project:

| Company concept | Kubernetes |
|---|---|
| Teams | SIGs (sig-node, sig-scheduling, …) |
| Decisions | KEPs, Kubernetes Enhancement Proposals (`keps/*/*/kep.yaml`) |
| Tickets | the KEP tracking issues (`kubernetes/enhancements/issues/<n>`) |
| People | KEP authors, reviewers and approvers |

## The problem

Large engineering organizations write decisions down, then lose track of how they connect. When someone
proposes to supersede a design, the questions that matter are graph questions: which other active decisions
link to this one, how many teams own those, does it point at something already dead, and who reviewed this
area recently. Keyword search over documents can't answer them reliably, and an LLM guessing at them is
worse than no answer.

## What it does

1. **Ask the company brain.** "Who knows about sig-scheduling?", "Does KEP-2377 have a successor?", "Which KEPs
   list KEP-1287 in see-also?", "Why does KEP-4381 exist?", "Which KEPs are the most central?" (PageRank). Each
   question routes to one typed tool backed by a read-only Cypher template.
2. **Simulate superseding a KEP.** The bailiff, a deterministic rule set, walks the graph and returns
   `CLEAN`, `AT_RISK` or `HUMAN_GATE_REQUIRED`. It also returns the blast radius (affected KEPs, SIGs and the path to
   each), stale references, suggested reviewers and source links. "No path found" is reported as a real result.
3. **Approve behind a human gate.** Quorum stages an impact notice and never sends it. When a human clicks
   "Approve and record precedent", Quorum writes a `Precedent` into a second graph, `agent_episodic_memory`. The next check of
   the same KEP shows a "checked before" banner from that graph.

## Results

Does the graph answer questions that plain text search over the same files can't? The multi-hop numbers below are the
test of that; the lookups after them are a baseline. Everything in this section is generated from
`evals/results.json` by `make readme`.

<!-- EVAL:START -->

Every question below was answered by two systems and scored against gold answers computed from the raw YAML alone (no FalkorDB, no `ingest/`). Run 2026-10-18T17:07:31+00:00 on data `32731b67d488`, corpus commit `5b503a75369b`; reproduce with `make eval`.

- **Query layer**: Quorum, i.e. typed queries over the FalkorDB graph.
- **Keyword search**: a one-pass keyword search over the raw kep.yaml text (`evals/baseline.py`). It is a plain text baseline, not a strong retrieval system: it never goes past the first hop and applies none of the cleaning rules (SIG aliases, status normalization, team aliases). A stronger baseline, such as iterated searches or a model reading the files, could close part of the gap.

### Multi-hop questions

These need paths: everything reachable via see-also/replaces within 2 or 3 hops, the active KEPs reachable from a withdrawn, rejected or replaced KEP, and the SIGs that own them. "Reachable via see-also/replaces" means exactly that; a link is not a claim that one KEP depends on another.

| Question kind | Questions | Query layer correct | Keyword search correct | Tie (both correct) | Query layer only | Keyword search only | Both wrong |
|---|---|---|---|---|---|---|---|
| `hop2_set` | 8 | 8 | 2 | 2 | 6 | 0 | 0 |
| `reachable_count_3hop` | 5 | 5 | 0 | 0 | 5 | 0 | 0 |
| `reachable_sigs` | 8 | 8 | 4 | 4 | 4 | 0 | 0 |
| `stale_reach` | 8 | 8 | 3 | 3 | 5 | 0 | 0 |
| **All** | 29 | 29 | 9 | 9 | 20 | 0 | 0 |

### Lookups (a baseline)

Single-fact questions (owner, status, successor, referrers, counts, approvers). A keyword search can answer most of these, so they check that the graph agrees with the files rather than show an advantage.

| Question kind | Questions | Query layer correct | Keyword search correct | Tie (both correct) | Query layer only | Keyword search only | Both wrong |
|---|---|---|---|---|---|---|---|
| `approvers` | 5 | 5 | 5 | 5 | 0 | 0 | 0 |
| `has_successor` | 8 | 8 | 8 | 8 | 0 | 0 | 0 |
| `owning_sig` | 5 | 5 | 5 | 5 | 0 | 0 | 0 |
| `see_also_referrers` | 5 | 5 | 4 | 4 | 1 | 0 | 0 |
| `sig_kep_count` | 5 | 5 | 5 | 5 | 0 | 0 | 0 |
| `status` | 5 | 5 | 5 | 5 | 0 | 0 | 0 |
| **All** | 33 | 33 | 32 | 32 | 1 | 0 | 0 |

Query-layer failures: none. Questions only the keyword search got right: none. Per-question answers, gold and errors for both systems are in `evals/results.json`.

<!-- EVAL:END -->

## Architecture

```
 kubernetes/enhancements (public)      ingest/ (pure Python, no LLM)            FalkorDB (Docker, v4.22.1)
 keps/*/*/kep.yaml  ──clone──▶  parse.py ──rows──▶ load.py ──UNWIND…MERGE──▶  graph "company_brain"  (Meta.ready)
                                                                                        │ GRAPH.RO_QUERY, 3 s timeout
 Browser UI (ui/) ──HTTP──▶ FastAPI (agent/api.py)                                      │
   ask box, KEP picker        │ router.py: keyword rules, or optional LLM tool choice   │
   verdict card               │   └─ pydantic-validated typed params ───────────────────┤
   blast radius + paths       │ queries.py / templates.py (16 guarded templates) ◀─────┘
   Cypher evidence panel      │ bailiff.py: deterministic verdict rules R1–R4
   staged notice + Approve ───┴▶ episodic.py: MERGE Precedent ──▶ graph "agent_episodic_memory"
```

FalkorDB does the core work. Every answer is a Cypher traversal: variable-length paths over
`REPLACES|SEE_ALSO` in both directions, per-KEP shortest paths, recency-weighted aggregation over people, and
`algo.pageRank` for KEP centrality. Without FalkorDB there is no answer, no verdict and no precedent. The app
refuses to serve anything until `Meta.ready = true`.

## Graph data model

```
Graph "company_brain" (read-only for the agent)

  (:Person {handle})
      │  AUTHORED / REVIEWED / APPROVED
      ▼
  (:KEP {id, number, path, title, status, status_raw, stage, latest_milestone, latest_minor,
         creation_date, owning_sig, source_url, issue_url, unresolved_refs})
      │  OWNED_BY ─────────────▶ (:SIG {name})
      │  PARTICIPATES ─────────▶ (:SIG)            read as "the SIG participates in the KEP"
      │  SEE_ALSO ─────────────▶ (:KEP)            from kep.yaml see-also
      │  REPLACES ─────────────▶ (:KEP)            from replaces, and reversed superseded-by
  (:Meta {key:'brain', data_hash, source_commit, max_minor, kep_count, person_count, sig_count,
          edge_count, ready})

Graph "agent_episodic_memory" (written only by the approval gate)

  (:Session {id}) ─CHECKED▶ (:Precedent {id, verdict, kep_id, action, created_at, data_hash,
                                         staged_notice}) ─ABOUT▶ (:KEPRef {id})
```

Indexes: `KEP.id`, `Person.handle`, `SIG.name`, `Meta.key`.

How the data is cleaned:
- **KEP identity:** a KEP's id is its kep-number when that number is valid and unique. If the number is missing, zero or
  shared by two folders (kep-number 2133 is used by two KEPs), the id is `<sig>/<folder>`. Looking up an ambiguous number fails loudly and lists the
  candidates.
- **Statuses:** normalized with an explicit table. For example, `imlpemented` → implemented and `superseded` →
  replaced. The raw value is kept in `status_raw`.
- **SIG names:** spelling variants (`sig-scheduler`, `sig-arch`, …) are merged so the "2+ SIGs" rule doesn't double-count.
- **References:** references that don't resolve to a KEP (design docs, legacy `.md` paths, "TODO") are kept as
  `KEP.unresolved_refs` and counted. They are never guessed.
- **Handles:** "@" is stripped and handles are lowercased. TBD and empty entries are skipped, and so are GitHub team aliases like
  `@sig-node-tech-leads`, because those are not people.

### The data, in numbers

<!-- STATS:START -->

_Generated by `python -m scripts.render_readme` from `data/report.json`, `data/verdict_report.json` and `evals/results.json`; not typed by hand._

- Corpus: kubernetes/enhancements @ `5b503a75369b`: **648** kep.yaml files, **0** parse failures, data hash `32731b67d488`.
- Graph: 648 KEPs, 693 people, 32 SIGs; edges AUTHORED 1100, REVIEWED 1837, APPROVED 1203, OWNED_BY 648, PARTICIPATES 615, SEE_ALSO 314, REPLACES 9.
- see-also references: 314 resolved to a KEP, 141 unresolvable (kept on `KEP.unresolved_refs`), 100 placeholders (n/a, TBD). replaces: 8 resolved / 24 unresolvable. superseded-by: 1 resolved.
- Cleaning: 5 status values normalized, 59 handles skipped (TBD, empty, team aliases), 5 KEPs keyed by folder because their number is missing or duplicated; prr-approvers non-empty in 0 files.
- "Recent" threshold: latest-milestone >= v1.35 (corpus max minus 3).
- Verdicts over all 648 KEPs (live graph): AT_RISK 187, CLEAN 339, HUMAN_GATE_REQUIRED 122 (rule breakdown in the verdict section).
- Cross-check: a pure-Python re-implementation over the raw YAML predicts AT_RISK 187, CLEAN 339, HUMAN_GATE_REQUIRED 122 (identical).
- Threshold sensitivity: requiring 3 SIGs instead of 2 would gate 62 KEPs instead of 122.
- Eval run 2026-10-18T17:07:31+00:00 on data `32731b67d488`: results in the section at the top.

<!-- STATS:END -->

## Verdict rules (the bailiff)

`agent/bailiff.py` evaluates these rules over graph facts. No LLM is involved.

| Rule | Effect | Fires when |
|---|---|---|
| R1 | `HUMAN_GATE_REQUIRED` | active KEPs (provisional, implementable or implemented) within 3 `REPLACES`/`SEE_ALSO` hops, in either direction, are owned by **2 or more** SIGs |
| R2 | `AT_RISK` | active KEPs within 3 hops exist, all owned by one SIG |
| R3 | `AT_RISK` | the target references, via outgoing `SEE_ALSO` within 3 hops, a stale KEP (replaced, rejected, withdrawn or deferred). A see-also link says "references", not "depends on" |
| R4 | `AT_RISK` | none of the target's approvers is recently active, or no approvers are listed |
| none | `CLEAN` | none of the above: "no path found" to any active KEP within 3 hops |

**"Recently active"** uses only kep.yaml fields. A person is recently active if they are listed as author,
reviewer or approver on any KEP whose `latest-milestone` is within the 4 newest minors in the corpus. The newest minor is
computed at ingest, never hardcoded. We have no activity history beyond that, so we don't claim any.

**Thresholds, and what they are not.** The 2-SIG gate and the 3-hop radius were tuned on this corpus: we looked at how
the verdicts split on real KEPs (`scripts/data_report.py`, `scripts/verdict_report.py`) and picked values at which all
three verdicts occur. They are **not validated against ground truth**: there is no labelled set of supersessions that
did or didn't need a human, so we cannot say how often a verdict agrees with what the Kubernetes maintainers would
decide. A verdict describes what is reachable via see-also/replaces and who owns it, nothing more. Every reason in
the UI shows the rule that fired, the query it was decided on, the ids it matched and the exact Cypher.

<!-- VERDICTS:START -->

`python -m scripts.verdict_report` runs the bailiff over all 648 KEPs of the live graph (data `32731b67d488`, gate at 2+ SIGs, 3 hops):

| Verdict | KEPs |
|---|---|
| `CLEAN` | 339 |
| `AT_RISK` | 187 |
| `HUMAN_GATE_REQUIRED` | 122 |

Rules fired. *All-match* counts every rule that fired on a KEP, so it can add up to more than 648; *first-match* gives each KEP to the first rule that fired in the order R1, R2, R3, R4 (CLEAN if none), so it adds up to 648.

| Rule | Name | All-match | First-match |
|---|---|---|---|
| R1 | multi-SIG reach | 122 | 122 |
| R2 | single-SIG reach | 147 | 147 |
| R3 | stale reference | 32 | 0 |
| R4 | no recently active approver | 63 | 40 |
| CLEAN | no path found | 339 | 339 |

Sensitivity to the hop threshold (the same rules, with reach limited to 1, 2 or 3 hops):

| Hops | CLEAN | AT_RISK | HUMAN_GATE_REQUIRED | KEPs with any KEP in reach |
|---|---|---|---|---|
| 1 | 341 | 261 | 46 | 272 |
| 2 | 339 | 221 | 88 | 272 |
| 3 | 339 | 187 | 122 | 272 |

Why the split looks like this, from the numbers above:
- 376 of 648 KEPs have no REPLACES/SEE_ALSO path to any KEP within 3 hops, so nothing is reachable from them; such a KEP is CLEAN unless R4 fires on it (no recently active approver). CLEAN is 339.
- Links concentrate on a few hub KEPs: only 272 KEPs reach anything, and the largest reach from one KEP is 52 KEPs.
- The gate is sensitive to the hop threshold: 46 KEPs reach 2+ SIGs through direct links alone, 122 within 3 hops. We kept 3 because it is the radius the blast-radius query reports; that is a choice, not a finding.
- R3 is the first rule to fire on 0 KEPs: every KEP that references a stale KEP already has an active KEP in reach, so R1 or R2 fires before it. It still shows up as extra evidence on 32 verdicts.
- 703 of 703 reasons carry the rule name, the query id, the matched ids and the exact Cypher.

<!-- VERDICTS:END -->

**Suggested reviewers** come from `who_knows`, using this formula:
`score(person) = Σ over (role, KEP) pairs in scope of w(KEP)`, where `w = 2` if the KEP's latest-milestone is
recent and `1` otherwise. Scope is the KEP plus its 2-hop neighborhood, or a SIG's owned and participating KEPs.

**Real examples** (the tests run each of these against the live graph):

| KEP | Verdict | Why |
|---|---|---|
| KEP-1020 | `CLEAN` | no links at all |
| KEP-624 | `AT_RISK` | R2: active neighbours, all in sig-scheduling |
| KEP-3327 | `AT_RISK` | R2 + R4: its only approver is the team alias `@sig-node-tech-leads` |
| KEP-5468 | `AT_RISK` | R2 + R3: see-also points to KEP-4153, which is replaced |
| KEP-1287 / KEP-4381 | `HUMAN_GATE_REQUIRED` | in-place pod resize and DRA each reach several SIGs within 3 hops (the UI shows the exact counts) |

## Safety design

- **Read-only agent:** agent reads go only through 16 named, parameterized templates (`agent/templates.py`). They are
  executed with `GRAPH.RO_QUERY` and a 3000 ms timeout, and every one ends in `LIMIT ≤ 100`.
- **Two layers against writes:**
  - `agent/guard.py` rejects `CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV` and any
    non-allow-listed `CALL`. It also rejects a missing or oversized `LIMIT` and a timeout over 3 s. It runs on every
    template at import time and again before each execution.
  - FalkorDB's `RO_QUERY` independently refuses writes.
  - `make lint` runs `scripts/lint_cypher.py`, which checks that no `agent/` module other than the approval gate
    calls a write-capable `.query(`.
- **The LLM never writes Cypher and never decides:**
  - If an LLM is configured (see "Language model (optional)" below), it only picks one tool and fills its
    parameters. Those parameters are validated by pydantic (`agent/router.py`) before any query runs: unknown
    fields, unknown tools and malformed ids are rejected with a 422. It has no write tool, never sees Cypher and
    never sets a verdict.
  - Optionally, the LLM writes a 3-sentence summary of a staged notice. Quorum rejects that summary if it names
    any KEP, SIG or person that isn't in the notice.
  - Without a configured LLM, or without a passing smoke test for it, a deterministic keyword router answers
    everything and the UI says "Keywords only" and why. LLM errors are shown to the user, never silently replaced.
  - The API key comes only from the environment, is never logged or returned, and is scrubbed from every error
    message and result file. `tests/test_no_secrets.py` checks that `.env` is ignored and that no tracked file or
    commit contains a key.
  - `tests/test_llm.py`, `tests/test_llm_smoke.py` and the LLM tests in `tests/test_api.py` run the real `openai`
    client against a local stub server (request shape, refusals, API errors, 429s, invalid parameters, invented
    entities in a summary); the test session cannot see the real key.
- **Nothing is posted anywhere:** Quorum never posts to GitHub or any external service. Its only outbound traffic is
  the one-time corpus `git clone`, the optional LLM calls (only if you set a key), and two web fonts the page loads
  from Google Fonts (it falls back to system fonts offline). `/approve` only accepts notice ids that this server
  staged, refuses notices staged on older graph data, and `MERGE`s on the notice id, so a double click records one
  precedent.
- **Local only:** FalkorDB's image runs Redis without authentication, so `docker-compose.yml` publishes its ports on
  `127.0.0.1` only. Do not expose them.
- **Fails loudly:** an unready graph returns 503, an unknown KEP or SIG 404, an ambiguous KEP number 409, an
  unroutable question 422. No fake data and no fallbacks.
- **Idempotent ingest:** everything is `UNWIND … MERGE`, and `Meta` holds only deterministic values. Re-running
  `make ingest` leaves every count and property unchanged, which an integration test checks. `--rebuild` drops
  and reloads the graph when the upstream data changes.

## Setup (Linux and macOS)

**Requirements.** Docker Engine or Docker Desktop with the Compose v2 plugin (`docker compose version` must work; we
tested Docker 26 with Compose 2.26), Python 3.11 or newer with `venv` (we tested 3.12 and 3.13; on Debian or Ubuntu
install it with `sudo apt install python3-venv`), git and GNU make. FalkorDB and the app together need roughly 1 GB of
free memory and a few hundred MB of disk.

```bash
git clone https://github.com/Lucifer0000007/quorum && cd quorum
docker compose up -d --wait falkordb   # FalkorDB v4.22.1 on 127.0.0.1:6379 (Browser on :3000); returns once healthy
make venv                              # creates .venv and installs requirements.txt
make clone-data                        # kubernetes/enhancements at the pinned commit (CORPUS_COMMIT) into data/enhancements
make ingest                            # parse + idempotent MERGE into the graph "company_brain" (about 2 seconds)
make run                               # serves http://127.0.0.1:8000: leave it running and open that URL
```

(`--wait` needs Compose 2.1 or newer. `make up` does the same and also waits for PING.) In a second terminal, from the
same directory:

```bash
make check      # the graph is ready, its labels and edges exist, the Cypher lint passes
make test       # unit tests, and integration tests against the live graph
make eval       # regenerates both gold sets from the YAML, compares both systems, rewrites evals/results.json
make dev-deps   # once: Playwright and Chromium (on Linux this also installs system libraries, so it needs root)
make ui-smoke   # real-browser click-through of the demo flow on a throwaway server and graph
```

`make check` prints the data hash. It should equal the data hash in "The data, in numbers" below, because the corpus is
pinned to one upstream commit (the file `CORPUS_COMMIT`): everyone gets the same data, the same numbers as this README,
and the tests that name specific KEPs stay valid. For the latest upstream data use
`make clone-data CORPUS_COMMIT=master` (the app and `make eval` work on any commit, but those tests and the README
figures may then differ).

Other targets: `make lint` (ruff and the Cypher lint), `make report` (writes `data/report.json`), `make verdicts`
(bailiff over every KEP, writes `data/verdict_report.json`), `make readme` (regenerates the numbers and Cypher in this
file), `make status` (regenerates `docs/STATUS.md`), `make down` (stops FalkorDB; the data stays in a Docker volume).

The test suite and `make ui-smoke` run against throwaway episodic graphs (`agent_episodic_memory_test`,
`agent_episodic_memory_uismoke`), selected with `QUORUM_EPISODIC_GRAPH` (`QUORUM_BRAIN_GRAPH` does the same for the
main graph), so they never write to the demo's real episodic memory.

Optional: an LLM for tool selection, see "Language model (optional)" below. Nothing else needs a key.

To run the app in Docker too: `docker compose up -d --wait falkordb && make clone-data`, then
`docker compose --profile app run --rm app python -m ingest.load` and `docker compose --profile app up -d`.

### Windows (optional helper)

This is how we built it, on a Windows machine without Docker Desktop: Docker Engine runs inside WSL2 Debian, with
`DOCKER := wsl -d Debian -u root -- docker` in a git-ignored `local.mk`, and `make venv PYTHON=python` creates the
virtualenv. WSL stops idle distros and takes Docker with them, so the optional helper
`powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1` keeps WSL alive, starts FalkorDB, ingests if the graph is
missing or not ready, and serves the app (leave that window open). `docs/STATUS.md` lists its steps.

**There is no live deployment;** this section is the complete local setup.

## Language model (optional)

Quorum answers every question deterministically in **Keywords only** mode. Optionally an LLM can read a free-form
question and choose which of the ten tools to call and with which parameters; code validates those parameters and runs
the same read-only Cypher templates. The LLM never sets a verdict and never writes Cypher.

Any OpenAI-compatible chat-completions API with tool calling works. Configure it only through environment variables
(put them in `.env`, which git ignores; `.env.example` has OpenRouter and Gemini examples): `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`. All three are required, so a key can never be sent to a provider you didn't name.

**When the LLM is actually used.** "Auto" uses it only when it is configured **and** `python -m scripts.llm_smoke` has
passed for that exact provider and model. The smoke test asks 13 questions against the real API (every tool, an
invented KEP id that must come back as not found, and `delete KEP-2221`, which must not write), prints PASS, FAIL or
RATE LIMITED per question with the chosen tool, validated parameters and raw error, and writes
`evals/llm_smoke.json`. HTTP 429 gets a short retry and is then reported as rate limited, not as a failure. With no
key, or with any failure or rate-limited question, Quorum defaults to Keywords only and the UI header says why. If a
model turns out not to support tool calling, Quorum says so loudly and switches to Keywords only. The UI header always
shows the active mode and, for the LLM, the provider and model.

<!-- LLM:START -->

- Provider and model tested: **OpenRouter**, `nvidia/nemotron-3-super-120b-a12b:free` (OpenAI-compatible chat completions with tool calling).
- Smoke test (`python -m scripts.llm_smoke`), run against the real API on 2026-10-18T16:41:15+00:00: **13 passed, 0 failed, 0 rate limited** of 13 questions (`evals/llm_smoke.json`).
- Mode for anyone who configures exactly this provider and model: LLM. With any other configuration, or none, the default is Keywords only until the smoke test has passed for it.

<!-- LLM:END -->

## Demo

See [DEMO.md](DEMO.md) for the 2-minute script: ask, then simulate superseding KEP-4381, then approve and see
the precedent.

## How the eval works

Two suites, `evals/multihop/` and `evals/lookups/`. In each, `make_gold.py` computes the gold answers from the raw
kep.yaml files alone: it is an independent re-implementation (`evals/yamlcorpus.py`) that never imports `ingest/` or
`agent/` and never touches FalkorDB, and a test checks that. Questions are sampled with a fixed seed.
`evals/run_eval.py` then answers every question with both systems (see Results) and writes `evals/results.json`, which
the UI header and this README read. The UI marks the figures stale if the graph data changed after the run, and the
run refuses to compare a gold file and a graph built from different corpus commits.

A review subagent separately recomputed the 3-hop reach of every KEP that has a unique number, with its own YAML
parse and BFS, and matched the graph exactly.

## Limitations

- **Sparse links:** the graph only knows links that kep.yaml records. Many see-also and replaces references
  (counts in the numbers above) point to design docs or pre-numbering `.md` paths and can't be resolved, so we
  keep them as `unresolved_refs`. Superseding a KEP may affect things the corpus never linked.
- **Proxy for activity:** "recently active" approximates activity with latest-milestone. We did not ingest
  GitHub comment or PR activity.
- **No PRR data:** `prr-approvers` is empty in every kep.yaml, and we did not ingest `keps/prod-readiness/`.
- **The eval is narrow and not neutral:** the gold answers apply the same documented cleaning rules as the ingest
  (SIG aliases, status normalization, team-alias filtering), so keyword search is penalized for not applying them,
  and a misreading shared by both implementations would not be caught. The gold generator resolves references with
  the same documented rules as the ingest (it was checked to build the same graph), and it never samples the few
  KEPs whose answers touch two ambiguous reference spellings (a mistyped path and a bare number). The multi-hop
  suite is sampled from this one corpus, and the keyword baseline is deliberately simple. The eval says nothing about whether the verdict rules are
  the right policy; that is a judgment we document rather than score.
- **Keyword router:** it is simple pattern matching. Unusual phrasings get a clear routing error, not a guess.
- **One LLM, one run:** the smoke test above was run once against one free-tier model. It shows that this model
  picks the right tool for those 13 questions, not that every model or every phrasing works, and free-tier models can
  be rate limited. Keywords only is the fallback and needs nothing.
- **Ingest never deletes:** MERGE-only ingest does not remove nodes deleted upstream. It warns when the data hash
  changes, and you use `make ingest` with `--rebuild` (`python -m ingest.load --rebuild`).
- **Engine version:** FalkorDB is pinned to the 4.x C engine (`v4.22.1`). The new 6.x Rust engine (`latest`) was not tested.
- **Ephemeral staging:** staged notices live in server memory. Restarting the server means re-running the pre-flight
  check before approving.

## Key Cypher queries

<!-- QUERIES:START -->

### Agent read templates (`agent/templates.py`, via `GRAPH.RO_QUERY`, 3 s timeout)

**`meta`** (brain graph): Load metadata and the readiness flag.

```cypher
MATCH (m:Meta {key: 'brain'})
RETURN m.ready AS ready, m.data_hash AS data_hash, m.source_commit AS source_commit, m.max_minor AS max_minor,
       m.kep_count AS kep_count, m.person_count AS person_count, m.sig_count AS sig_count, m.edge_count AS edge_count
LIMIT 1
```

**`resolve_kep`** (brain graph): Map a user-supplied KEP number or id to KEP node ids (detects ambiguous numbers).

```cypher
MATCH (k:KEP)
WHERE k.id = $kep_id OR k.number = $kep_id
RETURN k.id AS id, k.title AS title, k.path AS path
LIMIT 10
```

**`search_keps`** (brain graph): KEP picker search by number, title or folder.

```cypher
MATCH (k:KEP)
WHERE k.id = $q OR k.number = $q OR toLower(k.title) CONTAINS toLower($q) OR toLower(k.path) CONTAINS toLower($q)
RETURN k.id AS id, k.title AS title, k.status AS status, k.owning_sig AS owning_sig
ORDER BY CASE WHEN k.id = $q THEN 0 ELSE 1 END, id
LIMIT $limit
```

**`kep_detail`** (brain graph): KEP metadata, owning/participating SIGs, people by role, and direct REPLACES/SEE_ALSO neighbors.

```cypher
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:OWNED_BY]->(o:SIG)
OPTIONAL MATCH (k)-[:PARTICIPATES]->(ps:SIG)
WITH k, collect(DISTINCT o.name) AS owning_sigs, collect(DISTINCT ps.name) AS participating_sigs
OPTIONAL MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(k)
WITH k, owning_sigs, participating_sigs, collect(DISTINCT [type(r), p.handle]) AS people
OPTIONAL MATCH (k)-[n:REPLACES|SEE_ALSO]-(m:KEP)
RETURN properties(k) AS kep, owning_sigs, participating_sigs, people,
       collect(DISTINCT [startNode(n).id, type(n), endNode(n).id, m.id, m.title, m.status]) AS neighbors
LIMIT 1
```

**`blast_radius`** (brain graph): KEPs reachable via REPLACES/SEE_ALSO in either direction within 3 hops, with one shortest path each.

```cypher
MATCH (k:KEP {id: $kep_id})
MATCH p = (k)-[:REPLACES|SEE_ALSO*1..3]-(o:KEP)
WHERE o.id <> k.id
WITH o, p ORDER BY length(p)
WITH o, collect(p)[0] AS sp
OPTIONAL MATCH (o)-[:OWNED_BY]->(s:SIG)
RETURN o.id AS id, o.title AS title, o.status AS status, s.name AS owning_sig, o.source_url AS source_url,
       length(sp) AS hops, [n IN nodes(sp) | n.id] AS path_nodes,
       [r IN relationships(sp) | [startNode(r).id, type(r), endNode(r).id]] AS path_edges
ORDER BY hops, id
LIMIT 100
```

**`is_still_valid`** (brain graph): Status, successor chain, predecessors, and stale KEPs it references via SEE_ALSO (3 hops).

```cypher
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH sp = (k)<-[:REPLACES*1..5]-(s:KEP)
WITH k, s, min(length(sp)) AS sh
WITH k, collect(DISTINCT [s.id, s.title, s.status, sh]) AS successors
OPTIONAL MATCH pp = (k)-[:REPLACES*1..5]->(pr:KEP)
WITH k, successors, pr, min(length(pp)) AS ph
WITH k, successors, collect(DISTINCT [pr.id, pr.title, pr.status, ph]) AS predecessors
OPTIONAL MATCH ap = (k)-[:SEE_ALSO*1..3]->(a:KEP)
WHERE a.status IN $stale_statuses
WITH k, successors, predecessors, a, ap ORDER BY length(ap)
WITH k, successors, predecessors, a, collect(ap)[0] AS best
RETURN k.id AS id, k.title AS title, k.status AS status, k.status_raw AS status_raw, successors, predecessors,
       collect(DISTINCT [a.id, a.title, a.status,
                         CASE WHEN best IS NULL THEN [] ELSE [n IN nodes(best) | n.id] END]) AS stale_ancestors
LIMIT 1
```

**`who_knows_sig`** (brain graph): Rank people by recency-weighted KEP contributions (author/review/approve) in a SIG.

```cypher
MATCH (s:SIG {name: $sig})<-[:OWNED_BY|PARTICIPATES]-(k:KEP)
WITH DISTINCT k
MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(k)
WITH p, k, type(r) AS role,
     CASE WHEN k.latest_minor IS NOT NULL AND k.latest_minor >= $recent_min_minor THEN 2 ELSE 1 END AS w
RETURN p.handle AS handle, toInteger(sum(w)) AS score, count(DISTINCT k) AS keps,
       toInteger(sum(CASE WHEN w = 2 THEN 1 ELSE 0 END)) AS recent_contributions,
       collect(DISTINCT role) AS roles, collect(DISTINCT k.id)[0..5] AS sample_keps
ORDER BY score DESC, handle
LIMIT $limit
```

**`who_knows_kep`** (brain graph): Rank people by recency-weighted contributions to a KEP and its 2-hop REPLACES/SEE_ALSO neighborhood.

```cypher
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:REPLACES|SEE_ALSO*1..2]-(n:KEP)
WITH k, collect(DISTINCT n) + [k] AS hood
UNWIND hood AS h
WITH DISTINCT h
MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(h)
WITH p, h, type(r) AS role,
     CASE WHEN h.latest_minor IS NOT NULL AND h.latest_minor >= $recent_min_minor THEN 2 ELSE 1 END AS w
RETURN p.handle AS handle, toInteger(sum(w)) AS score, count(DISTINCT h) AS keps,
       toInteger(sum(CASE WHEN w = 2 THEN 1 ELSE 0 END)) AS recent_contributions,
       collect(DISTINCT role) AS roles, collect(DISTINCT h.id)[0..5] AS sample_keps
ORDER BY score DESC, handle
LIMIT $limit
```

**`recent_approvers`** (brain graph): Which of a KEP's approvers are recently active (listed on any KEP with a recent latest-milestone).

```cypher
MATCH (a:Person)-[:APPROVED]->(k:KEP {id: $kep_id})
OPTIONAL MATCH (a)-[:AUTHORED|REVIEWED|APPROVED]->(r:KEP)
WHERE r.latest_minor IS NOT NULL AND r.latest_minor >= $recent_min_minor
RETURN a.handle AS handle, count(DISTINCT r) AS recent_keps, max(r.latest_milestone) AS latest_seen
ORDER BY handle
LIMIT 100
```

**`why`** (brain graph): Provenance chain: kep.yaml URL, tracking issue, owning SIG, approvers/authors, KEP lineage.

```cypher
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:OWNED_BY]->(o:SIG)
OPTIONAL MATCH (a:Person)-[:APPROVED]->(k)
WITH k, o, collect(DISTINCT a.handle) AS approvers
OPTIONAL MATCH (au:Person)-[:AUTHORED]->(k)
WITH k, o, approvers, collect(DISTINCT au.handle) AS authors
OPTIONAL MATCH (k)-[l:REPLACES|SEE_ALSO]-(m:KEP)
RETURN k.id AS id, k.title AS title, k.status AS status, k.status_raw AS status_raw,
       k.creation_date AS creation_date, k.latest_milestone AS latest_milestone, k.stage AS stage,
       k.source_url AS source_url, k.issue_url AS issue_url, o.name AS owning_sig, approvers, authors,
       collect(DISTINCT [startNode(l).id, type(l), endNode(l).id, m.title]) AS lineage,
       k.unresolved_refs AS unresolved_refs
LIMIT 1
```

**`referrers`** (brain graph): KEPs that list this KEP in see-also.

```cypher
MATCH (k:KEP {id: $kep_id})<-[:SEE_ALSO]-(r:KEP)
RETURN r.id AS id, r.title AS title, r.status AS status, r.owning_sig AS owning_sig, r.source_url AS source_url
ORDER BY id
LIMIT 100
```

**`sig_overview`** (brain graph): How many KEPs a SIG owns, by status.

```cypher
MATCH (s:SIG {name: $sig})<-[:OWNED_BY]-(k:KEP)
RETURN s.name AS sig, count(k) AS owned, collect(DISTINCT k.status) AS statuses,
       toInteger(sum(CASE WHEN k.status IN $active_statuses THEN 1 ELSE 0 END)) AS active
LIMIT 1
```

**`sig_exists`** (brain graph): Check that a SIG node exists (unknown SIGs fail loudly instead of returning zero).

```cypher
MATCH (s:SIG {name: $sig})
RETURN s.name AS name
LIMIT 1
```

**`list_sigs`** (brain graph): All SIGs with the number of KEPs they own.

```cypher
MATCH (s:SIG)
OPTIONAL MATCH (k:KEP)-[:OWNED_BY]->(s)
RETURN s.name AS name, count(k) AS owned
ORDER BY owned DESC, name
LIMIT 100
```

**`central_keps`** (brain graph): Most central KEPs by PageRank over SEE_ALSO (FalkorDB algorithm procedure).

```cypher
CALL algo.pageRank('KEP', 'SEE_ALSO') YIELD node, score
RETURN node.id AS id, node.title AS title, node.status AS status, node.owning_sig AS owning_sig,
       node.source_url AS source_url, score
ORDER BY score DESC
LIMIT $limit
```

**`prior_precedents`** (episodic graph): Approved precedents previously recorded for this KEP (second graph).

```cypher
MATCH (s:Session)-[:CHECKED]->(p:Precedent)-[:ABOUT]->(r:KEPRef {id: $kep_id})
RETURN p.id AS id, p.verdict AS verdict, p.created_at AS created_at, s.id AS session_id,
       p.data_hash AS data_hash, p.action AS action
ORDER BY created_at DESC
LIMIT 5
```

### Approval-gate write (`agent/episodic.py`, graph `agent_episodic_memory` only)

```cypher
MERGE (s:Session {id: $session_id})
MERGE (p:Precedent {id: $notice_id})
ON CREATE SET p.created_at = $created_at, p.verdict = $verdict, p.kep_id = $kep_id, p.action = $action,
              p.staged_notice = $markdown, p.data_hash = $data_hash
MERGE (s)-[:CHECKED]->(p)
MERGE (r:KEPRef {id: $kep_id})
MERGE (p)-[:ABOUT]->(r)
RETURN p.id AS id, p.created_at AS created_at, p.verdict AS verdict
```

### Ingest (`ingest/load.py`, idempotent `UNWIND ... MERGE`, batches of 300)

```cypher
UNWIND $rows AS r MERGE (:SIG {name: r.name})
UNWIND $rows AS r MERGE (:Person {handle: r.handle})
UNWIND $rows AS r
MERGE (k:KEP {id: r.id})
SET k.path = r.path, k.number = r.number, k.title = r.title, k.status = r.status, k.status_raw = r.status_raw,
    k.stage = r.stage, k.latest_milestone = r.latest_milestone, k.latest_minor = r.latest_minor,
    k.creation_date = r.creation_date, k.owning_sig = r.owning_sig, k.source_url = r.source_url,
    k.issue_url = r.issue_url, k.unresolved_refs = r.unresolved_refs
UNWIND $rows AS r MATCH (a:KEP {id: r.src}) MATCH (b:KEP {id: r.dst}) MERGE (a)-[:SEE_ALSO]->(b)
UNWIND $rows AS r MATCH (p:Person {handle: r.src}) MATCH (k:KEP {id: r.dst}) MERGE (p)-[:AUTHORED]->(k)
```

<!-- QUERIES:END -->

## AI usage

This was built during the event with an AI coding assistant (Claude Code). See [AI_USAGE.md](AI_USAGE.md) for
the running log and the assumptions it stated instead of asking.

## Repository layout

```
ingest/   config.py, parse.py (pure), load.py (MERGE)       agent/  guard, templates, db, queries, bailiff,
ui/       index.html, app.js, style.css (no build)                  router, llm, episodic, api
scripts/  data_report, check_graph, lint_cypher, verdict_report, llm_smoke, ui_smoke, render_readme, render_status,
          wait_falkordb, dev_up.ps1 (optional Windows helper)
evals/    multihop/ and lookups/ (make_gold.py + gold.json each), yamlcorpus.py, baseline.py, run_eval.py,
          results.json, llm_smoke.json
docs/     STATUS.md (generated: what ran, with the command that reproduces each number)
tests/    real kep.yaml fixtures; unit + live-graph integration tests
```
