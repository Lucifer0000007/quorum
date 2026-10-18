# Quorum: 2-minute demo script

**Setup before recording:** `make up && make ingest && make run`, then open http://127.0.0.1:8000. On Windows with
Docker in WSL, run `powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1` instead and leave it open. Optionally
open the FalkorDB Browser on http://localhost:3000 in a second tab, on graph `company_brain`.

The header should show: graph ready, the KEP count, the mode (`LLM` with provider and model, or `Keywords only` and
why), and the eval results from `evals/results.json`. For a fully deterministic take, pick **Keywords only** in the
Ask box's dropdown (or leave `LLM_MODEL` empty in `.env`) and say so.
Close heavy browser tabs first: the app, FalkorDB and WSL together need a couple of GB free.

**Start from a clean precedent store** so the "checked before" banner first appears after your own approval:
the graph `agent_episodic_memory` should not exist yet (the tests and `make ui-smoke` use separate throwaway graphs).
To reset it between takes: `wsl -d Debian -u root -- docker exec quorum-falkordb-1 redis-cli GRAPH.DELETE agent_episodic_memory`
(with plain Docker: `docker exec quorum-falkordb-1 redis-cli GRAPH.DELETE agent_episodic_memory`).

The figures below (for example "37 active KEPs across 6 SIGs") are what the pinned corpus commit produces; your screen
shows the live values.

## 0:00–0:45 Ask the company brain

1. Say: "Quorum treats the Kubernetes project as a company. SIGs are teams, KEPs are decisions, and
   people are authors, reviewers and approvers. It's all one FalkorDB graph built from public kep.yaml files."
2. Click the example **"Does KEP-2377 have a successor?"**.
   - Answer: *implemented → superseded. Successors: KEP-2386.*
   - Point at the right panel: "This is the exact Cypher that ran: a read-only template, `RO_QUERY`, a 3-second
     timeout and a LIMIT. The model never writes Cypher."
3. Click **"Who knows about sig-scheduling?"**.
   - A ranked list of people appears, with the scoring formula printed under it. Say: "Recency comes only from
     kep.yaml milestones. We don't claim activity data we don't have."
4. Click a **Sources** link to show it opens the real kep.yaml on GitHub.

## 0:45–1:35 Simulate superseding a decision

1. In **Pick a KEP to supersede**, type `4381` (DRA Structured Parameters) and click **Simulate superseding it**.
2. The verdict stamp reads **human gate required**. Read the reasons:
   - **R1:** 37 active KEPs across 6 SIGs within 3 hops.
   - **R3:** it references KEP-3063, which was withdrawn, through see-also.
3. Scroll the **Blast radius** table. Each row shows its graph path (e.g. `4815 -[SEE_ALSO]-> 4381`) and hop
   count. Say: "This is a variable-length traversal in FalkorDB, `REPLACES|SEE_ALSO*1..3`, in both directions."
4. Quickly show the contrast: type `1020` and simulate. It comes back **clean**, with *"No path found"*. Say: "No path is a
   real answer, not an error."
5. Go back to `4381`.

## 1:35–2:00 Approve, precedent, eval

1. Point at the **Staged impact notice**: "Staged, not sent. Quorum never posts anywhere."
2. Click **Approve and record precedent**. The confirmation reads *Precedent recorded in agent_episodic_memory …
   Posted externally: no.*
3. Simulate `4381` again. The blue banner reads *"This has been checked before"*. That banner comes from the second graph.
4. End on the header's **Eval** cell: read the multi-hop figure and the keyword-search figure under it off the
   screen. "Multi-hop questions answered by the graph, against a plain keyword search over the same files. Gold
   answers are computed straight from the YAML by a separate script, and the numbers come from the eval run, not
   from me." (Lookups are only a baseline, and say so.)

Backup lines if asked:
- "Verdicts are fixed rules (R1–R4) in `agent/bailiff.py`. Across all 648 KEPs the live distribution
  exactly matches a pure-Python re-implementation over the raw YAML."
- "Re-running ingest is a no-op: everything is MERGE, and a test proves the counts don't change."
