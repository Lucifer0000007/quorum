import pytest

from agent.guard import GuardError, check_query, check_timeout
from agent.templates import TEMPLATES


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_passes_guard(name):
    check_query(TEMPLATES[name]["cypher"], {"limit": 10})


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (k:KEP) SET k.status = 'x' RETURN k LIMIT 1",
        "CREATE (:KEP {id: '1'}) RETURN 1 LIMIT 1",
        "MATCH (k) DETACH DELETE k RETURN 1 LIMIT 1",
        "MERGE (k:KEP {id: '1'}) RETURN k LIMIT 1",
        "MATCH (k) REMOVE k.title RETURN k LIMIT 1",
        "MATCH (k) FOREACH (x IN [1] | SET k.a = x) RETURN k LIMIT 1",
        "LOAD CSV FROM 'file:///x' AS row RETURN row LIMIT 1",
        "CALL db.idx.fulltext.createNodeIndex('KEP', 'title') RETURN 1 LIMIT 1",
        "match (k:KEP) set k.x = 1 return k limit 1",
    ],
)
def test_writes_and_unlisted_procedures_rejected(cypher):
    with pytest.raises(GuardError):
        check_query(cypher)


def test_limit_required_and_capped():
    with pytest.raises(GuardError, match="LIMIT"):
        check_query("MATCH (k:KEP) RETURN k")
    with pytest.raises(GuardError, match="exceeds"):
        check_query("MATCH (k:KEP) RETURN k LIMIT 101")
    with pytest.raises(GuardError):
        check_query("MATCH (k:KEP) RETURN k LIMIT $limit", {"limit": 500})
    with pytest.raises(GuardError):
        check_query("MATCH (k:KEP) RETURN k LIMIT $limit", {})
    check_query("MATCH (k:KEP) RETURN k LIMIT $limit", {"limit": 100})


def test_keywords_inside_strings_are_not_false_positives():
    check_query("MATCH (k:KEP) WHERE k.title = 'SET CREATE DELETE' RETURN k LIMIT 5")


def test_allow_listed_procedure_passes():
    check_query("CALL algo.pageRank('KEP', 'SEE_ALSO') YIELD node, score RETURN node LIMIT 5")


def test_timeout_bounds():
    check_timeout(3000)
    for bad in (0, 3001, -1, "3000"):
        with pytest.raises(GuardError):
            check_timeout(bad)
