"""Query-layer tools.

Each returns {tool, result, cypher, params, source_links, path, queries}, built from graph rows only.
"""

from __future__ import annotations

from agent.db import meta, run_ro
from ingest.parse import ACTIVE, STALE

RECENT_WINDOW = 3  # a KEP is "recent" if latest-milestone minor >= corpus max minor - 3 (max is computed at ingest)
WHO_KNOWS_FORMULA = (
    "score(person) = sum over each (role, KEP) pair in scope, role in {AUTHORED, REVIEWED, APPROVED}, of "
    "w(KEP), where w = 2 if the KEP's latest-milestone minor >= {threshold} (corpus max v1.{max} minus "
    f"{RECENT_WINDOW}) else 1"
)


class NotFound(LookupError):
    pass


class Ambiguous(LookupError):
    pass


def _clean(rows: list) -> list:
    """Drop OPTIONAL MATCH placeholders (lists made entirely of nulls)."""
    return [r for r in rows if r is not None and not (isinstance(r, list) and all(v is None for v in r[:2]))]


def _q(run: dict) -> dict:
    return {k: run[k] for k in ("name", "graph", "cypher", "params", "ms")} | {"rows": len(run["rows"])}


def kep_links(kep: dict) -> list[dict]:
    links = [{"label": f"KEP-{kep['id']} kep.yaml", "url": kep.get("source_url")}]
    if kep.get("issue_url"):
        links.append({"label": f"Tracking issue #{kep.get('number') or kep['id']}", "url": kep["issue_url"]})
    return [x for x in links if x["url"]]


def recent_min_minor() -> int:
    m = meta()
    if m.get("max_minor") is None:
        raise RuntimeError("Meta.max_minor missing; re-run `make ingest`.")
    return int(m["max_minor"]) - RECENT_WINDOW


def resolve_kep(kep_id: str) -> tuple[str, dict]:
    """Normalize 'KEP-1287' / '1287' / '<sig>/<folder>' to a unique KEP id; fail loudly if missing or ambiguous."""
    raw = str(kep_id).strip()
    key = raw[4:] if raw.upper().startswith("KEP-") else raw
    key = key.lstrip("#").strip()
    if key.isdigit():
        key = str(int(key))
    run = run_ro("resolve_kep", {"kep_id": key})
    if not run["rows"]:
        raise NotFound(f"No KEP with id or number '{raw}' in the graph.")
    if len(run["rows"]) > 1:
        options = ", ".join(r["id"] for r in run["rows"])
        raise Ambiguous(f"KEP number '{raw}' is claimed by {len(run['rows'])} KEP folders: {options}. "
                        "Use one of these ids.")
    return run["rows"][0]["id"], _q(run)


def require_sig(sig: str) -> dict:
    run = run_ro("sig_exists", {"sig": sig})
    if not run["rows"]:
        raise NotFound(f"No SIG named '{sig}' in the graph.")
    return _q(run)


def _result(tool: str, result, runs: list[dict], source_links: list[dict], path: list | None = None) -> dict:
    return {
        "tool": tool,
        "result": result,
        "cypher": runs[-1]["cypher"],
        "params": runs[-1]["params"],
        "queries": runs,
        "source_links": source_links,
        "path": path or [],
    }


def kep_detail(kep_id: str) -> dict:
    kid, rq = resolve_kep(kep_id)
    run = run_ro("kep_detail", {"kep_id": kid})
    row = run["rows"][0]
    kep = dict(row["kep"])
    people: dict[str, list[str]] = {"AUTHORED": [], "REVIEWED": [], "APPROVED": []}
    for role, handle in _clean(row["people"]):
        people[role].append(handle)
    neighbors = []
    for start, rel, _end, mid, title, status in _clean(row["neighbors"]):
        neighbors.append({"rel": rel, "direction": "out" if start == kid else "in", "id": mid, "title": title,
                          "status": status})
    result = {
        "kep": kep,
        "owning_sigs": [s for s in row["owning_sigs"] if s],
        "participating_sigs": [s for s in row["participating_sigs"] if s],
        "people": {k: sorted(v) for k, v in people.items()},
        "neighbors": sorted(neighbors, key=lambda n: (n["rel"], n["id"])),
    }
    path = [[kid, "OWNED_BY", s] for s in result["owning_sigs"]]
    path += [[kid, n["rel"], n["id"]] if n["direction"] == "out" else [n["id"], n["rel"], kid] for n in neighbors]
    return _result("kep_detail", result, [rq, _q(run)], kep_links(kep), path)


