"""Bailiff verdict branches on real KEPs (live graph) plus pure-rule checks on facts taken from those KEPs."""

import ast
import copy
import json
from pathlib import Path

import pytest

from agent import bailiff
from agent.bailiff import RULES, attach_evidence, decide, hop_limited_facts, preflight
from agent.templates import TEMPLATES
from scripts.verdict_report import kep_ids, render, summarize

SEVERITY = {"CLEAN": 0, "AT_RISK": 1, "HUMAN_GATE_REQUIRED": 2}
REASON_KEYS = {"rule", "rule_name", "effect", "detail", "evidence", "matched_ids", "query", "cypher", "params"}


def facts(**kw):
    base = {"kep_id": "x", "affected": [], "affected_sigs": set(), "stale_ancestors": [], "approvers": ["a"],
            "recent_approvers": ["a"], "recent_min_minor": 35}
    return base | kw


# Facts that fire R1 (two SIGs), R3 (one stale KEP) and R4 (the only approver is not recently active) at once.
ALL_RULES = {"affected": [{"id": "1"}, {"id": "2"}], "affected_sigs": {"sig-a", "sig-b"},
             "stale_ancestors": [{"id": "9", "status": "replaced"}], "approvers": ["bob"], "recent_approvers": []}


def reason(result, rule):
    return next(x for x in result["reasons"] if x["rule"] == rule)


def test_decide_clean_only_when_nothing_fires():
    out = decide(facts())
    assert out["verdict"] == "CLEAN" and "no path found" in out["reasons"][0]["detail"]


def test_decide_gate_beats_at_risk():
    aff = [{"id": "1"}, {"id": "2"}]
    out = decide(facts(affected=aff, affected_sigs={"sig-a", "sig-b"}, recent_approvers=[]))
    assert out["verdict"] == "HUMAN_GATE_REQUIRED"
    assert [r["rule"] for r in out["reasons"]] == ["R1", "R4"]


def test_decide_no_approvers_is_at_risk():
    out = decide(facts(approvers=[], recent_approvers=[]))
    assert out["verdict"] == "AT_RISK" and "no approvers listed" in out["reasons"][0]["detail"]


def test_decide_reasons_name_their_rule_query_and_matched_ids():
    f = facts(**ALL_RULES)
    before = copy.deepcopy(f)
    out = decide(f)
    assert f == before  # decide() is pure
    assert {x["rule"]: (x["rule_name"], x["query"], x["matched_ids"]) for x in out["reasons"]} == {
        "R1": ("multi-SIG reach", "blast_radius", ["1", "2"]),
        "R3": ("stale reference", "is_still_valid", ["9"]),
        "R4": ("no recently active approver", "recent_approvers", ["bob"]),
    }
    r2 = decide(facts(affected=[{"id": "1"}], affected_sigs={"sig-a"}))["reasons"][0]
    assert (r2["rule"], r2["rule_name"], r2["query"], r2["matched_ids"]) == (
        "R2", "single-SIG reach", "blast_radius", ["1"])
    clean = decide(facts())["reasons"][0]
    assert (clean["rule"], clean["rule_name"], clean["query"], clean["matched_ids"]) == (
        "CLEAN", "no path found", "blast_radius", [])


def test_reason_wording_says_references_and_never_depends():
    r3 = reason(decide(facts(**ALL_RULES)), "R3")
    assert r3["detail"] == "references 1 stale KEP(s) via see-also within 3 hops"
    variants = [facts(**ALL_RULES), facts(affected=[{"id": "1"}], affected_sigs={"sig-a"}, approvers=[],
                                         recent_approvers=[]), facts()]
    texts = [t for f in variants for x in decide(f)["reasons"] for t in (x["detail"], x["rule_name"], *x["evidence"])]
    assert len(texts) > 8
    assert not [t for t in texts if "depend" in t.lower()]


def test_bailiff_source_has_no_string_saying_depends():
    tree = ast.parse(Path(bailiff.__file__).read_text(encoding="utf-8"))
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert strings and not [s for s in strings if "depend" in s.lower()]


