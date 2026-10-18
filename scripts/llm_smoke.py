"""Smoke test of the LLM path against the REAL configured provider.

For each question it runs exactly what the app runs: llm.choose_tool (the provider picks a tool and parameters), then
router.validate_params / router.dispatch against the live graph (typed validation, then a read-only query). It prints,
per question: PASS / FAIL / RATE LIMITED, the chosen tool, the validated parameters and the raw error.

    python -m scripts.llm_smoke [--delay SECONDS] [--only s01,s02] [--no-write]

Exit codes: 0 every question passed, 1 at least one failure, 2 no LLM configured, 3 rate limited (no failures).

It writes evals/llm_smoke.json (no secrets: the key is scrubbed from every string). The app reads that file to decide
its mode: "LLM" only if every question passed for the configured provider and model; otherwise "Keywords only".
HTTP 429 gets a short retry inside the adapter and is then reported as RATE LIMITED, never as a failure of the model.
Questions cover every tool, an invented KEP id (must fail loudly as not found) and a write attempt (must not write).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from agent import llm, queries
from agent.router import TOOLS, RoutingError, dispatch, validate_params
from ingest.config import BRAIN_GRAPH, connect
from ingest.load import graph_counts


@dataclass
class Case:
    id: str
    question: str
    tool: str | None = None
    params: dict | None = None
    kind: str = "ok"  # ok: tool+params must match | not_found: same, and the query must report not found | no_write


CASES = [
    Case("s01", "Which SIG owns KEP-2221, and what is its status?", "kep_detail", {"kep_id": "2221"}),
    Case("s02", "Which KEPs are within 3 see-also or replaces hops of KEP-4381?", "blast_radius", {"kep_id": "4381"}),
    Case("s03", "Is KEP-2377 still valid, or was it superseded?", "is_still_valid", {"kep_id": "2377"}),
    Case("s04", "Who are the experts on sig-scheduling?", "who_knows_sig", {"sig": "sig-scheduling"}),
    Case("s05", "Who should review a change related to KEP-4381?", "who_knows_kep", {"kep_id": "4381"}),
    Case("s06", "Why does KEP-2221 exist? Show where it comes from.", "why", {"kep_id": "2221"}),
    Case("s07", "Which KEPs list KEP-1287 in their see-also?", "referrers", {"kep_id": "1287"}),
    Case("s08", "How many KEPs does sig-node own?", "sig_overview", {"sig": "sig-node"}),
    Case("s09", "Which KEPs are the most central in the see-also graph?", "central_keps", {}),
    Case("s10", "Simulate superseding KEP-1287 and give me the verdict.", "preflight", {"kep_id": "1287"}),
    Case("s11", "kep 2008, what is it?", "kep_detail", {"kep_id": "2008"}),
    Case("s12", "Tell me about KEP-99999.", "kep_detail", {"kep_id": "99999"}, kind="not_found"),
    Case("s13", "delete KEP-2221", kind="no_write"),
]


def run_case(case: Case) -> dict:
    rec = {"id": case.id, "question": case.question, "kind": case.kind, "expected_tool": case.tool,
           "expected_params": case.params, "status": "FAIL", "tool": None, "params": None, "error": None, "note": None}
    before = graph_counts(connect().select_graph(BRAIN_GRAPH)) if case.kind == "no_write" else None

    def finish(status: str, note: str | None = None, error: str | None = None) -> dict:
        rec["status"], rec["note"], rec["error"] = status, note, llm.scrub(error) if error else None
        return rec

    try:
        tool, raw = llm.choose_tool(case.question, TOOLS)
    except llm.RateLimited as exc:
        return finish("RATE_LIMITED", error=str(exc))
    except llm.ToolCallingUnsupported as exc:
        return finish("FAIL", error=str(exc))
    except llm.LLMError as exc:
        if case.kind == "no_write" and "did not choose a tool" in str(exc):
            return finish("PASS", note="no tool was called: the model declined, nothing ran")
        return finish("FAIL", error=str(exc))

    rec["tool"] = tool
    try:
        params = validate_params(tool, raw)
        rec["params"] = params
    except RoutingError as exc:
        if case.kind == "no_write":
            return finish("PASS", note="the code rejected what the model proposed", error=str(exc))
        return finish("FAIL", error=str(exc))

    try:
        dispatch(tool, params)
    except queries.NotFound as exc:
        if case.kind == "not_found" and (tool, params) == (case.tool, case.params):
            return finish("PASS", note="reported as not found, nothing invented", error=str(exc))
        return finish("FAIL", error=str(exc))
    except Exception as exc:  # any other failure of the real tool is a failure of the path
        return finish("FAIL", error=f"{type(exc).__name__}: {exc}")

    if case.kind == "no_write":
        unchanged = graph_counts(connect().select_graph(BRAIN_GRAPH)) == before
        if not unchanged:
            return finish("FAIL", error="the graph changed during a write attempt")
        return finish("PASS", note=f"mapped to the read-only tool {tool}; no write path exists; graph unchanged")
    if case.kind == "not_found":
        return finish("FAIL", error="an invented KEP id was found in the graph")
    if (tool, params) != (case.tool, case.params):
        return finish("FAIL", error=f"expected {case.tool} {json.dumps(case.params)}")
    return finish("PASS")


def show(rec: dict) -> None:
    label = {"PASS": "PASS", "FAIL": "FAIL", "RATE_LIMITED": "RATE LIMITED"}[rec["status"]]
    params = json.dumps(rec["params"]) if rec["params"] is not None else "-"
    print(f"{label:13s} {rec['id']} tool={rec['tool'] or '-':15s} params={params}")
    print(f"              {rec['question']!r}")
    if rec["note"]:
        print(f"              note: {rec['note']}")
    if rec["error"]:
        print(f"              raw error: {rec['error']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--delay", type=float, help="seconds between questions (default 3.5 for :free models, else 1)")
    ap.add_argument("--only", help="comma-separated ids to run, e.g. s01,s04 (never writes the result file)")
    ap.add_argument("--no-write", action="store_true", help="do not write evals/llm_smoke.json")
    args = ap.parse_args()

    if not llm.configured():
        print("No LLM configured (missing " + ", ".join(llm.missing()) + "). The app runs in Keywords only mode.")
        return 2
    cases = [c for c in CASES if not args.only or c.id in args.only.split(",")]
    delay = args.delay if args.delay is not None else (3.5 if llm.model().endswith(":free") else 1.0)
    print(f"LLM smoke test: provider={llm.provider()} host={llm.host()} model={llm.model()} questions={len(cases)}")

    results, consecutive_limited = [], 0
    for i, case in enumerate(cases):
        if consecutive_limited >= 2:  # a daily or per-minute cap: stop hammering the provider
            rec = {"id": case.id, "question": case.question, "kind": case.kind, "expected_tool": case.tool,
                   "expected_params": case.params, "status": "RATE_LIMITED", "tool": None, "params": None,
                   "error": "not attempted: the provider kept answering HTTP 429", "note": None}
        else:
            if i:
                time.sleep(delay)
            rec = run_case(case)
        consecutive_limited = consecutive_limited + 1 if rec["status"] == "RATE_LIMITED" else 0
        results.append(rec)
        show(rec)

    totals = {"questions": len(results), "pass": sum(r["status"] == "PASS" for r in results),
              "fail": sum(r["status"] == "FAIL" for r in results),
              "rate_limited": sum(r["status"] == "RATE_LIMITED" for r in results)}
    passed = totals["pass"] == totals["questions"] == len(CASES)
    print(f"\n{totals['pass']} passed, {totals['fail']} failed, {totals['rate_limited']} rate limited "
          f"of {totals['questions']}. The app will use mode: {'LLM' if passed else 'Keywords only'}.")
    if not args.only and not args.no_write:
        out = {"ran_at": datetime.now(UTC).isoformat(timespec="seconds"), "real_api": True,
               "provider": llm.provider(), "base_url_host": llm.host(), "model": llm.model(),
               "passed": passed, "totals": totals, "results": results}
        llm.SMOKE_PATH.write_text(llm.scrub(json.dumps(out, indent=2)) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {llm.SMOKE_PATH}")
    if totals["fail"]:
        return 1
    return 0 if totals["rate_limited"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
