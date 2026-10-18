"""Parser tests on real kep.yaml files copied from kubernetes/enhancements into tests/fixtures/keps."""

from pathlib import Path

import pytest

from ingest.parse import (
    build_rows,
    milestone_minor,
    normalize_handle,
    normalize_sig,
    normalize_status,
    parse_corpus,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def built():
    records, failures = parse_corpus(FIXTURES)
    assert failures == []
    return build_rows(records, "fixture")


def kep(built, kep_id):
    return next(k for k in built["rows"]["keps"] if k["id"] == kep_id)


def edges(built, rel):
    return {(e["src"], e["dst"]) for e in built["rows"]["edges"][rel]}


def test_all_fixtures_parse(built):
    assert built["meta"]["kep_count"] == 10


def test_dockershim_2221_fields(built):
    k = kep(built, "2221")
    assert k["title"] == "Removing dockershim from kubelet"
    assert k["status"] == "implemented"
    assert k["owning_sig"] == "sig-node"
    assert k["latest_milestone"] == "v1.24" and k["latest_minor"] == 24
    assert k["issue_url"] == "https://github.com/kubernetes/enhancements/issues/2221"
    assert k["source_url"].endswith("/keps/sig-node/2221-remove-dockershim")
    assert ("dchen1107", "2221") in edges(built, "APPROVED")
    assert ("resouer", "2221") in edges(built, "AUTHORED")
    assert ("sjennning", "2221") in edges(built, "REVIEWED")


def test_invalid_date_4355_still_parses_as_string(built):
    k = kep(built, "4355")
    assert k["creation_date"] == "2023-14-05"  # impossible date kept verbatim, not crashed on
    assert ("4355", "sig-cluster-lifecycle") in edges(built, "PARTICIPATES")


def test_replaces_4671_resolves_only_known_targets(built):
    # 583 and 5832 are not in the fixture set, so they must be counted as unresolved, never invented.
    assert not {e for e in edges(built, "REPLACES") if e[0] == "4671"}
    assert "/keps/sig-scheduling/583-coscheduling" in kep(built, "4671")["unresolved_refs"]


def test_superseded_by_2377_becomes_reverse_replaces(built):
    assert ("2386", "2377") in edges(built, "REPLACES")


def test_see_also_resolves_within_corpus(built):
    # 4381 -> /keps/sig-node/3063-... is outside the fixtures: unresolved here.
    assert "/keps/sig-node/3063-dynamic-resource-allocation" in kep(built, "4381")["unresolved_refs"]


def test_duplicate_number_2133_gets_path_ids(built):
    ids = {k["id"] for k in built["rows"]["keps"]}
    assert "2133" not in ids
    assert "sig-node/2133-kubelet-credential-providers" in ids
    assert "sig-cloud-provider/2133-out-of-tree-credential-provider" in ids
    assert kep(built, "sig-node/2133-kubelet-credential-providers")["issue_url"].endswith("/issues/2133")


def test_zero_number_gets_path_id_and_no_issue(built):
    k = kep(built, "sig-architecture/0000-kep-process")
    assert k["issue_url"] is None


def test_data_hash_is_deterministic():
    a = build_rows(parse_corpus(FIXTURES)[0], "fixture")["meta"]["data_hash"]
    b = build_rows(parse_corpus(FIXTURES)[0], "fixture")["meta"]["data_hash"]
    assert a == b


@pytest.mark.parametrize(
    "raw,expected",
    [("@Dims", "dims"), ("TBD", None), ("", None), ("Jane Doe (@jdoe)", "jdoe"), ("n/a", None), ("thockin", "thockin")],
)
def test_normalize_handle(raw, expected):
    assert normalize_handle(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("implemented", "implemented"), ("imlpemented", "implemented"), ("implemented (alpha)", "implemented"),
     ("superseded", "replaced"), ("removed", "withdrawn"), ("provisional|implementable|implemented", "unknown")],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


def test_sig_aliases_and_milestones():
    assert normalize_sig("sig-scheduler") == "sig-scheduling"
    assert normalize_sig("TBD") is None
    assert milestone_minor("v1.38") == 38
    assert milestone_minor("1.20") == 20
    assert milestone_minor("0.0") is None
