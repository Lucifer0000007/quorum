"""Fast checks for evals/make_gold.py (no FalkorDB; reads the cloned corpus directly)."""

from pathlib import Path

import pytest
import yaml

from evals.make_gold import approver_handles, blast_radius_count, build_gold, load_keps, successor_ids

ENHANCEMENTS = Path(__file__).resolve().parent.parent / "data" / "enhancements"

if not (ENHANCEMENTS / "keps").is_dir():
    pytest.skip("data/enhancements not cloned (run `make clone-data`)", allow_module_level=True)


@pytest.fixture(scope="module")
def keps():
    return load_keps(ENHANCEMENTS)


@pytest.fixture(scope="module")
def gold():
    return build_gold(ENHANCEMENTS)


def test_shape(gold):
    qs = gold["questions"]
    assert len(qs) == 38
    assert len({q["id"] for q in qs}) == 38
    assert gold["generated_by"] == "evals/make_gold.py"
    assert len(gold["source_commit"]) == 40
    kinds = {q["kind"] for q in qs}
    assert kinds == {
        "owning_sig",
        "status",
        "has_successor",
        "see_also_referrers",
        "sig_kep_count",
        "blast_radius_count",
        "approvers",
    }
    for q in qs:
        assert set(q) == {"id", "kind", "question", "params", "gold"}


def test_deterministic(gold):
    assert build_gold(ENHANCEMENTS) == gold


def test_kep_2221_owning_sig(keps):
    raw = yaml.load(
        (ENHANCEMENTS / "keps" / "sig-node" / "2221-remove-dockershim" / "kep.yaml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert raw["kep-number"] == "2221"
    assert raw["owning-sig"] == "sig-node"
    kep = next(k for k in keps if k.kep_id == "2221")
    assert kep.owning_sig == "sig-node"


def test_kep_2377_has_successor(keps):
    assert "2377" in successor_ids(keps)


def test_has_successor_includes_true_and_false(gold):
    vals = [q["gold"] for q in gold["questions"] if q["kind"] == "has_successor"]
    assert vals.count(True) >= 3
    assert vals.count(True) == vals.count(False)


def test_see_also_gold_nonempty_and_no_self(gold):
    for q in gold["questions"]:
        if q["kind"] == "see_also_referrers":
            assert q["gold"], q
            assert q["params"]["kep_id"] not in q["gold"]


def test_blast_radius_gold_in_range(gold):
    qs = [q for q in gold["questions"] if q["kind"] == "blast_radius_count"]
    assert len(qs) == 5
    assert all(1 <= q["gold"] <= 60 for q in qs)
    assert all(q["question"] == f"What is the blast radius of KEP-{q['params']['kep_id']}?" for q in qs)


def test_blast_radius_bfs():
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b", "d"}, "d": {"c", "e"}, "e": {"d"}}
    assert blast_radius_count(adj, "a") == 3
    assert blast_radius_count(adj, "c") == 4


def test_approver_handles_filtering(keps):
    kep = next(k for k in keps if k.kep_id == "2221")
    assert approver_handles(kep) == ["dchen1107", "derekwaynecarr"]
    fake = type(kep)(
        folder="x",
        sig_dir="sig-x",
        data={"approvers": ["@Foo", "bar", "TBD", "n/a", "@foo", "@sig-node-leads", "@deads2k - For API portion", ""]},
    )
    assert approver_handles(fake) == ["bar", "deads2k", "foo"]


def test_approvers_gold(gold):
    qs = [q for q in gold["questions"] if q["kind"] == "approvers"]
    assert len(qs) == 5
    for q in qs:
        assert q["gold"] and q["gold"] == sorted(set(q["gold"]))
        assert q["question"] == f"Who approved KEP-{q['params']['kep_id']}?"
