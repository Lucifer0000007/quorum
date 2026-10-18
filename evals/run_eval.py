"""Ask every gold question through the real app path (keyword router -> typed dispatch -> FalkorDB) and score it.

Gold answers come from evals/make_gold.py, which reads the raw YAML and never touches FalkorDB or ingest/.
Writes evals/results.json. The pass rate is whatever this run measures; nothing is hardcoded.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from agent.db import meta
from agent.router import dispatch, keyword_route

HERE = Path(__file__).resolve().parent

# How to read each kind's answer out of a tool result.
EXTRACT = {
    "owning_sig": lambda out: (out["result"]["owning_sigs"] or [None])[0],
    "status": lambda out: out["result"]["kep"]["status"],
    "has_successor": lambda out: out["result"]["has_successor"],
    "see_also_referrers": lambda out: sorted(r["id"] for r in out["result"]["referrers"]),
    "sig_kep_count": lambda out: out["result"]["owned"],
    "blast_radius_count": lambda out: out["result"]["reachable_count"],
    "approvers": lambda out: out["result"]["approvers"],
}


def norm(v):
    return sorted(v) if isinstance(v, list) else v


def main() -> dict:
    gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    rows, passed, t0 = [], 0, time.time()
    for q in gold["questions"]:
        row = {"id": q["id"], "kind": q["kind"], "question": q["question"], "gold": q["gold"]}
        try:
            tool, params = keyword_route(q["question"])
            out = dispatch(tool, params)
            answer = EXTRACT[q["kind"]](out)
            row |= {"tool": tool, "params": params, "answer": answer, "ok": norm(answer) == norm(q["gold"])}
        except Exception as exc:  # a failure is a failed question, recorded with its error
            row |= {"tool": None, "answer": None, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        passed += row["ok"]
        rows.append(row)
    m = meta()
    results = {
        "passed": passed,
        "total": len(rows),
        "rate": round(passed / len(rows), 4) if rows else 0.0,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_hash": m["data_hash"],
        "gold_source_commit": gold["source_commit"],
        "graph_source_commit": m["source_commit"],
        "router": "keyword (deterministic)",
        "seconds": round(time.time() - t0, 2),
        "by_kind": {},
        "questions": rows,
    }
    for r in rows:
        k = results["by_kind"].setdefault(r["kind"], {"passed": 0, "total": 0})
        k["total"] += 1
        k["passed"] += r["ok"]
    (HERE / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8", newline="\n")
    print(f"Eval: {passed}/{len(rows)} passed ({results['rate']:.1%}) in {results['seconds']}s")
    for kind, k in results["by_kind"].items():
        print(f"  {kind:20s} {k['passed']}/{k['total']}")
    for r in rows:
        if not r["ok"]:
            print(f"  FAIL {r['id']} {r['question']!r}: gold={r['gold']!r} answer={r['answer']!r} {r.get('error', '')}")
    return results


if __name__ == "__main__":
    main()
