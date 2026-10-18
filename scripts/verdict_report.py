"""Verdict report: run the bailiff over every KEP in the live graph and summarize how its rules behave.

Read-only: it calls preflight() for each KEP (graph reads only; nothing is staged, approved or written to any graph)
and writes data/verdict_report.json. Run it as `python -m scripts.verdict_report` (make verdicts).

The JSON depends only on the graph and the code (sorted keys, no timestamps, no timings), so re-running it on the
same data leaves git clean. Keys:

  data_hash              Meta.data_hash of the graph the report was computed on
  kep_count              KEPs checked
  min_sigs_for_gate      R1 threshold: distinct SIGs among the active KEPs within reach
  hops                   depth of the REPLACES/SEE_ALSO search behind the verdicts
  verdicts               {CLEAN, AT_RISK, HUMAN_GATE_REQUIRED: KEP count}
  rules_all_match        {R1, R2, R3, R4, CLEAN: KEPs the rule fired on}. A KEP counts once for every rule that
                         fired and under CLEAN only when none did, so R1..R4 can add up to more than kep_count
  rules_first_match      same keys; each KEP counts once, under the first rule that fired in the order R1, R2, R3,
                         R4 (CLEAN when none did), so the values add up to kep_count
  hop_sensitivity        {"1" .. str(hops): {"verdicts": as above, "keps_with_reach": int}}: decide() re-run on
                         hop_limited_facts() for each limit; keps_with_reach counts KEPs with at least one
                         reachable KEP, of any status, within that many hops. The deepest level equals `verdicts`
  reasons_have_evidence  {"total": reasons over all KEPs, "with_evidence": those with a non-empty cypher and query}
  no_reach               KEPs with no REPLACES/SEE_ALSO path to any KEP within `hops`
  largest_reach          most KEPs reachable from any single KEP within `hops`
  reach_capped           KEPs whose reach hit the LIMIT 100 of blast_radius (0 means every count above is exact)
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from agent.bailiff import RULES, decide, hop_limited_facts, preflight
from agent.db import db, meta
from agent.guard import MAX_LIMIT, MAX_TIMEOUT_MS
from ingest.config import BRAIN_GRAPH, DATA_DIR

OUT_PATH = DATA_DIR / "verdict_report.json"
VERDICTS = ("CLEAN", "AT_RISK", "HUMAN_GATE_REQUIRED")
FIRING_ORDER = tuple(rule for rule in RULES if rule != "CLEAN")  # R1, R2, R3, R4: the first that fired wins
REACH_CAP = MAX_LIMIT  # blast_radius ends in LIMIT 100, so a reach this large may have been cut off


def kep_ids() -> list[str]:
    """Every KEP id in the live graph, sorted (one read-only query)."""
    res = db().select_graph(BRAIN_GRAPH).ro_query("MATCH (k:KEP) RETURN k.id ORDER BY k.id", timeout=MAX_TIMEOUT_MS)
    return [row[0] for row in res.result_set]


def summarize(results: Iterable[dict]) -> dict:
    """Aggregate preflight() results, one per KEP, into the report dict. Pure: no graph access."""
    results = list(results)
    if not results:
        raise ValueError("No preflight results to summarize.")
    stamps = {(r["data_hash"], r["rules"]["hops"], r["rules"]["min_sigs_for_gate"]) for r in results}
    if len(stamps) != 1:
        raise RuntimeError(f"Preflight results disagree on graph data or rule settings: {sorted(stamps)}")
    ((data_hash, hops, min_sigs),) = stamps

    verdicts = dict.fromkeys(VERDICTS, 0)
    all_match = dict.fromkeys(RULES, 0)
    first_match = dict.fromkeys(RULES, 0)
    evidence = {"total": 0, "with_evidence": 0}
    hop_sensitivity = {str(h): {"verdicts": dict.fromkeys(VERDICTS, 0), "keps_with_reach": 0}
                       for h in range(1, hops + 1)}
    reach_sizes = []
    for r in results:
        verdicts[r["verdict"]] += 1
        fired = [x["rule"] for x in r["reasons"] if x["rule"] != "CLEAN"]
        for rule in fired:
            all_match[rule] += 1
        if not fired:
            all_match["CLEAN"] += 1
        first_match[next((rule for rule in FIRING_ORDER if rule in fired), "CLEAN")] += 1
        for reason in r["reasons"]:
            evidence["total"] += 1
            if reason.get("cypher") and reason.get("query"):
                evidence["with_evidence"] += 1
        reachable = r["affected_keps"] + r["inactive_reachable"]
        reach_sizes.append(len(reachable))
        for h in range(1, hops + 1):
            verdict_at_h = decide(hop_limited_facts(r, h))["verdict"]
            if h == hops and verdict_at_h != r["verdict"]:
                raise RuntimeError(f"KEP-{r['kep']['id']}: the verdict at the full {hops} hops ({verdict_at_h}) "
                                   f"differs from preflight's ({r['verdict']}).")
            level = hop_sensitivity[str(h)]
            level["verdicts"][verdict_at_h] += 1
            if any(a["hops"] <= h for a in reachable):
                level["keps_with_reach"] += 1

    no_reach = sum(1 for r in results if r["no_path_found"])
    if no_reach != len(results) - hop_sensitivity[str(hops)]["keps_with_reach"]:
        raise RuntimeError("no_path_found disagrees with the reachable KEPs of the same results.")
    return {
        "data_hash": data_hash,
        "kep_count": len(results),
        "min_sigs_for_gate": min_sigs,
        "hops": hops,
        "verdicts": verdicts,
        "rules_all_match": all_match,
        "rules_first_match": first_match,
        "hop_sensitivity": hop_sensitivity,
        "reasons_have_evidence": evidence,
        "no_reach": no_reach,
        "largest_reach": max(reach_sizes),
        "reach_capped": sum(1 for n in reach_sizes if n >= REACH_CAP),
    }


def _table(header: list[str], rows: list[list], left: int = 1) -> str:
    """Plain-text table; the first `left` columns are left-aligned, the rest right-aligned."""
    grid = [[str(cell) for cell in row] for row in [header, *rows]]
    widths = [max(len(row[i]) for row in grid) for i in range(len(header))]

    def line(row: list[str]) -> str:
        cells = zip(row, widths, strict=True)
        return "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(cells))

    return "\n".join([line(grid[0]), line(["-" * w for w in widths]), *[line(row) for row in grid[1:]]])


def render(report: dict) -> str:
    """The report as readable tables."""
    total, hops = report["kep_count"], report["hops"]
    ev = report["reasons_have_evidence"]
    out = [f"Verdict report: {total} KEPs, graph data {report['data_hash'][:12]}, R1 gates at >= "
           f"{report['min_sigs_for_gate']} SIGs, search depth {hops} hops", "",
           "Verdicts",
           _table(["verdict", "KEPs", "share"],
                  [[v, report["verdicts"][v], f"{report['verdicts'][v] / total:.1%}"] for v in VERDICTS]), "",
           "Rules (all matches: KEPs the rule fired on; first match: each KEP under the first of R1, R2, R3, R4 "
           "that fired, else CLEAN)",
           _table(["rule", "name", "all matches", "first match"],
                  [[rule, RULES[rule][0], report["rules_all_match"][rule], report["rules_first_match"][rule]]
                   for rule in RULES], left=2), "",
           "Hop sensitivity (decide() re-run on facts limited to N hops)",
           _table(["hops", *VERDICTS, "KEPs with reach"],
                  [[h, *(level["verdicts"][v] for v in VERDICTS), level["keps_with_reach"]]
                   for h, level in sorted(report["hop_sensitivity"].items(), key=lambda kv: int(kv[0]))]), "",
           f"Evidence: {ev['with_evidence']} of {ev['total']} reasons carry the query name and the Cypher that "
           "produced them",
           f"Reach: {report['no_reach']} of {total} KEPs have no REPLACES/SEE_ALSO path to any KEP within {hops} hops; "
           f"the largest reach from one KEP is {report['largest_reach']} KEPs"]
    if report["reach_capped"]:
        out.append(f"WARNING: {report['reach_capped']} KEPs hit the LIMIT {REACH_CAP} of blast_radius, so their reach "
                   "is a lower bound.")
    return "\n".join(out)


def main() -> None:
    meta()  # raises GraphNotReady unless company_brain is loaded and ready
    report = summarize(preflight(kid) for kid in kep_ids())
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(render(report))
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
