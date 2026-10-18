"""Integration: re-running ingest must leave the live graph unchanged."""

import pytest

from ingest.config import BRAIN_GRAPH, connect
from ingest.load import graph_counts, load

pytestmark = pytest.mark.integration


def test_ingest_twice_is_idempotent(capsys):
    first = load()
    second = load()
    assert first == second
    graph = connect().select_graph(BRAIN_GRAPH)
    meta = graph.ro_query("MATCH (m:Meta) RETURN count(m), m.ready LIMIT 1").result_set
    assert meta[0] == [1, True]
    assert graph_counts(graph) == second
