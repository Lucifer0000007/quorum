"""Fast checks for the eval gold generators (lookups + multihop) and their shared YAML helpers.

No FalkorDB: the corpus tests read the cloned kep.yaml files directly; the helper tests use tiny hand-built inputs.
"""

import json
from collections import Counter, deque
from pathlib import Path

import pytest
import yaml

from evals.lookups.make_gold import approver_handles, successor_ids
from evals.lookups.make_gold import build_gold as build_lookups
from evals.multihop.make_gold import build_gold as build_multihop
from evals.yamlcorpus import (
    ACTIVE,
    STALE,
    Kep,
    contested_edges,
    id_sort_key,
    kep_graph,
    load_keps,
    make_resolver,
    node_id,
    normalize_sig,
    normalize_status,
    reach,
)

ROOT = Path(__file__).resolve().parent.parent
ENHANCEMENTS = ROOT / "data" / "enhancements"
needs_corpus = pytest.mark.skipif(
    not (ENHANCEMENTS / "keps").is_dir(), reason="data/enhancements not cloned (run `make clone-data`)"
)

LOOKUP_COUNTS = {
    "owning_sig": 5,
    "status": 5,
    "has_successor": 8,
    "see_also_referrers": 5,
    "sig_kep_count": 5,
    "approvers": 5,
}
MULTIHOP_COUNTS = {"hop2_set": 8, "reachable_count_3hop": 5, "stale_reach": 8, "reachable_sigs": 8}

TEMPLATES = {
    "owning_sig": "Which SIG owns KEP-{kep_id}?",
    "status": "What is the status of KEP-{kep_id}?",
    "has_successor": "Does KEP-{kep_id} have a successor?",
    "see_also_referrers": "Which KEPs list KEP-{kep_id} in see-also?",
    "sig_kep_count": "How many KEPs does {sig} own?",
    "approvers": "Who approved KEP-{kep_id}?",
    "hop2_set": "Which KEPs are reachable via see-also/replaces within 2 hops of KEP-{kep_id}?",
    "reachable_count_3hop": "How many KEPs are reachable via see-also/replaces within 3 hops of KEP-{kep_id}?",
    "stale_reach": (
        "Which active KEPs are reachable via see-also/replaces within 3 hops of KEP-{kep_id}, which is {status}?"
    ),
    "reachable_sigs": "Which SIGs own the active KEPs reachable via see-also/replaces within 3 hops of KEP-{kep_id}?",
}


@pytest.fixture(scope="module")
def keps():
    return load_keps(ENHANCEMENTS)


@pytest.fixture(scope="module")
def lookups():
    return build_lookups(ENHANCEMENTS)


@pytest.fixture(scope="module")
def multihop():
    return build_multihop(ENHANCEMENTS)


@pytest.fixture(scope="module", params=["lookups", "multihop"])
def suite(request, lookups, multihop):
    return request.param, {"lookups": lookups, "multihop": multihop}[request.param]


def raw_yaml(sig_dir: str, folder: str) -> dict:
    path = ENHANCEMENTS / "keps" / sig_dir / folder / "kep.yaml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def bfs_levels(adj: dict, start: str, max_hops: int) -> dict:
    """A plain queue-based BFS, written independently of `evals.yamlcorpus.reach`."""
    dist = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if dist[node] == max_hops:
            continue
        for neighbour in adj.get(node, ()):
            if neighbour not in dist:
                dist[neighbour] = dist[node] + 1
                queue.append(neighbour)
    del dist[start]
    return dist


def fake_kep(folder: str, number: str | None, sig_dir: str = "sig-x", status: str = "implemented", **refs) -> Kep:
    return Kep(
        folder=folder,
        sig_dir=sig_dir,
        data={},
        kep_id=number,
        owning_sig=normalize_sig(sig_dir),
        raw_status=status,
        refs={field.replace("_", "-"): value for field, value in refs.items()},
    )


# ---- shared helpers on hand-built inputs (no corpus needed) ------------------------------------------------------


