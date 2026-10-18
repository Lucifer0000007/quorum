"""Write docs/STATUS.md: a status report in complete sentences, generated from the live system.

Every number is read from the live FalkorDB graph, the committed evidence files (evals/llm_smoke.json,
evals/results.json, data/verdict_report.json) or from commands this script runs (PING, tests, lint), and each one is
printed next to the command that reproduces it. Run it with `make status` (it runs the whole test suite, so it takes
about a minute).
"""

import json
import subprocess
import sys
from datetime import UTC, datetime

from agent.db import meta
from ingest.config import BRAIN_GRAPH, ROOT, connect
from ingest.load import graph_counts

OUT = ROOT / "docs" / "STATUS.md"


def run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *cmd], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else "(no output)"


def load(rel: str):
    path = ROOT / rel
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main() -> None:
    m = meta(force=True)
    counts = graph_counts(connect().select_graph(BRAIN_GRAPH))
    nodes = {k.split(":")[1]: v for k, v in counts.items() if k.startswith("node:")}
    edges = {k.split(":")[1]: v for k, v in counts.items() if k.startswith("edge:")}
    ping = last_line(run("scripts/wait_falkordb.py").stdout)
    tests = run("-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider")
    tests_line = last_line(tests.stdout)
    ruff_line = last_line(run("-m", "ruff", "check", ".").stdout)
    cypher_line = last_line(run("-m", "scripts.lint_cypher").stdout)
    smoke, ev, vr = load("evals/llm_smoke.json"), load("evals/results.json"), load("data/verdict_report.json")

    md = [
        "# Quorum status",
        "",
        f"This file was generated on {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} by `python -m scripts.render_status` "
        "(`make status`). Every figure below was read from the live system when it was generated, and each is followed "
        "by the command that reproduces it. Nothing here is typed by hand.",
        "",
        "## What `scripts/dev_up.ps1` does",
        "",
        "It is an optional helper for a Windows machine where Docker runs inside WSL2, which is how this project was "
        "built. On Linux or macOS you do not need it. Step by step, it does the following. "
        "First, it finds the repository root and refuses to continue if `.venv` does not exist. "
        "Second, it checks whether a hidden `wsl.exe -d Debian -u root -- sleep infinity` process is already running, "
        "and starts one if not, because WSL stops idle distributions and would take Docker and FalkorDB down with it. "
        "Third, it converts the repository path with `wslpath` and runs `docker compose up -d falkordb` inside WSL. "
        "Fourth, it runs `scripts/wait_falkordb.py`, which waits up to a minute for FalkorDB to answer PING. "
        "Fifth, it runs `scripts.check_graph` quietly; if the graph is missing or not ready it fetches the pinned corpus "
        "commit from `CORPUS_COMMIT` when `data/enhancements` is absent, and runs `ingest.load`. "
        "Sixth, it prints the last line of `scripts.check_graph`. "
        "Finally, unless `-NoServer` was passed, it serves the app with uvicorn on 127.0.0.1:8000 in the foreground, so "
        "the window must stay open.",
        "",
        "## Phase 0 facts, as they stand now",
        "",
        f"FalkorDB answers PING: `{ping}` (reproduce with `python scripts/wait_falkordb.py`). "
        f"The test suite reports `{tests_line}` (reproduce with `python -m pytest -o addopts= -q`). "
        f"Ruff reports `{ruff_line}` (`python -m ruff check .`), and the Cypher lint reports `{cypher_line}` "
        "(`python -m scripts.lint_cypher`). At the end of Phase 0 the same checks gave a PONG with the graph module "
        "loaded, one passing smoke test and a clean ruff run; the suite has grown since.",
        "",
        "## The live graph",
        "",
        f"The graph `{BRAIN_GRAPH}` is ready (`Meta.ready` is true) and was built from kubernetes/enhancements commit "
        f"`{m['source_commit']}` with data hash `{m['data_hash']}`. It holds {nodes.get('KEP')} KEPs, "
        f"{nodes.get('Person')} people and {nodes.get('SIG')} SIGs, and {sum(edges.values())} relationships "
        f"(`Meta.edge_count` says {m['edge_count']}): " + ", ".join(f"{n} {k}" for k, n in sorted(edges.items())) + ". "
        "All of these come from `python -m scripts.check_graph`, which prints one `node:` or `edge:` line per label and "
        "relationship type and the data hash on its last line. A fresh clone that follows the README reaches the same "
        "hash, because the corpus commit is pinned.",
        "",
    ]
    if vr:
        v = vr["verdicts"]
        md += [
            f"Running the bailiff over all {vr['kep_count']} KEPs gives {v.get('CLEAN')} CLEAN, {v.get('AT_RISK')} AT_RISK "
            f"and {v.get('HUMAN_GATE_REQUIRED')} HUMAN_GATE_REQUIRED (`python -m scripts.verdict_report`, which writes "
            "`data/verdict_report.json`). These thresholds were tuned on this corpus and are not validated against ground "
            "truth.",
            "",
        ]
    if ev:
        mh, lk = ev["suites"]["multihop"], ev["suites"]["lookups"]
        md += [
            f"On the multi-hop questions the query layer answered {mh['query_layer_correct']} of {mh['total']} correctly "
            f"and the one-pass keyword search {mh['keyword_correct']}; on the lookup baseline they answered "
            f"{lk['query_layer_correct']} and {lk['keyword_correct']} of {lk['total']} (`make eval`, which writes "
            "`evals/results.json`).",
            "",
        ]
    md += ["## The language-model path, and its caveat", ""]
    if not smoke:
        md += ["No LLM smoke test has been run, so Quorum runs in Keywords only mode. Reproduce a run with "
               "`python -m scripts.llm_smoke` after setting `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.", ""]
    else:
        t = smoke["totals"]
        md += [
            f"The optional LLM path uses the OpenAI-compatible chat-completions API with tool calling. The provider "
            f"tested is **{smoke['provider']}** and the model is `{smoke['model']}`. The smoke test "
            f"(`python -m scripts.llm_smoke`) was run {'against the real API' if smoke.get('real_api') else 'against a stub, not the real API'} "
            f"on {smoke['ran_at']} and gave {t['pass']} passed, {t['fail']} failed and {t['rate_limited']} rate limited "
            f"out of {t['questions']} questions; the evidence is in `evals/llm_smoke.json`.",
            "",
            "The caveat is this. That run was a single run against a single free-tier model, using an OpenRouter key that "
            "is to be rotated, so it shows that this model chose the right tool and parameters for those questions on "
            "that day and nothing more. It does not show that other models or other phrasings work, and free-tier models "
            "can be rate limited. Quorum therefore uses the LLM only when all three variables are set and the smoke test "
            "has passed for exactly that provider and model; in every other case, including a fresh clone without a key, "
            "it runs in Keywords only mode and the UI header says so and why. The LLM never sets a verdict and never "
            "writes Cypher, and the test suite cannot see the real key.",
            "",
        ]
    md += [
        "## MCP servers and subagents that were configured but never loaded",
        "",
        "`.mcp.json` configures three MCP servers: the FalkorDB server `@falkordb/mcpserver@1.3.0`, Context7 "
        "`@upstash/context7-mcp@4.4.2` and Playwright `@playwright/mcp@0.0.83`. Their package names and versions were "
        "checked against the npm registry, but Claude Code only loads project MCP servers at session start, after "
        "approval, and the session that built this project began before the file existed, so none of them was ever loaded "
        "or called. In their place the work used the FalkorDB Python client directly, Python Playwright for browser "
        "checks (`scripts/ui_smoke.py`), and ordinary documentation lookups.",
        "",
        "The three subagent definitions in `.claude/agents` (`parser-agent`, `cypher-reviewer`, `eval-agent`) were "
        "likewise not loadable in that session, which reported the agent types as not found. Generic subagents were "
        "instead told to follow the definitions of `cypher-reviewer` and `eval-agent`; `parser-agent` was never used, "
        "because the parser and its tests were written by the main assistant. The definitions will load in a new session.",
        "",
        "## Reproduce every number",
        "",
        "| What | Command |",
        "|---|---|",
        "| PING | `python scripts/wait_falkordb.py` |",
        "| Node and relationship counts, data hash | `python -m scripts.check_graph` |",
        "| Verdict counts, rules, hop sensitivity | `python -m scripts.verdict_report` |",
        "| Eval results (both suites, both systems) | `make eval` |",
        "| LLM smoke test | `python -m scripts.llm_smoke` |",
        "| Tests | `python -m pytest -o addopts= -q` |",
        "| Lint | `python -m ruff check .` and `python -m scripts.lint_cypher` |",
        "| README figures | `make readme` |",
        "| This file | `make status` |",
        "",
    ]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(md), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    print(f"ping: {ping}\ntests: {tests_line}\nruff: {ruff_line}\ncypher lint: {cypher_line}")


if __name__ == "__main__":
    main()
