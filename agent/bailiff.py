"""The bailiff: a deterministic pre-flight verdict for "supersede KEP-X", computed only from graph results.

Rules (documented in README, thresholds chosen from data/report.json):
  R1 HUMAN_GATE_REQUIRED  active KEPs within 3 REPLACES/SEE_ALSO hops are owned by >= 2 distinct SIGs
  R2 AT_RISK              active KEPs within 3 hops exist, all owned by one SIG
  R3 AT_RISK              the target depends (SEE_ALSO, <= 3 hops, outgoing) on stale KEPs
  R4 AT_RISK              none of the target's approvers is recently active (or none are listed)
  CLEAN                   none of R1-R4 fired: no path to any active KEP, reported as "no path found"
No LLM is involved in any of this.
"""

from __future__ import annotations

import hashlib
import json

from agent import queries as q
from agent.db import meta, run_ro

MIN_SIGS_FOR_GATE = 2
ACTIONS = {"supersede"}


def decide(facts: dict) -> dict:
    """Pure rule evaluation over gathered facts. Returns verdict + reasons; every reason carries its evidence."""
    affected, sigs = facts["affected"], sorted(facts["affected_sigs"])
    reasons = []
    if len(sigs) >= MIN_SIGS_FOR_GATE:
        reasons.append({"rule": "R1", "effect": "HUMAN_GATE_REQUIRED",
                        "detail": f"{len(affected)} active KEPs across {len(sigs)} SIGs are within 3 hops",
                        "evidence": sigs})
    elif affected:
        reasons.append({"rule": "R2", "effect": "AT_RISK",
                        "detail": f"{len(affected)} active KEPs within 3 hops, all owned by {sigs[0]}",
                        "evidence": [a["id"] for a in affected]})
    if facts["stale_ancestors"]:
        reasons.append({"rule": "R3", "effect": "AT_RISK",
                        "detail": f"depends via SEE_ALSO on {len(facts['stale_ancestors'])} stale KEP(s)",
                        "evidence": [f"KEP-{s['id']} ({s['status']})" for s in facts["stale_ancestors"]]})
    if not facts["recent_approvers"]:
        detail = ("no approvers listed in kep.yaml" if not facts["approvers"] else
                  "none of its approvers appears on a KEP with latest-milestone >= v1." f"{facts['recent_min_minor']}")
        reasons.append({"rule": "R4", "effect": "AT_RISK", "detail": detail,
                        "evidence": ["@" + a for a in facts["approvers"]]})
    if any(r["effect"] == "HUMAN_GATE_REQUIRED" for r in reasons):
        verdict = "HUMAN_GATE_REQUIRED"
    elif reasons:
        verdict = "AT_RISK"
    else:
        verdict = "CLEAN"
        reasons.append({"rule": "CLEAN", "effect": "CLEAN",
                        "detail": f"no path found from KEP-{facts['kep_id']} to any active KEP within 3 hops; "
                                  "no stale dependencies; a recently active approver is listed",
                        "evidence": []})
    return {"verdict": verdict, "reasons": reasons}


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
        "affected": affected,
        "affected_sigs": {a["owning_sig"] for a in affected if a["owning_sig"]},
        "stale_ancestors": validity["result"]["stale_ancestors"],
        "approvers": detail["result"]["people"]["APPROVED"],
        "recent_approvers": [r["handle"] for r in recent_run["rows"] if r["recent_keps"]],
        "recent_min_minor": threshold,
    }
    verdict = decide(facts)
    suggested = [p for p in reviewers["result"]["people"]]
    notice = _notice(kep, verdict, facts, suggested, m["data_hash"], action)
    queries_run = (detail["queries"] + blast["queries"][1:] + validity["queries"][1:]
                   + [q._q(recent_run)] + reviewers["queries"][1:] + precedents["queries"])
    return {
        "tool": "preflight",
        "action": action,
        "kep": kep,
        "verdict": verdict["verdict"],
        "reasons": verdict["reasons"],
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
        "rules": {"min_sigs_for_gate": MIN_SIGS_FOR_GATE, "hops": 3, "recent_min_minor": threshold},
        "data_hash": m["data_hash"],
    }
