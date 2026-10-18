"""The bailiff: a deterministic pre-flight verdict for "supersede KEP-X", computed only from graph results.

Rules (documented in README, thresholds chosen from data/report.json):
  R1 HUMAN_GATE_REQUIRED  active KEPs within 3 REPLACES/SEE_ALSO hops are owned by >= 2 distinct SIGs
  R2 AT_RISK              active KEPs within 3 hops exist, all owned by one SIG
  R3 AT_RISK              the target references stale KEPs through outgoing SEE_ALSO within 3 hops
  R4 AT_RISK              none of the target's approvers is recently active (or none are listed)
  CLEAN                   none of R1-R4 fired: no path to any active KEP, reported as "no path found"
No LLM is involved in any of this.

Every reason names its rule, the query template whose rows it was decided on, and the ids it matched. preflight()
adds the exact Cypher text and params of that query run (attach_evidence), so each verdict traces to a graph query.
"""

from __future__ import annotations

import copy
import hashlib
import json

from agent import queries as q
from agent.db import meta, run_ro

MIN_SIGS_FOR_GATE = 2
HOPS = 3  # depth of the blast_radius and is_still_valid queries (the *1..3 in agent/templates.py)
ACTIONS = {"supersede"}

# rule -> (name, query template whose rows the rule is decided on). CLEAN is "no path found" from blast_radius.
RULES = {
    "R1": ("multi-SIG reach", "blast_radius"),
    "R2": ("single-SIG reach", "blast_radius"),
    "R3": ("stale reference", "is_still_valid"),
    "R4": ("no recently active approver", "recent_approvers"),
    "CLEAN": ("no path found", "blast_radius"),
}


def _within(hops: int) -> str:
    return f"within {hops} hop" + ("" if hops == 1 else "s")


def _reason(rule: str, effect: str, detail: str, evidence: list[str], matched_ids: list[str]) -> dict:
    name, query = RULES[rule]
    return {"rule": rule, "rule_name": name, "effect": effect, "detail": detail, "evidence": list(evidence),
            "matched_ids": list(matched_ids), "query": query}


def decide(facts: dict) -> dict:
    """Pure rule evaluation over gathered facts. Returns verdict + reasons; every reason carries its evidence.

    A reason holds rule, rule_name, effect, detail, evidence, matched_ids and the name of the query it was decided
    on; attach_evidence() adds that query's Cypher and params. facts["hops"] (optional, default HOPS) only words
    the reasons: hop_limited_facts() sets it when it narrows the facts to fewer hops.
    """
    affected, sigs = facts["affected"], sorted(facts["affected_sigs"])
    stale = facts["stale_ancestors"]
    within = _within(facts.get("hops", HOPS))
    reasons = []
    if len(sigs) >= MIN_SIGS_FOR_GATE:
        reasons.append(_reason("R1", "HUMAN_GATE_REQUIRED",
                               f"{len(affected)} active KEPs across {len(sigs)} SIGs are {within}",
                               sigs, [a["id"] for a in affected]))
    elif affected:
        owner = sigs[0] if sigs else "a KEP with no owning SIG listed"
        ids = [a["id"] for a in affected]
        reasons.append(_reason("R2", "AT_RISK", f"{len(affected)} active KEPs {within}, all owned by {owner}",
                               ids, ids))
    if stale:
        reasons.append(_reason("R3", "AT_RISK", f"references {len(stale)} stale KEP(s) via see-also {within}",
                               [f"KEP-{s['id']} ({s['status']})" for s in stale], [s["id"] for s in stale]))
    if not facts["recent_approvers"]:
        detail = ("no approvers listed in kep.yaml" if not facts["approvers"] else
                  f"none of its approvers appears on a KEP with latest-milestone >= v1.{facts['recent_min_minor']}")
        reasons.append(_reason("R4", "AT_RISK", detail, ["@" + a for a in facts["approvers"]], facts["approvers"]))
    if any(r["effect"] == "HUMAN_GATE_REQUIRED" for r in reasons):
        verdict = "HUMAN_GATE_REQUIRED"
    elif reasons:
        verdict = "AT_RISK"
    else:
        verdict = "CLEAN"
        reasons.append(_reason("CLEAN", "CLEAN",
                               f"no path found from KEP-{facts['kep_id']} to any active KEP {within}; "
                               "no stale KEP is referenced via see-also; a recently active approver is listed",
                               [], []))
    return {"verdict": verdict, "reasons": reasons}


def attach_evidence(reasons: list[dict], runs: list[dict]) -> list[dict]:
    """Copy each reason with the Cypher text and params of the query run it was decided on.

    `runs` is preflight's `queries` list (name, graph, cypher, params, rows, ms): the Cypher is the text that was
    executed, never retyped. A reason whose query has no run is an error, not a silent gap.
    """
    run_by_name: dict[str, dict] = {}
    for run in runs:
        run_by_name.setdefault(run["name"], run)
    out = []
    for reason in reasons:
        run = run_by_name.get(reason["query"])
        if run is None:
            raise LookupError(f"Reason {reason['rule']} cites query {reason['query']!r}, but no such query was run.")
        out.append(reason | {"cypher": run["cypher"], "params": copy.deepcopy(run["params"])})
    return out


