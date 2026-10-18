"""Corpus report computed from the raw kep.yaml files (no FalkorDB). Writes data/report.json.

Also simulates the bailiff rules in pure Python over the parsed edges, to choose thresholds from real data.
"""

import json
from collections import Counter, defaultdict

from ingest.config import DATA_DIR, ENHANCEMENTS_DIR
from ingest.load import source_commit
from ingest.parse import ACTIVE, STALE, build_rows, parse_corpus

RECENT_WINDOW = 3  # minors below the corpus max that still count as "recent" (documented in README)
FIELDS = {
    "title": "title", "kep-number": "number_raw", "authors": "authors", "owning-sig": "owning_sig",
    "participating-sigs": "participating_sigs", "status": "status_raw", "creation-date": "creation_date",
    "reviewers": "reviewers", "approvers": "approvers", "prr-approvers": "prr_approvers_raw",
    "see-also": "see_also_raw", "replaces": "replaces_raw", "superseded-by": "superseded_by_raw",
    "stage": "stage", "latest-milestone": "latest_milestone",
}


def simulate(keps, rows, max_minor, hops=3):
    """Pure-Python twin of the bailiff rules over parsed edges, for threshold tuning only."""
    by_id = {k["id"]: k for k in keps}
    adj, see_also_out = defaultdict(set), defaultdict(set)
    for rel in ("SEE_ALSO", "REPLACES"):
        for e in rows["edges"][rel]:
            adj[e["src"]].add(e["dst"])
            adj[e["dst"]].add(e["src"])
            if rel == "SEE_ALSO":
                see_also_out[e["src"]].add(e["dst"])
    recent_people = set()
    for rel in ("AUTHORED", "REVIEWED", "APPROVED"):
        for e in rows["edges"][rel]:
            m = by_id[e["dst"]]["latest_minor"]
            if m is not None and max_minor is not None and m >= max_minor - RECENT_WINDOW:
                recent_people.add(e["src"])
    approvers = defaultdict(set)
    for e in rows["edges"]["APPROVED"]:
        approvers[e["dst"]].add(e["src"])

    def reach(start, nbrs):
        seen, frontier = {start}, {start}
        for _ in range(hops):
            frontier = {n for f in frontier for n in nbrs[f]} - seen
            seen |= frontier
        return seen - {start}

    results = {}
    for kid in by_id:
        affected = [o for o in reach(kid, adj) if by_id[o]["status"] in ACTIVE]
        sigs = {by_id[o]["owning_sig"] for o in affected if by_id[o]["owning_sig"]}
        stale = [o for o in reach(kid, see_also_out) if by_id[o]["status"] in STALE]
        no_active_approver = not (approvers[kid] & recent_people)
        results[kid] = (len(affected), len(sigs), len(stale), no_active_approver)

    def verdicts(min_sigs):
        out = Counter()
        for n_aff, n_sigs, n_stale, no_appr in results.values():
            if n_sigs >= min_sigs:
                out["HUMAN_GATE_REQUIRED"] += 1
            elif n_aff or n_stale or no_appr:
                out["AT_RISK"] += 1
            else:
                out["CLEAN"] += 1
        return dict(out)

    examples = defaultdict(list)
    for kid, (n_aff, n_sigs, n_stale, no_appr) in sorted(results.items(), key=lambda x: -x[1][0]):
        v = "HUMAN_GATE_REQUIRED" if n_sigs >= 2 else ("AT_RISK" if (n_aff or n_stale or no_appr) else "CLEAN")
        if len(examples[v]) < 5:
            examples[v].append({"id": kid, "affected": n_aff, "sigs": n_sigs, "stale_ancestors": n_stale,
                                "no_recent_approver": no_appr})
    return {
        "recent_people": len(recent_people),
        "keps_with_no_recent_approver": sum(1 for r in results.values() if r[3]),
        "keps_with_stale_ancestors": sum(1 for r in results.values() if r[2]),
        "verdicts_min_sigs_2": verdicts(2),
        "verdicts_min_sigs_3": verdicts(3),
        "examples_min_sigs_2": dict(examples),
    }


def main():
    records, failures = parse_corpus(ENHANCEMENTS_DIR)
    built = build_rows(records, source_commit())
    keps, rows, meta = built["rows"]["keps"], built["rows"], built["meta"]
    non_empty = {f: sum(1 for r in records if r.get(k)) for f, k in FIELDS.items()}
    status_map = Counter(f"{r['status_raw']!r} -> {r['status']}" for r in records if r["status"] != r["status_raw"])
    minors = sorted({k["latest_minor"] for k in keps if k["latest_minor"] is not None})
    report = {
        "source_commit": meta["source_commit"],
        "data_hash": meta["data_hash"],
        "kep_yaml_files": len(records) + len(failures),
        "parse_failures": failures,
        "keps": len(keps),
        "fields_non_empty": non_empty,
        "status_counts": dict(Counter(k["status"] for k in keps).most_common()),
        "status_normalizations": dict(status_map),
        "stale_keps": sorted(k["id"] for k in keps if k["status"] in STALE),
        "ref_stats": built["stats"],
        "edge_counts": {rel: len(v) for rel, v in rows["edges"].items()},
        "sigs": [s["name"] for s in rows["sigs"]],
        "people": meta["person_count"],
        "latest_minor_range": [minors[0], minors[-1]] if minors else None,
        "recent_threshold_minor": (meta["max_minor"] - RECENT_WINDOW) if meta["max_minor"] else None,
        "keps_without_latest_milestone": sum(1 for k in keps if k["latest_minor"] is None),
        "simulation": simulate(keps, rows, meta["max_minor"]),
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("stale_keps", "sigs")}, indent=2))
    print(f"\nWrote {DATA_DIR / 'report.json'}")


if __name__ == "__main__":
    main()