def blast_radius(kep_id: str) -> dict:
    kid, rq = resolve_kep(kep_id)
    drun = run_ro("kep_detail", {"kep_id": kid})
    detail = drun["rows"][0]["kep"]
    run = run_ro("blast_radius", {"kep_id": kid})
    affected = []
    for r in run["rows"]:
        affected.append(r | {"active": r["status"] in ACTIVE})
    result = {
        "kep_id": kid,
        "hops": 3,
        "reachable": affected,
        "reachable_count": len(affected),
        "at_limit": len(affected) >= 100,  # True means the LIMIT 100 cap was reached; results may be cut off
        "no_path_found": not affected,
        "message": None if affected else f"No path found: KEP-{kid} has no REPLACES/SEE_ALSO link within 3 hops.",
    }
    links = kep_links(detail) + [{"label": f"KEP-{a['id']}", "url": a["source_url"]} for a in affected[:20]]
    path = [e for a in affected for e in a["path_edges"]]
    return _result("blast_radius", result, [rq, _q(drun), _q(run)], links, path)


def is_still_valid(kep_id: str) -> dict:
    kid, rq = resolve_kep(kep_id)
    drun = run_ro("kep_detail", {"kep_id": kid})
    detail = drun["rows"][0]["kep"]
    run = run_ro("is_still_valid", {"kep_id": kid, "stale_statuses": list(STALE)})
    row = run["rows"][0]
    succ = [{"id": i, "title": t, "status": s, "hops": h} for i, t, s, h in _clean(row["successors"])]
    pred = [{"id": i, "title": t, "status": s, "hops": h} for i, t, s, h in _clean(row["predecessors"])]
    stale = [{"id": i, "title": t, "status": s, "path": p} for i, t, s, p in _clean(row["stale_ancestors"])]
    status = row["status"]
    if succ:
        verdict = "superseded"
    elif status in STALE:
        verdict = f"not valid (status: {status})"
    elif status in ACTIVE:
        verdict = "valid" if not stale else "valid, but depends on stale KEPs"
    else:
        verdict = f"unknown (raw status: {row['status_raw']!r})"
    result = {"kep_id": kid, "title": row["title"], "status": status, "status_raw": row["status_raw"],
              "has_successor": bool(succ), "successors": succ, "predecessors": pred, "stale_ancestors": stale,
              "validity": verdict}
    path = [[s["id"], "REPLACES", kid] for s in succ if s["hops"] == 1] + [[kid, "REPLACES", p["id"]] for p in pred
                                                                         if p["hops"] == 1]
    for s in stale:
        path += [[a, "SEE_ALSO", b] for a, b in zip(s["path"], s["path"][1:], strict=False)]
    return _result("is_still_valid", result, [rq, _q(drun), _q(run)], kep_links(detail), path)


def who_knows(sig: str | None = None, kep_id: str | None = None, limit: int = 10) -> dict:
    if not sig and not kep_id:
        raise ValueError("who_knows needs a sig or a kep_id")
    threshold = recent_min_minor()
    runs, links = [], []
    if kep_id:
        kid, rq = resolve_kep(kep_id)
        runs.append(rq)
        run = run_ro("who_knows_kep", {"kep_id": kid, "recent_min_minor": threshold, "limit": limit})
        scope = f"KEP-{kid} and its 2-hop REPLACES/SEE_ALSO neighborhood"
        drun = run_ro("kep_detail", {"kep_id": kid})
        runs.append(_q(drun))
        links = kep_links(drun["rows"][0]["kep"])
    else:
        sig = sig.strip().lower()
        sig = sig if sig.startswith(("sig-", "wg-", "committee-")) else f"sig-{sig}"
        runs.append(require_sig(sig))
        run = run_ro("who_knows_sig", {"sig": sig, "recent_min_minor": threshold, "limit": limit})
        scope = f"KEPs owned by or involving {sig}"
        links = [{"label": f"{sig} KEPs", "url": f"https://github.com/kubernetes/enhancements/tree/master/keps/{sig}"}]
    runs.append(_q(run))
    m = meta()
    result = {
        "scope": scope,
        "formula": WHO_KNOWS_FORMULA.replace("{threshold}", str(threshold)).replace("{max}", str(m["max_minor"])),
        "people": run["rows"],
        "no_result": not run["rows"],
    }
    path = [[p["handle"], "CONTRIBUTED_TO", k] for p in run["rows"][:5] for k in p["sample_keps"][:2]]
    return _result("who_knows", result, runs, links, path)


