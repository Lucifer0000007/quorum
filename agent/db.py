"""Read-only execution of named templates via GRAPH.RO_QUERY with a timeout. Refuses to run on an unready graph."""

from __future__ import annotations

import time

from agent.guard import MAX_TIMEOUT_MS, check_query, check_timeout
from agent.templates import BRAIN, EPISODIC, TEMPLATES
from ingest.config import BRAIN_GRAPH, EPISODIC_GRAPH, connect

TIMEOUT_MS = MAX_TIMEOUT_MS
_GRAPHS = {BRAIN: BRAIN_GRAPH, EPISODIC: EPISODIC_GRAPH}
_db = None
_ready_cache: tuple[float, dict] | None = None


class GraphNotReady(RuntimeError):
    """The company_brain graph is missing or Meta.ready is not true."""


def db():
    global _db
    if _db is None:
        _db = connect()
    return _db


def _execute(name: str, params: dict) -> dict:
    template = TEMPLATES[name]  # unknown names raise KeyError: only registered templates can run
    cypher = template["cypher"]
    check_query(cypher, params)
    check_timeout(TIMEOUT_MS)
    graph_name = _GRAPHS[template["graph"]]
    t0 = time.perf_counter()
    if template["graph"] == EPISODIC and graph_name not in db().list_graphs():
        rows = []  # nothing has ever been approved: a real empty result
    else:
        res = db().select_graph(graph_name).ro_query(cypher, params, timeout=TIMEOUT_MS)
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        rows = [dict(zip(cols, row, strict=False)) for row in res.result_set]
    return {
        "name": name,
        "graph": graph_name,
        "cypher": cypher,
        "params": params,
        "rows": rows,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def meta(force: bool = False) -> dict:
    """Meta row of company_brain, cached for 5 s. Raises GraphNotReady if missing/not ready."""
    global _ready_cache
    if not force and _ready_cache and time.time() - _ready_cache[0] < 5:
        return _ready_cache[1]
    if BRAIN_GRAPH not in db().list_graphs():
        raise GraphNotReady(f"Graph '{BRAIN_GRAPH}' does not exist. Run `make ingest`.")
    rows = _execute("meta", {})["rows"]
    if not rows or rows[0].get("ready") is not True:
        raise GraphNotReady(f"Graph '{BRAIN_GRAPH}' is not ready (Meta.ready != true). Run `make ingest`.")
    _ready_cache = (time.time(), rows[0])
    return rows[0]


def run_ro(name: str, params: dict | None = None) -> dict:
    """Run a registered template read-only. Always checks readiness first."""
    meta()
    return _execute(name, dict(params or {}))
