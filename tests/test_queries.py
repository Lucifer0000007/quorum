"""Integration tests for the query layer on the live company_brain graph (expected values checked against kep.yaml)."""

import pytest

from agent import queries as q

pytestmark = pytest.mark.integration


def test_kep_detail_4381():
    r = q.kep_detail("KEP-4381")
    assert r["result"]["owning_sigs"] == ["sig-node"]
    assert {"sig-scheduling", "sig-autoscaling"} <= set(r["result"]["participating_sigs"])
    assert any(n["id"] == "3063" and n["direction"] == "out" for n in r["result"]["neighbors"])
    assert r["cypher"].startswith("MATCH") and r["source_links"]


def test_blast_radius_1287_spans_many_sigs():
    r = q.blast_radius("1287")["result"]
    sigs = {a["owning_sig"] for a in r["reachable"] if a["active"]}
    assert len(sigs) >= 2 and not r["no_path_found"]
    assert all(1 <= a["hops"] <= 3 for a in r["reachable"])


def test_blast_radius_reports_no_path():
    r = q.blast_radius("1020")["result"]
    assert r["no_path_found"] and "No path found" in r["message"]


def test_successor_chain_2377():
    r = q.is_still_valid("2377")["result"]
    assert r["has_successor"] and r["successors"][0]["id"] == "2386"
    assert r["validity"] == "superseded"


def test_who_knows_exposes_formula():
    r = q.who_knows(sig="sig-node")["result"]
    assert r["people"] and "score(person)" in r["formula"]


def test_ambiguous_and_missing_ids_fail_loudly():
    with pytest.raises(q.Ambiguous):
        q.resolve_kep("2133")
    with pytest.raises(q.NotFound):
        q.resolve_kep("999999")