def why(kep_id: str) -> dict:
    kid, rq = resolve_kep(kep_id)
    run = run_ro("why", {"kep_id": kid})
    r = run["rows"][0]
    lineage = [{"from": a, "rel": rel, "to": b, "other_title": t} for a, rel, b, t in _clean(r["lineage"])]
    chain = [
        {"step": "decision", "value": f"KEP-{kid}: {r['title']}", "source": r["source_url"]},
        {"step": "status", "value": f"{r['status']} (stage {r['stage']}, latest milestone {r['latest_milestone']})",
         "source": r["source_url"]},
        {"step": "created", "value": r["creation_date"], "source": r["source_url"]},
        {"step": "owned by", "value": r["owning_sig"], "source": r["source_url"]},
        {"step": "approved by", "value": ", ".join("@" + a for a in r["approvers"]) or "(none listed)",
         "source": r["source_url"]},
        {"step": "authored by", "value": ", ".join("@" + a for a in r["authors"]) or "(none listed)",
         "source": r["source_url"]},
        {"step": "tracked in", "value": r["issue_url"] or "(no valid KEP number, no issue)", "source": r["issue_url"]},
    ]
    result = {"kep_id": kid, "chain": chain, "lineage": lineage, "unresolved_refs": r["unresolved_refs"] or [],
              "approvers": sorted(r["approvers"]), "authors": sorted(r["authors"]),
              "note": "Only fields present in kep.yaml are shown; no free-text rationale is inferred."}
    links = kep_links({"id": kid, "source_url": r["source_url"], "issue_url": r["issue_url"], "number": None})
    path = [[kid, "DEFINED_IN", "kep.yaml"], [kid, "TRACKED_IN", f"issue #{kid}" if r["issue_url"] else "none"],
            [kid, "OWNED_BY", r["owning_sig"]]] + [[x["from"], x["rel"], x["to"]] for x in lineage]
    return _result("why", result, [rq, _q(run)], links, path)


def referrers(kep_id: str) -> dict:
    kid, rq = resolve_kep(kep_id)
    run = run_ro("referrers", {"kep_id": kid})
    result = {"kep_id": kid, "referrers": run["rows"], "count": len(run["rows"]),
              "message": None if run["rows"] else f"No KEP lists KEP-{kid} in see-also."}
    links = [{"label": f"KEP-{r['id']}", "url": r["source_url"]} for r in run["rows"]]
    return _result("referrers", result, [rq, _q(run)], links, [[r["id"], "SEE_ALSO", kid] for r in run["rows"]])


def sig_overview(sig: str) -> dict:
    sig = sig.strip().lower()
    sig = sig if sig.startswith(("sig-", "wg-", "committee-")) else f"sig-{sig}"
    exists = require_sig(sig)
    run = run_ro("sig_overview", {"sig": sig, "active_statuses": list(ACTIVE)})
    row = run["rows"][0] if run["rows"] else {"sig": sig, "owned": 0, "statuses": [], "active": 0}
    links = [{"label": f"{sig} KEPs", "url": f"https://github.com/kubernetes/enhancements/tree/master/keps/{sig}"}]
    return _result("sig_overview", row, [exists, _q(run)], links, [])


def search_keps(q: str, limit: int = 20) -> dict:
    q = str(q).strip()
    q = q[4:] if q.upper().startswith("KEP-") else q
    run = run_ro("search_keps", {"q": q, "limit": max(1, min(int(limit), 100))})
    return _result("search_keps", run["rows"], [_q(run)], [], [])


def list_sigs() -> dict:
    run = run_ro("list_sigs", {})
    return _result("list_sigs", run["rows"], [_q(run)], [], [])


def central_keps(limit: int = 10) -> dict:
    run = run_ro("central_keps", {"limit": max(1, min(int(limit), 100))})
    return _result("central_keps", run["rows"], [_q(run)], [], [])


def prior_precedents(kep_id: str) -> dict:
    run = run_ro("prior_precedents", {"kep_id": kep_id})
    return _result("prior_precedents", run["rows"], [_q(run)], [], [])
