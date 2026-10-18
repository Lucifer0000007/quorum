"""Fast checks for evals/make_gold.py (no FalkorDB; reads the cloned corpus directly)."""

from pathlib import Path

import pytest
import yaml

from evals.make_gold import build_gold, load_keps, successor_ids

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
    assert len(qs) == 28
    assert len({q["id"] for q in qs}) == 28
    assert gold["generated_by"] == "evals/make_gold.py"
    assert len(gold["source_commit"]) == 40
    kinds = {q["kind"] for q in qs}
    assert kinds == {"owning_sig", "status", "has_successor", "see_also_referrers", "sig_kep_count"}
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