def hop_limited_facts(result: dict, hops: int) -> dict:
    """Facts for decide() from a full preflight() result, keeping only what lies within `hops` hops of the target.

    Valid for any hops up to the depth the result was computed at (rules["hops"], 3): blast_radius returns every
    reachable KEP once with its true minimum hop distance, and a stale ancestor's path has one node more than it has
    SEE_ALSO hops, so filtering by distance gives what a shallower query would have returned. Approvers and the
    recency threshold are the same at every depth. Asking for more hops than were computed raises instead of guessing.
    """
    computed = result["rules"]["hops"]
    if not 0 <= hops <= computed:
        raise ValueError(f"hops must be between 0 and {computed} (the depth preflight computed), got {hops!r}")
    affected = [a for a in result["affected_keps"] if a["hops"] <= hops]
    return {
        "kep_id": result["kep"]["id"],
        "hops": hops,
        "affected": affected,
        "affected_sigs": {a["owning_sig"] for a in affected if a["owning_sig"]},
        "stale_ancestors": [s for s in result["stale_ancestors"] if len(s["path"]) - 1 <= hops],
        "approvers": result["approvers"],
        "recent_approvers": result["recent_approvers"],
        "recent_min_minor": result["rules"]["recent_min_minor"],
    }


def _notice(kep: dict, verdict: dict, facts: dict, reviewers: list[dict], data_hash: str, action: str) -> dict:
    key = json.dumps([kep["id"], action, verdict["verdict"], data_hash, sorted(a["id"] for a in facts["affected"])])
    notice_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    lines = [
        f"**STAGED — NOT SENT.** Impact notice: proposal to {action} KEP-{kep['id']} ({kep['title']})",
        "",
        f"Verdict: **{verdict['verdict']}** (deterministic rules R1–R4, graph data {data_hash[:12]})",
        "",
        "Why:",
        *[f"- [{r['rule']}] {r['detail']}" for r in verdict["reasons"]],
    ]
    if facts["affected"]:
        lines += ["", f"Affected active KEPs ({len(facts['affected'])}):"]
        lines += [f"- KEP-{a['id']} {a['title']} ({a['status']}, {a['owning_sig']}, {a['hops']} hop(s)) "
                  f"{a['source_url']}" for a in facts["affected"][:25]]
        if len(facts["affected"]) > 25:
            lines.append(f"- … and {len(facts['affected']) - 25} more (see blast radius)")
    if facts["affected_sigs"]:
        lines += ["", "SIGs to notify: " + ", ".join(sorted(facts["affected_sigs"]))]
    if reviewers:
        lines += ["", "Suggested reviewers (who_knows): " + ", ".join("@" + r["handle"] for r in reviewers[:5])]
    lines += ["", f"Source: {kep['source_url']}"]
    if kep.get("issue_url"):
        lines.append(f"Tracking issue: {kep['issue_url']}")
    return {
        "id": notice_id,
        "kep_id": kep["id"],
        "action": action,
        "verdict": verdict["verdict"],
        "data_hash": data_hash,
        "recipients": {"sigs": sorted(facts["affected_sigs"]), "people": [r["handle"] for r in reviewers[:5]]},
        "markdown": "\n".join(lines),
        "posted": False,
    }


def preflight(kep_id: str, action: str = "supersede") -> dict:
    if action not in ACTIONS:
        raise ValueError(f"Unsupported action {action!r}; supported: {sorted(ACTIONS)}")
    detail = q.kep_detail(kep_id)
    kep = detail["result"]["kep"]
    kid = kep["id"]
    blast = q.blast_radius(kid)
    validity = q.is_still_valid(kid)
    threshold = q.recent_min_minor()
    recent_run = run_ro("recent_approvers", {"kep_id": kid, "recent_min_minor": threshold})
    reviewers = q.who_knows(kep_id=kid, limit=8)
    precedents = q.prior_precedents(kid)
    m = meta()

    affected = [a for a in blast["result"]["reachable"] if a["active"]]
    facts = {
        "kep_id": kid,
        "hops": HOPS,
        "affected": affected,
        "affected_sigs": {a["owning_sig"] for a in affected if a["owning_sig"]},
        "stale_ancestors": validity["result"]["stale_ancestors"],
        "approvers": detail["result"]["people"]["APPROVED"],
        "recent_approvers": [r["handle"] for r in recent_run["rows"] if r["recent_keps"]],
        "recent_min_minor": threshold,
    }
    queries_run, seen = [], set()  # every distinct query that ran, once each (tools share resolve/detail lookups)
    for run in (detail["queries"] + blast["queries"] + validity["queries"] + [q._q(recent_run)]
                + reviewers["queries"] + precedents["queries"]):
        key = (run["name"], json.dumps(run["params"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            queries_run.append(run)
    verdict = decide(facts)
    reasons = attach_evidence(verdict["reasons"], queries_run)
    suggested = [p for p in reviewers["result"]["people"]]
    notice = _notice(kep, verdict, facts, suggested, m["data_hash"], action)
    return {
        "tool": "preflight",
        "action": action,
        "kep": kep,
        "verdict": verdict["verdict"],
        "reasons": reasons,
        "affected_keps": affected,
        "affected_sigs": sorted(facts["affected_sigs"]),
        "inactive_reachable": [a for a in blast["result"]["reachable"] if not a["active"]],
        "stale_ancestors": facts["stale_ancestors"],
        "approvers": facts["approvers"],
        "recent_approvers": facts["recent_approvers"],
        "suggested_reviewers": suggested,
        "who_knows_formula": reviewers["result"]["formula"],
        "no_path_found": blast["result"]["no_path_found"],
        "path": [e for a in affected for e in a["path_edges"]],
        "source_links": detail["source_links"] + [{"label": f"KEP-{a['id']}", "url": a["source_url"]}
                                                  for a in affected[:30]],
        "queries": queries_run,
        "prior_precedents": precedents["result"],
        "staged_notice": notice,
        "rules": {"min_sigs_for_gate": MIN_SIGS_FOR_GATE, "hops": HOPS, "recent_min_minor": threshold},
        "data_hash": m["data_hash"],
    }