def test_normalize_status():
    for raw, expected in {
        "implemented": "implemented",
        " Implementable ": "implementable",
        "imlpemented": "implemented",
        "implemented (alpha)": "implemented",
        "superseded": "replaced",
        "removed": "withdrawn",
        "deferred": "deferred",
        "provisional|implementable|implemented|deferred|rejected|withdrawn|replaced": "unknown",
        "": "unknown",
        None: "unknown",
    }.items():
        assert normalize_status(raw) == expected, raw
    assert ACTIVE == {"provisional", "implementable", "implemented"}
    assert STALE == {"replaced", "rejected", "withdrawn", "deferred"}


def test_normalize_sig_merges_aliases():
    assert normalize_sig("sig-apimachinery") == "sig-api-machinery"
    assert normalize_sig("sig-machinery") == "sig-api-machinery"
    assert normalize_sig("sig-arch") == "sig-architecture"
    assert normalize_sig("sig-contribex") == "sig-contributor-experience"
    assert normalize_sig("sig-scheduler") == "sig-scheduling"
    assert normalize_sig(" SIG-Node ") == "sig-node"
    assert normalize_sig("") is None
    assert normalize_sig(None) is None


def test_resolver_rules():
    keps = [fake_kep("100-alpha", "100"), fake_kep("200-beta", "200"), fake_kep("0000-draft", None)]
    resolve = make_resolver(keps)
    assert resolve("/keps/sig-x/100-alpha/") == "100"
    assert resolve("keps/sig-x/100-alpha/README.md") == "100"
    assert resolve("https://github.com/kubernetes/enhancements/blob/master/keps/sig-x/200-beta.md") == "200"
    assert resolve("/keps/sig-x/100-renamed") == "100"  # a renamed folder resolves by its number prefix
    assert resolve("/keps/sig-x/999-unknown") is None
    assert resolve("/keps/sig-x/0000-draft") == "sig-x/0000-draft"  # KEPs without a valid number keep a path id
    assert resolve("https://github.com/kubernetes/enhancements/issues/200") == "200"  # a KEP's tracking issue
    assert resolve("https://github.com/kubernetes/enhancements/issues/999") is None
    assert resolve("see KEP-100 for context") == "100"
    assert resolve("https://github.com/kubernetes/enhancements/pull/100") is None
    assert resolve("n/a") is None
    assert resolve("") is None


def test_graph_is_undirected_and_drops_self_references():
    keps = [
        fake_kep("1-a", "1", see_also=["/keps/sig-x/1-a", "/keps/sig-x/2-b"], replaces=["/keps/sig-x/3-c"]),
        fake_kep("2-b", "2"),
        fake_kep("3-c", "3"),
        fake_kep("4-d", "4", superseded_by=["/keps/sig-x/1-a"]),
        fake_kep("0-e", None, see_also=["/keps/sig-x/4-d"]),
        fake_kep("5-f", "5"),
    ]
    adj = kep_graph(keps)
    assert adj == {
        "1": {"2", "3", "4"},
        "2": {"1"},
        "3": {"1"},
        "4": {"1", "sig-x/0-e"},
        "sig-x/0-e": {"4"},
        "5": set(),
    }


def test_contested_edges_are_links_the_resolver_does_not_follow():
    keps = [
        fake_kep("100-alpha", "100"),
        fake_kep("200-beta", "200", see_also=["/keps/sig-x/100", "/kep/sig-x/100-alpha/", "https://example.org/x"]),
    ]
    assert make_resolver(keps)("/keps/sig-x/100") is None
    assert contested_edges(keps) == {("200", "see-also", "100")}
    assert kep_graph(keps)["200"] == set()
    assert kep_graph(keps, include_contested=True)["200"] == {"100"}