def test_decide_words_reasons_with_the_hop_limit_in_the_facts():
    one = decide(facts(**ALL_RULES, hops=1))["reasons"]
    assert one[0]["detail"] == "2 active KEPs across 2 SIGs are within 1 hop"
    assert one[1]["detail"] == "references 1 stale KEP(s) via see-also within 1 hop"
    assert decide(facts(**ALL_RULES, hops=2))["reasons"][0]["detail"].endswith("within 2 hops")
    assert "within 3 hops" in decide(facts(**ALL_RULES))["reasons"][0]["detail"]  # default: the full depth


def test_attach_evidence_takes_cypher_and_params_from_the_runs():
    runs = [{"name": "blast_radius", "cypher": "MATCH (a)", "params": {"kep_id": "x"}},
            {"name": "is_still_valid", "cypher": "MATCH (b)",
             "params": {"kep_id": "x", "stale_statuses": ["replaced"]}},
            {"name": "recent_approvers", "cypher": "MATCH (c)", "params": {"kep_id": "x", "recent_min_minor": 35}}]
    reasons = decide(facts(**ALL_RULES))["reasons"]
    out = attach_evidence(reasons, runs)
    assert [(x["rule"], x["cypher"], x["params"]) for x in out] == [
        ("R1", "MATCH (a)", {"kep_id": "x"}),
        ("R3", "MATCH (b)", {"kep_id": "x", "stale_statuses": ["replaced"]}),
        ("R4", "MATCH (c)", {"kep_id": "x", "recent_min_minor": 35}),
    ]
    assert all(set(x) == REASON_KEYS - {"cypher", "params"} for x in reasons)  # the input reasons are untouched
    runs[1]["params"]["stale_statuses"].append("other")  # the reason holds a copy, not the run record's own dict
    assert out[1]["params"]["stale_statuses"] == ["replaced"]
    with pytest.raises(LookupError, match="blast_radius"):
        attach_evidence(reasons, runs[1:])  # a reason whose query never ran is an error, not a silent gap


def hand_built_result():
    """A preflight()-shaped result computed at 3 hops: reach at 1, 2 and 3 hops over three SIGs, two stale KEPs."""
    def kep(kep_id, hops, sig):
        return {"id": kep_id, "title": f"t{kep_id}", "status": "implementable", "hops": hops, "owning_sig": sig}

    return {
        "kep": {"id": "100"},
        "rules": {"min_sigs_for_gate": 2, "hops": 3, "recent_min_minor": 35},
        "affected_keps": [kep("1", 1, "sig-a"), kep("2", 2, "sig-a"), kep("3", 2, "sig-b"), kep("4", 3, "sig-c"),
                          kep("5", 3, None)],  # KEP 5 lists no owning SIG, so it adds no SIG
        "stale_ancestors": [{"id": "8", "title": "t8", "status": "replaced", "path": ["100", "8"]},
                            {"id": "9", "title": "t9", "status": "withdrawn", "path": ["100", "7", "6", "9"]}],
        "approvers": ["x"],
        "recent_approvers": [],
    }


def test_hop_limited_facts_keeps_only_what_lies_within_the_limit():
    result = hand_built_result()
    before = copy.deepcopy(result)
    f0, f1, f2, f3 = (hop_limited_facts(result, h) for h in (0, 1, 2, 3))
    assert result == before  # the full result is not modified
    assert [(f["hops"], [a["id"] for a in f["affected"]], f["affected_sigs"]) for f in (f0, f1, f2, f3)] == [
        (0, [], set()),
        (1, ["1"], {"sig-a"}),
        (2, ["1", "2", "3"], {"sig-a", "sig-b"}),
        (3, ["1", "2", "3", "4", "5"], {"sig-a", "sig-b", "sig-c"}),
    ]
    # a stale ancestor counts at its SEE_ALSO hop distance: path length minus one (KEP 8 is 1 hop away, KEP 9 is 3)
    assert [[s["id"] for s in f["stale_ancestors"]] for f in (f0, f1, f2, f3)] == [[], ["8"], ["8"], ["8", "9"]]
    for f in (f0, f1, f2, f3):
        assert f["kep_id"] == "100" and f["approvers"] == ["x"] and f["recent_approvers"] == []
        assert f["recent_min_minor"] == 35


