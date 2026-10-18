"""Fast checks for the raw-text keyword baseline and for the imports the eval code is allowed to use.

No FalkorDB: the baseline reads the cloned kep.yaml files as text; the import checks only parse source code.
"""

import ast
from pathlib import Path

import pytest

from evals.baseline import Doc, KeywordSearch, block, first_line
from evals.lookups.make_gold import build_gold as build_lookups
from evals.lookups.make_gold import sig_kep_counts, successor_ids
from evals.multihop.make_gold import build_gold as build_multihop
from evals.yamlcorpus import VALID_STATUSES, kep_graph, load_keps

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
ENHANCEMENTS = ROOT / "data" / "enhancements"
needs_corpus = pytest.mark.skipif(
    not (ENHANCEMENTS / "keps").is_dir(), reason="data/enhancements not cloned (run `make clone-data`)"
)

GOLD_MODULES = ("yamlcorpus.py", "lookups/make_gold.py", "multihop/make_gold.py")


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import statement in the file (relative imports keep their leading dots)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
    return found


def banned_imports(path: Path, roots: set[str]) -> set[str]:
    return {m for m in imported_modules(path) if m.lstrip(".").split(".")[0] in roots}


# ---- what the eval code may import ---------------------------------------------------------------------------------


def test_baseline_reads_text_only():
    path = EVALS / "baseline.py"
    # no YAML parser, no app code, no database client, and none of the gold helpers (graph, BFS, normalization)
    assert not banned_imports(path, {"yaml", "ingest", "agent", "falkordb", "redis", "evals"})
    assert "importlib" not in imported_modules(path)
    assert "__import__" not in path.read_text(encoding="utf-8")


def test_gold_code_is_independent_of_the_app_and_the_database():
    for rel in GOLD_MODULES:
        path = EVALS / rel
        assert not banned_imports(path, {"ingest", "agent", "falkordb", "redis"}), rel
        assert "evals.baseline" not in imported_modules(path), rel
        assert "importlib" not in imported_modules(path), rel
        assert "__import__" not in path.read_text(encoding="utf-8"), rel


# ---- the three text primitives, on a hand-written file -------------------------------------------------------------

SAMPLE = """title: Sample
kep-number: "0123" # quoted
owning-sig: "sig-node" # trailing comment
status: implementable # Jumping over "provisional" here
approvers:
  - "@Alice" # a comment
  - @sig-node-leads
- bare-at-column-zero

# a comment line between keys
see-also:
  - "/keps/sig-node/1-foo/README.md"
replaces: []
superseded-by:
stage: stable
"""


def test_first_line_strips_quotes_and_comments():
    doc = Doc("sig-node", "123-sample", SAMPLE)
    assert first_line(doc, "kep-number") == "0123"
    assert first_line(doc, "owning-sig") == "sig-node"
    assert first_line(doc, "status") == "implementable"
    assert first_line(doc, "stage") == "stable"
    assert first_line(doc, "missing") is None


def test_block_reads_until_the_next_top_level_key():
    doc = Doc("sig-node", "123-sample", SAMPLE)
    approvers = [ln.strip() for ln in block(doc, "approvers")]
    assert approvers == ['- "@Alice" # a comment', "- @sig-node-leads", "- bare-at-column-zero"]
    assert [ln.strip() for ln in block(doc, "see-also")] == ['- "/keps/sig-node/1-foo/README.md"']
    assert [ln.strip() for ln in block(doc, "replaces")] == ["[]"]
    assert block(doc, "superseded-by") == []
    assert block(doc, "missing") == []


# ---- the baseline on the cloned corpus -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def search():
    return KeywordSearch(ENHANCEMENTS)


@pytest.fixture(scope="module")
def questions():
    return build_lookups(ENHANCEMENTS)["questions"] + build_multihop(ENHANCEMENTS)["questions"]


@needs_corpus
def test_kep_2221(search):
    assert search.answer("owning_sig", {"kep_id": "2221"}) == "sig-node"
    assert search.answer("status", {"kep_id": "2221"}) == "implemented"
    assert search.answer("approvers", {"kep_id": "2221"}) == ["dchen1107", "derekwaynecarr"]
    assert search.answer("has_successor", {"kep_id": "2221"}) is False


@needs_corpus
def test_kep_2377_has_a_successor(search):
    assert search.answer("has_successor", {"kep_id": "2377"}) is True
    assert search.answer("owning_sig", {"kep_id": "2377"}) == "sig-cli"


@needs_corpus
def test_text_search_agrees_with_the_yaml_gold_where_it_should(search):
    # Two independent implementations (grep-style text search, YAML parse + resolver) on every 8th numbered KEP.
    keps = load_keps(ENHANCEMENTS)
    successors = successor_ids(keps)
    numbered = [k for k in keps if k.kep_id][::8]
    assert len(numbered) > 50
    for kep in numbered:
        params = {"kep_id": kep.kep_id}
        assert search.answer("owning_sig", params) == kep.owning_sig, kep.kep_id
        assert search.answer("has_successor", params) is (kep.kep_id in successors), kep.kep_id
        if kep.raw_status in VALID_STATUSES:
            assert search.answer("status", params) == kep.raw_status.strip().lower(), kep.kep_id
    counts = sig_kep_counts(keps)
    for sig in ("sig-network", "sig-apps", "sig-scheduling"):  # sig-apps and sig-scheduling have quoted values
        assert search.answer("sig_kep_count", {"sig": sig}) == counts[sig]


@needs_corpus
def test_answers_have_the_shape_of_the_gold_for_every_kind(search, questions):
    assert {q["kind"] for q in questions} == set(search._handlers)
    for q in questions:
        answer = search.answer(q["kind"], q["params"])
        assert type(answer) is type(q["gold"]), q
        if isinstance(answer, list):
            assert all(isinstance(item, str) for item in answer), q
            assert answer == sorted(answer, key=lambda i: (not i.isdigit(), int(i) if i.isdigit() else 0, i)), q


@needs_corpus
def test_multihop_answers_are_a_lower_bound(search, questions):
    adj = kep_graph(load_keps(ENHANCEMENTS))
    for q in questions:
        kid = q["params"].get("kep_id")
        answer = search.answer(q["kind"], q["params"])
        if q["kind"] == "hop2_set":
            assert set(answer) <= set(adj[kid]), q  # only first-hop neighbours, never anything else
            assert set(answer) <= set(q["gold"]), q
        elif q["kind"] == "reachable_count_3hop":
            assert answer <= len(adj[kid]) and answer <= q["gold"], q
        elif q["kind"] in ("stale_reach", "reachable_sigs"):
            assert set(answer) <= set(q["gold"]), q


@needs_corpus
def test_unknown_kind_and_unknown_kep_fail_loudly(search):
    with pytest.raises(KeyError):
        search.answer("blast_radius_count", {"kep_id": "2221"})
    with pytest.raises(LookupError):
        search.answer("owning_sig", {"kep_id": "99999999"})
