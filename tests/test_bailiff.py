"""Bailiff verdict branches on real KEPs (live graph) plus pure-rule checks on facts taken from those KEPs."""

import pytest

from agent.bailiff import decide, preflight


def facts(**kw):
    base = {"kep_id": "x", "affected": [], "affected_sigs": set(), "stale_ancestors": [], "approvers": ["a"],
            "recent_approvers": ["a"], "recent_min_minor": 35}
    return base | kw


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


@pytest.mark.integration
@pytest.mark.parametrize(
    "kep_id,verdict,rules",
    [
        ("1020", "CLEAN", ["CLEAN"]),            # no REPLACES/SEE_ALSO links at all
        ("3327", "AT_RISK", ["R2"]),             # 9 active neighbours, all sig-node
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