def test_hop_limited_facts_feed_decide_a_shallower_world():
    result = hand_built_result()
    runs = [decide(hop_limited_facts(result, h)) for h in (0, 1, 2, 3)]
    assert [out["verdict"] for out in runs] == ["AT_RISK", "AT_RISK", "HUMAN_GATE_REQUIRED", "HUMAN_GATE_REQUIRED"]
    assert [[x["rule"] for x in out["reasons"]] for out in runs] == [
        ["R4"], ["R2", "R3", "R4"], ["R1", "R3", "R4"], ["R1", "R3", "R4"]]
    assert runs[1]["reasons"][0]["detail"] == "1 active KEPs within 1 hop, all owned by sig-a"
    assert runs[2]["reasons"][0]["matched_ids"] == ["1", "2", "3"]


@pytest.mark.parametrize("hops", [-1, 4])
def test_hop_limited_facts_refuses_a_depth_that_was_not_computed(hops):
    with pytest.raises(ValueError, match="between 0 and 3"):
        hop_limited_facts(hand_built_result(), hops)


def fake_result(kep_id, affected=(), inactive=(), stale=(), approvers=("a",), recent=("a",)):
    """A preflight()-shaped result built from hand-made facts (no graph): everything summarize() reads."""
    active = [{"id": i, "hops": h, "owning_sig": sig} for i, h, sig in affected]
    f = facts(kep_id=kep_id, affected=active, affected_sigs={a["owning_sig"] for a in active},
              stale_ancestors=[{"id": i, "status": "replaced", "path": path} for i, path in stale],
              approvers=list(approvers), recent_approvers=list(recent))
    out = decide(f)
    runs = [{"name": n, "cypher": f"MATCH ({n})", "params": {"kep_id": kep_id}}
            for n in ("blast_radius", "is_still_valid", "recent_approvers")]
    return {"kep": {"id": kep_id}, "verdict": out["verdict"], "reasons": attach_evidence(out["reasons"], runs),
            "affected_keps": active, "inactive_reachable": [{"id": i, "hops": h} for i, h in inactive],
            "no_path_found": not (active or inactive), "stale_ancestors": f["stale_ancestors"],
            "approvers": f["approvers"], "recent_approvers": f["recent_approvers"],
            "rules": {"min_sigs_for_gate": 2, "hops": 3, "recent_min_minor": 35}, "data_hash": "abc"}


def five_results():
    return [
        fake_result("A", affected=[("1", 1, "sig-a"), ("2", 3, "sig-b")], approvers=["x"], recent=[]),  # R1 + R4
        fake_result("B", affected=[("3", 2, "sig-a")], inactive=[("9", 2)], stale=[("9", ["B", "5", "9"])]),  # R2 + R3
        fake_result("C", inactive=[("8", 1)], stale=[("8", ["C", "8"])], approvers=["x"], recent=[]),  # R3 + R4
        fake_result("D", approvers=[], recent=[]),  # R4 only: no approvers listed
        fake_result("E"),  # CLEAN
    ]


def test_summarize_counts_all_matches_first_matches_and_hops_exactly():
    rep = summarize(five_results())
    assert (rep["data_hash"], rep["kep_count"], rep["min_sigs_for_gate"], rep["hops"]) == ("abc", 5, 2, 3)
    assert rep["verdicts"] == {"CLEAN": 1, "AT_RISK": 3, "HUMAN_GATE_REQUIRED": 1}
    assert rep["rules_all_match"] == {"R1": 1, "R2": 1, "R3": 2, "R4": 3, "CLEAN": 1}
    assert rep["rules_first_match"] == {"R1": 1, "R2": 1, "R3": 1, "R4": 1, "CLEAN": 1}
    assert rep["hop_sensitivity"] == {
        "1": {"verdicts": {"CLEAN": 2, "AT_RISK": 3, "HUMAN_GATE_REQUIRED": 0}, "keps_with_reach": 2},
        "2": {"verdicts": {"CLEAN": 1, "AT_RISK": 4, "HUMAN_GATE_REQUIRED": 0}, "keps_with_reach": 3},
        "3": {"verdicts": {"CLEAN": 1, "AT_RISK": 3, "HUMAN_GATE_REQUIRED": 1}, "keps_with_reach": 3},
    }
    assert rep["hop_sensitivity"]["3"]["verdicts"] == rep["verdicts"]
    assert rep["reasons_have_evidence"] == {"total": 8, "with_evidence": 8}
    assert (rep["no_reach"], rep["largest_reach"], rep["reach_capped"]) == (2, 2, 0)
    assert json.loads(json.dumps(rep, sort_keys=True)) == rep  # plain JSON types only
    text = render(rep)
    assert "HUMAN_GATE_REQUIRED" in text and "multi-SIG reach" in text and "8 of 8 reasons" in text


