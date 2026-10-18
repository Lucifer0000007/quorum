"""Run both evaluation suites through two systems and compare them. Writes evals/results.json.

Suites (gold answers come from evals/<suite>/make_gold.py, which parses the YAML only; no FalkorDB, no ingest/):
  multihop  questions that need paths over see-also/replaces: reach within 2 and 3 hops, active KEPs reachable from a
            stale KEP, the SIGs that own them
  lookups   single-fact questions (owner, status, successor, referrers, counts, approvers): a baseline

Systems answering every question:
  query_layer     Quorum. Lookups go through the real app path (keyword router -> typed tool -> FalkorDB). Multi-hop
                  questions call agent/queries.py blast_radius, whose rows carry each KEP's hop distance, status and
                  owning SIG; the harness only filters those rows (hops <= 2, active, distinct SIGs).
  keyword_search  evals/baseline.py: one retrieval round of keyword search over the raw kep.yaml text, no graph.

A question is "correct" for a system when its answer equals the gold answer. The outcome per question is one of
tie (both correct), query_layer_only, keyword_only, both_wrong. Nothing here is hardcoded: every figure in the README
and the UI is read from the file this script writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agent import queries
from agent.db import meta
from agent.router import dispatch, keyword_route
from evals.baseline import KeywordSearch
from ingest.config import ENHANCEMENTS_DIR

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.json"
OUTCOMES = ("tie", "query_layer_only", "keyword_only", "both_wrong")

LOOKUP_EXTRACT = {
    "owning_sig": lambda out: (out["result"]["owning_sigs"] or [None])[0],
    "status": lambda out: out["result"]["kep"]["status"],
    "has_successor": lambda out: out["result"]["has_successor"],
    "see_also_referrers": lambda out: sorted(r["id"] for r in out["result"]["referrers"]),
    "sig_kep_count": lambda out: out["result"]["owned"],
    "approvers": lambda out: out["result"]["approvers"],
}


def _reach(params: dict) -> dict:
    return queries.blast_radius(params["kep_id"])["result"]


MULTIHOP_EXTRACT = {
    "hop2_set": lambda p: sorted(r["id"] for r in _reach(p)["reachable"] if r["hops"] <= 2),
    "reachable_count_3hop": lambda p: _reach(p)["reachable_count"],
    "stale_reach": lambda p: sorted(r["id"] for r in _reach(p)["reachable"] if r["active"]),
    "reachable_sigs": lambda p: sorted({r["owning_sig"] for r in _reach(p)["reachable"]
                                        if r["active"] and r["owning_sig"]}),
}

SUITES = {
    "multihop": {"title": "Multi-hop", "gold": HERE / "multihop" / "gold.json"},
    "lookups": {"title": "Lookups (baseline)", "gold": HERE / "lookups" / "gold.json"},
}


def query_layer(suite: str, q: dict):
    if suite == "lookups":
        tool, params = keyword_route(q["question"])
        return LOOKUP_EXTRACT[q["kind"]](dispatch(tool, params))
    return MULTIHOP_EXTRACT[q["kind"]](q["params"])


def norm(value):
    return sorted(value) if isinstance(value, list) else value


def attempt(fn) -> dict:
    """Run one system on one question; an exception is a failed answer with its error recorded, never a crash."""
    try:
        return {"answer": fn(), "error": None}
    except Exception as exc:  # noqa: BLE001 - a system that errors simply gets the question wrong
        return {"answer": None, "error": f"{type(exc).__name__}: {exc}"}


def outcome(ql_ok: bool, kw_ok: bool) -> str:
    return {(True, True): "tie", (True, False): "query_layer_only", (False, True): "keyword_only",
            (False, False): "both_wrong"}[(ql_ok, kw_ok)]


def run_suite(suite: str, gold: dict, baseline: KeywordSearch) -> dict:
    rows = []
    for q in gold["questions"]:
        ql = attempt(lambda q=q: query_layer(suite, q))
        kw = attempt(lambda q=q: baseline.answer(q["kind"], q["params"]))
        ql["ok"] = ql["error"] is None and norm(ql["answer"]) == norm(q["gold"])
        kw["ok"] = kw["error"] is None and norm(kw["answer"]) == norm(q["gold"])
        rows.append({"id": q["id"], "kind": q["kind"], "question": q["question"], "params": q["params"],
                     "gold": q["gold"], "query_layer": ql, "keyword_search": kw,
                     "outcome": outcome(ql["ok"], kw["ok"])})

    def tally(subset: list[dict]) -> dict:
        return {"total": len(subset),
                "query_layer_correct": sum(r["query_layer"]["ok"] for r in subset),
                "keyword_correct": sum(r["keyword_search"]["ok"] for r in subset),
                "outcomes": {o: sum(r["outcome"] == o for r in subset) for o in OUTCOMES}}

    kinds = sorted({r["kind"] for r in rows})
    return {"title": SUITES[suite]["title"], **tally(rows),
            "by_kind": {k: tally([r for r in rows if r["kind"] == k]) for k in kinds}, "questions": rows}


def evaluate() -> dict:
    """Both suites, both systems. Refuses to compare answers computed on different corpus commits."""
    m = meta()
    golds = {}
    for suite, cfg in SUITES.items():
        if not cfg["gold"].exists():
            raise SystemExit(f"{cfg['gold']} is missing. Run `make eval` (it generates the gold files first).")
        golds[suite] = json.loads(cfg["gold"].read_text(encoding="utf-8"))
        if golds[suite]["source_commit"] != m["source_commit"]:
            raise SystemExit(
                f"{suite}: gold answers come from corpus commit {golds[suite]['source_commit'][:12]} but the graph "
                f"was ingested from {str(m['source_commit'])[:12]}. Re-run `make ingest` and `make eval`.")
    baseline = KeywordSearch(ENHANCEMENTS_DIR)
    return {
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_hash": m["data_hash"],
        "corpus_commit": m["source_commit"],
        "systems": {
            "query_layer": "Quorum: keyword router + typed tools for lookups; agent/queries.py blast_radius "
                           "(hop distance per KEP) for multi-hop questions; FalkorDB underneath",
            "keyword_search": "evals/baseline.py: one retrieval round of keyword search over the raw kep.yaml text",
        },
        "suites": {suite: run_suite(suite, golds[suite], baseline) for suite in SUITES},
    }


def main() -> dict:
    results = evaluate()
    RESULTS.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    for s in results["suites"].values():
        o = s["outcomes"]
        print(f"{s['title']}: query layer {s['query_layer_correct']}/{s['total']}, keyword search "
              f"{s['keyword_correct']}/{s['total']}  (tie {o['tie']}, query layer only {o['query_layer_only']}, "
              f"keyword only {o['keyword_only']}, both wrong {o['both_wrong']})")
        for kind, k in s["by_kind"].items():
            print(f"  {kind:22s} query layer {k['query_layer_correct']}/{k['total']}   "
                  f"keyword search {k['keyword_correct']}/{k['total']}")
        for r in s["questions"]:
            if not r["query_layer"]["ok"]:
                print(f"  QUERY LAYER WRONG {r['id']} {r['question']!r}: gold={r['gold']!r} "
                      f"answer={r['query_layer']['answer']!r} {r['query_layer']['error'] or ''}")
    return results


if __name__ == "__main__":
    main()