def test_reach_on_a_hand_built_graph():
    # a - b - c - d - e is a line; a - f - d is a shortcut to d, so d is 2 hops from a, not 4. g is isolated.
    edges = [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"), ("a", "f"), ("f", "d")]
    adj: dict[str, set[str]] = {n: set() for n in "abcdefg"}
    for x, y in edges:
        adj[x].add(y)
        adj[y].add(x)
    assert reach(adj, "a", 1) == {"b": 1, "f": 1}
    assert reach(adj, "a", 2) == {"b": 1, "f": 1, "c": 2, "d": 2}
    assert reach(adj, "a", 3) == {"b": 1, "f": 1, "c": 2, "d": 2, "e": 3}
    assert reach(adj, "e", 2) == {"d": 1, "c": 2, "f": 2}
    assert reach(adj, "g", 3) == {}
    for start in "abcdefg":
        for hops in (1, 2, 3):
            assert reach(adj, start, hops) == bfs_levels(adj, start, hops)


def test_id_sort_puts_numbers_first():
    ids = ["sig-b/x", "100", "sig-a/y", "9", "20"]
    assert sorted(ids, key=id_sort_key) == ["9", "20", "100", "sig-a/y", "sig-b/x"]


def test_approver_handles_filtering():
    fake = Kep(
        folder="x",
        sig_dir="sig-x",
        data={"approvers": ["@Foo", "bar", "TBD", "n/a", "@foo", "@sig-node-leads", "@deads2k - For API portion", ""]},
    )
    assert approver_handles(fake) == ["bar", "deads2k", "foo"]


# ---- both gold files ---------------------------------------------------------------------------------------------


@needs_corpus
def test_shape(suite):
    name, gold = suite
    qs = gold["questions"]
    assert gold["generated_by"] == f"evals/{name}/make_gold.py"
    assert len(gold["source_commit"]) == 40
    assert len({q["id"] for q in qs}) == len(qs)
    for q in qs:
        assert set(q) == {"id", "kind", "question", "params", "gold"}


@needs_corpus
def test_counts_per_kind(lookups, multihop):
    assert Counter(q["kind"] for q in lookups["questions"]) == LOOKUP_COUNTS
    assert Counter(q["kind"] for q in multihop["questions"]) == MULTIHOP_COUNTS


@needs_corpus
def test_exact_phrasing(suite):
    _, gold = suite
    for q in gold["questions"]:
        template = TEMPLATES[q["kind"]]
        if q["kind"] == "stale_reach":  # the status word is checked against the KEP in test_stale_reach_gold
            status = q["question"].rsplit("which is ", 1)[-1].rstrip("?")
            assert q["question"] == template.format(**q["params"], status=status)
        else:
            assert q["question"] == template.format(**q["params"])


@needs_corpus
def test_no_dependency_wording(suite):
    _, gold = suite
    for q in gold["questions"]:
        assert "depend" not in q["question"].lower(), q["question"]


@needs_corpus
def test_deterministic(lookups, multihop):
    assert build_lookups(ENHANCEMENTS) == lookups
    assert build_multihop(ENHANCEMENTS) == multihop


@needs_corpus
def test_committed_gold_files_match_the_generators(lookups, multihop):
    for name, gold in (("lookups", lookups), ("multihop", multihop)):
        committed = json.loads((ROOT / "evals" / name / "gold.json").read_text(encoding="utf-8"))
        assert committed == gold, f"evals/{name}/gold.json is stale: run `python -m evals.{name}.make_gold`"


@needs_corpus
def test_kep_2221_owning_sig(keps):
    raw = raw_yaml("sig-node", "2221-remove-dockershim")
    assert raw["kep-number"] == "2221"
    assert raw["owning-sig"] == "sig-node"
    kep = next(k for k in keps if k.kep_id == "2221")
    assert kep.owning_sig == "sig-node"
    assert kep.status == "implemented"


@needs_corpus
def test_kep_2377_has_successor(keps):
    raw = raw_yaml("sig-cli", "2377-Kustomize")
    assert any("2386-kustomize-subcommand-integration" in ref for ref in raw["superseded-by"])
    assert "2377" in successor_ids(keps)


@needs_corpus
def test_corpus_statuses_normalize(keps):
    by_raw = {k.raw_status: k.status for k in keps}
    assert by_raw["imlpemented"] == "implemented"
    assert by_raw["implemented (alpha)"] == "implemented"
    assert by_raw["superseded"] == "replaced"
    assert by_raw["removed"] == "withdrawn"


# ---- lookups -----------------------------------------------------------------------------------------------------


@needs_corpus
def test_has_successor_includes_true_and_false(lookups):
    vals = [q["gold"] for q in lookups["questions"] if q["kind"] == "has_successor"]
    assert vals.count(True) == vals.count(False) == 4


@needs_corpus
def test_see_also_gold_nonempty_and_no_self(lookups):
    for q in lookups["questions"]:
        if q["kind"] == "see_also_referrers":
            assert q["gold"], q
            assert q["params"]["kep_id"] not in q["gold"]
            assert q["gold"] == sorted(q["gold"], key=int)


@needs_corpus
def test_approvers_gold(keps, lookups):
    approvers_by_id = {k.kep_id: approver_handles(k) for k in keps if k.kep_id}
    qs = [q for q in lookups["questions"] if q["kind"] == "approvers"]
    assert len(qs) == 5
    for q in qs:
        assert q["gold"] and q["gold"] == sorted(set(q["gold"]))
        assert q["gold"] == approvers_by_id[q["params"]["kep_id"]]


@needs_corpus
def test_approver_handles_on_a_real_kep(keps):
    kep = next(k for k in keps if k.kep_id == "2221")
    assert approver_handles(kep) == ["dchen1107", "derekwaynecarr"]


# ---- multihop ----------------------------------------------------------------------------------------------------


@needs_corpus
def test_multihop_gold_matches_an_independent_bfs(keps, multihop):
    adj = kep_graph(keps)
    by_node = {node_id(k): k for k in keps}
    for q in multihop["questions"]:
        within3 = bfs_levels(adj, q["params"]["kep_id"], 3)
        active = {n for n in within3 if by_node[n].status in ACTIVE}
        if q["kind"] == "reachable_count_3hop":
            assert q["gold"] == len(within3), q
            continue
        if q["kind"] == "reachable_sigs":
            assert q["gold"] == sorted({by_node[n].owning_sig for n in active if by_node[n].owning_sig}), q
            continue
        expected = {n for n, hops in within3.items() if hops <= 2} if q["kind"] == "hop2_set" else active
        assert set(q["gold"]) == expected, q
        assert q["gold"] == sorted(q["gold"], key=id_sort_key), q  # numbers first, then "<sig-dir>/<folder>" ids


@needs_corpus
def test_a_real_chain_from_raw_yaml(keps):
    # KEP-5366 sits at the end of a chain 5366 - 6361 - 4355 - 1965. Build that graph by hand from the three files that
    # hold the links, and compare with the graph the generators use.
    sig_dir = "sig-api-machinery"
    first_links = raw_yaml(sig_dir, "6361-leader-election-recovery")["see-also"]
    second_links = raw_yaml(sig_dir, "4355-coordinated-leader-election")["see-also"]
    assert any("5366-graceful-leader-transition" in ref for ref in first_links)
    assert any("4355-coordinated-leader-election" in ref for ref in first_links)
    assert any("1965-kube-apiserver-identity" in ref for ref in second_links)
    hand_built = {"5366": {"6361"}, "6361": {"5366", "4355"}, "4355": {"6361", "1965"}, "1965": {"4355"}}
    assert bfs_levels(hand_built, "5366", 3) == {"6361": 1, "4355": 2, "1965": 3}
    assert bfs_levels(hand_built, "5366", 2) == {"6361": 1, "4355": 2}
    assert reach(kep_graph(keps), "5366", 3) == {"6361": 1, "4355": 2, "1965": 3}


@needs_corpus
def test_a_stale_reach_answer_from_raw_yaml(multihop):
    # KEP-2451 is replaced, and KEP-1205 (implemented) lists it under `replaces`: a two-node graph built by hand.
    old = raw_yaml("sig-storage", "2451-service-account-token-volumes")
    new = raw_yaml("sig-auth", "1205-bound-service-account-tokens")
    assert old["status"] == "replaced" and new["status"] == "implemented"
    assert any("2451-service-account-token-volumes" in ref for ref in new["replaces"])
    reachable = bfs_levels({"2451": {"1205"}, "1205": {"2451"}}, "2451", 3)
    expected = [n for n in reachable if new["status"] in ACTIVE]
    q = next((q for q in multihop["questions"] if q["kind"] == "stale_reach" and q["params"]["kep_id"] == "2451"), None)
    assert q is not None, "KEP-2451 should be one of the stale_reach targets"
    assert q["gold"] == expected == ["1205"]
    assert q["question"].endswith("of KEP-2451, which is replaced?")


@needs_corpus
def test_multihop_targets_are_numbered_distinct_and_unambiguous(keps, multihop):
    numbered = {k.kep_id for k in keps if k.kep_id}
    targets = [q["params"]["kep_id"] for q in multihop["questions"]]
    assert all(set(q["params"]) == {"kep_id"} for q in multihop["questions"])
    assert len(set(targets)) == len(targets)
    assert set(targets) <= numbered
    strict, wide = kep_graph(keps), kep_graph(keps, include_contested=True)
    for kid in targets:  # no answer changes if the doubtful links were followed too
        assert reach(strict, kid, 3) == reach(wide, kid, 3)


@needs_corpus
def test_hop2_questions_mostly_need_the_second_hop(keps, multihop):
    adj = kep_graph(keps)
    qs = [q for q in multihop["questions"] if q["kind"] == "hop2_set"]
    needs_second_hop = 0
    for q in qs:
        assert 1 <= len(q["gold"]) <= 40
        needs_second_hop += len(q["gold"]) > len(bfs_levels(adj, q["params"]["kep_id"], 1))
    assert needs_second_hop >= len(qs) / 2


@needs_corpus
def test_reachable_count_gold(multihop):
    golds = [q["gold"] for q in multihop["questions"] if q["kind"] == "reachable_count_3hop"]
    assert all(isinstance(g, int) and 1 <= g <= 60 for g in golds)
    assert len(set(golds)) == len(golds)  # spread over different sizes


@needs_corpus
def test_stale_reach_gold(keps, multihop):
    by_node = {node_id(k): k for k in keps}
    qs = [q for q in multihop["questions"] if q["kind"] == "stale_reach"]
    for q in qs:
        status = by_node[q["params"]["kep_id"]].status
        assert status in {"withdrawn", "rejected", "replaced"}
        assert q["question"].endswith(f", which is {status}?")
        assert q["gold"], q  # every target has at least one active KEP within 3 hops
        assert all(by_node[n].status in ACTIVE for n in q["gold"])
    assert any(by_node[q["params"]["kep_id"]].raw_status == "superseded" for q in qs)  # "superseded" counts as replaced


@needs_corpus
def test_reachable_sigs_gold(multihop):
    qs = [q for q in multihop["questions"] if q["kind"] == "reachable_sigs"]
    for q in qs:
        assert q["gold"] and q["gold"] == sorted(set(q["gold"]))
        assert all(sig.startswith("sig-") for sig in q["gold"])
    assert sum(len(q["gold"]) >= 2 for q in qs) >= 3
    assert sum(len(q["gold"]) == 1 for q in qs) >= 2


@needs_corpus
def test_lookups_do_not_depend_on_doubtful_links(keps, lookups):
    contested = contested_edges(keps)
    doubtful_referrer_targets = {tgt for _, field, tgt in contested if field == "see-also"}
    for q in lookups["questions"]:
        if q["kind"] == "see_also_referrers":
            assert q["params"]["kep_id"] not in doubtful_referrer_targets