def test_summarize_counts_a_reason_without_evidence_and_refuses_mixed_input():
    results = five_results()
    del results[0]["reasons"][0]["cypher"]
    assert summarize(results)["reasons_have_evidence"] == {"total": 8, "with_evidence": 7}
    results[1]["data_hash"] = "other"
    with pytest.raises(RuntimeError, match="disagree"):
        summarize(results)
    with pytest.raises(ValueError, match="No preflight results"):
        summarize([])


@pytest.fixture(scope="module")
def sample():
    """preflight() for 30 KEPs spread evenly over the sorted ids of the graph: the same 30 on every run."""
    ids = kep_ids()
    return [preflight(kid) for kid in ids[:: max(1, len(ids) // 30)][:30]]


@pytest.mark.integration
@pytest.mark.parametrize(
    "kep_id,verdict,rules",
    [
        ("1020", "CLEAN", ["CLEAN"]),            # no REPLACES/SEE_ALSO links at all
        ("624", "AT_RISK", ["R2"]),              # active neighbours, all sig-scheduling
        ("3327", "AT_RISK", ["R2", "R4"]),       # only approver is the group alias @sig-node-tech-leads
        ("1451", "AT_RISK", ["R2", "R4"]),       # approvers not on any KEP with a recent milestone
        ("5468", "AT_RISK", ["R2", "R3"]),       # see-also -> KEP-4153, status replaced
        ("1287", "HUMAN_GATE_REQUIRED", ["R1"]),  # in-place pod resize touches 7 SIGs
    ],
)
def test_preflight_real_keps(kep_id, verdict, rules):
    r = preflight(kep_id)
    assert r["verdict"] == verdict
    assert [x["rule"] for x in r["reasons"]] == rules
    assert r["staged_notice"]["posted"] is False
    assert r["staged_notice"]["markdown"].startswith("**STAGED")
    assert all(qr["cypher"] for qr in r["queries"])
    if verdict == "CLEAN":
        assert r["no_path_found"] and not r["affected_keps"]


@pytest.mark.integration
def test_clean_reason_cites_blast_radius_and_matches_nothing():
    (clean,) = preflight("1020")["reasons"]
    assert (clean["rule_name"], clean["query"], clean["matched_ids"]) == ("no path found", "blast_radius", [])
    assert clean["cypher"].startswith("MATCH") and clean["params"] == {"kep_id": "1020"}


@pytest.mark.integration
def test_stale_reference_reason_cites_is_still_valid_and_the_stale_kep():
    r3 = reason(preflight("5468"), "R3")
    assert (r3["rule_name"], r3["query"], r3["matched_ids"]) == ("stale reference", "is_still_valid", ["4153"])
    assert r3["detail"] == "references 1 stale KEP(s) via see-also within 3 hops"
    assert r3["cypher"] == TEMPLATES["is_still_valid"]["cypher"] and r3["params"]["kep_id"] == "5468"


@pytest.mark.integration
def test_no_recent_approver_reason_cites_recent_approvers_and_the_approvers():
    r = preflight("1451")
    r4 = reason(r, "R4")
    assert (r4["rule_name"], r4["query"]) == ("no recently active approver", "recent_approvers")
    assert r4["matched_ids"] and r4["matched_ids"] == r["approvers"]
    assert r4["params"] == {"kep_id": "1451", "recent_min_minor": r["rules"]["recent_min_minor"]}


@pytest.mark.integration
def test_multi_sig_reason_lists_the_affected_keps_and_the_executed_cypher():
    r = preflight("1287")
    r1 = reason(r, "R1")
    assert (r1["rule_name"], r1["query"]) == ("multi-SIG reach", "blast_radius")
    assert r1["matched_ids"] == [a["id"] for a in r["affected_keps"]] and len(r1["matched_ids"]) > 1
    assert r1["cypher"] == TEMPLATES["blast_radius"]["cypher"]
    assert r1["params"]["kep_id"] == "1287"


@pytest.mark.integration
def test_every_reason_carries_the_query_run_it_was_decided_on(sample):
    assert len(sample) == 30
    for r in sample:
        runs = {run["name"]: run for run in r["queries"]}
        for x in r["reasons"]:
            name, query = RULES[x["rule"]]
            assert set(x) == REASON_KEYS and (x["rule_name"], x["query"]) == (name, query)
            assert x["cypher"] == runs[query]["cypher"] == TEMPLATES[query]["cypher"]
            assert x["params"] == runs[query]["params"] and x["params"]["kep_id"] == r["kep"]["id"]
            assert isinstance(x["evidence"], list) and isinstance(x["matched_ids"], list)


@pytest.mark.integration
def test_no_reason_on_real_keps_says_depends(sample):
    texts = [t for r in sample for x in r["reasons"] for t in (x["detail"], x["rule_name"])]
    assert len(texts) >= 30
    assert not [t for t in texts if "depend" in t.lower()]


@pytest.mark.integration
def test_hop_sensitivity_counts_are_monotone_on_a_sample(sample):
    verdicts = {h: [decide(hop_limited_facts(r, h))["verdict"] for r in sample] for h in (1, 2, 3)}
    gates = [verdicts[h].count("HUMAN_GATE_REQUIRED") for h in (1, 2, 3)]
    assert gates[0] <= gates[1] <= gates[2]
    assert gates[2] > 0  # the sample does reach the gate, so the comparison is not vacuous
    for i, r in enumerate(sample):  # per KEP too: looking further out never lowers the severity
        assert SEVERITY[verdicts[1][i]] <= SEVERITY[verdicts[2][i]] <= SEVERITY[verdicts[3][i]], r["kep"]["id"]
        assert verdicts[3][i] == r["verdict"]  # and the full 3 hops is exactly what preflight decided


@pytest.mark.integration
def test_hop_limited_facts_at_full_depth_reproduce_the_preflight_reasons(sample):
    for r in sample:
        decided = [{k: v for k, v in x.items() if k not in ("cypher", "params")} for x in r["reasons"]]
        assert decide(hop_limited_facts(r, r["rules"]["hops"]))["reasons"] == decided, r["kep"]["id"]


@pytest.mark.integration
def test_verdict_report_summary_adds_up_on_a_sample(sample):
    rep = summarize(sample)
    deepest = rep["hop_sensitivity"][str(rep["hops"])]
    total_reasons = sum(len(r["reasons"]) for r in sample)
    assert rep["kep_count"] == len(sample) == sum(rep["verdicts"].values()) == sum(rep["rules_first_match"].values())
    assert rep["rules_all_match"]["CLEAN"] == rep["rules_first_match"]["CLEAN"] == rep["verdicts"]["CLEAN"]
    assert all(rep["rules_first_match"][rule] <= rep["rules_all_match"][rule] for rule in RULES)
    assert deepest["verdicts"] == rep["verdicts"]
    assert rep["reasons_have_evidence"] == {"total": total_reasons, "with_evidence": total_reasons}
    assert rep["no_reach"] + deepest["keps_with_reach"] == len(sample)
    assert rep["largest_reach"] == max(len(r["affected_keps"]) + len(r["inactive_reachable"]) for r in sample)
    assert rep["data_hash"] == sample[0]["data_hash"]
    assert json.loads(json.dumps(rep, sort_keys=True)) == rep
