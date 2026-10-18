"""Idempotent loader: kep.yaml corpus -> FalkorDB graph `company_brain` using UNWIND ... MERGE in batches.

Re-running with the same data leaves the graph unchanged (same nodes, edges and properties).
Usage: python -m ingest.load [--rebuild]
"""

from __future__ import annotations

import subprocess
import sys
import time

from ingest.config import BRAIN_GRAPH, ENHANCEMENTS_DIR, connect
from ingest.parse import build_rows, parse_corpus

BATCH = 300

NODE_QUERIES = {
    "sigs": "UNWIND $rows AS r MERGE (:SIG {name: r.name})",
    "people": "UNWIND $rows AS r MERGE (:Person {handle: r.handle})",
    "keps": """
UNWIND $rows AS r
MERGE (k:KEP {id: r.id})
SET k.path = r.path, k.number = r.number, k.title = r.title, k.status = r.status, k.status_raw = r.status_raw,
    k.stage = r.stage, k.latest_milestone = r.latest_milestone, k.latest_minor = r.latest_minor,
    k.creation_date = r.creation_date, k.owning_sig = r.owning_sig, k.source_url = r.source_url,
    k.issue_url = r.issue_url, k.unresolved_refs = r.unresolved_refs
""",
}

# Relationship types cannot be parameters in Cypher, so each type has its own fixed query.
_PERSON_EDGE = ("UNWIND $rows AS r MATCH (p:Person {{handle: r.src}}) MATCH (k:KEP {{id: r.dst}}) "
                "MERGE (p)-[:{rel}]->(k)")
EDGE_QUERIES = {
    "AUTHORED": _PERSON_EDGE.format(rel="AUTHORED"),
    "REVIEWED": _PERSON_EDGE.format(rel="REVIEWED"),
    "APPROVED": _PERSON_EDGE.format(rel="APPROVED"),
    "OWNED_BY": "UNWIND $rows AS r MATCH (k:KEP {id: r.src}) MATCH (s:SIG {name: r.dst}) MERGE (k)-[:OWNED_BY]->(s)",
    "PARTICIPATES": "UNWIND $rows AS r MATCH (k:KEP {id: r.src}) MATCH (s:SIG {name: r.dst}) "
    "MERGE (k)-[:PARTICIPATES]->(s)",
    "SEE_ALSO": "UNWIND $rows AS r MATCH (a:KEP {id: r.src}) MATCH (b:KEP {id: r.dst}) MERGE (a)-[:SEE_ALSO]->(b)",
    "REPLACES": "UNWIND $rows AS r MATCH (a:KEP {id: r.src}) MATCH (b:KEP {id: r.dst}) MERGE (a)-[:REPLACES]->(b)",
}

INDEXES = [("KEP", "id"), ("Person", "handle"), ("SIG", "name"), ("Meta", "key")]

META_QUERY = """
MERGE (m:Meta {key: 'brain'})
SET m.data_hash = $data_hash, m.source_commit = $source_commit, m.max_minor = $max_minor,
    m.kep_count = $kep_count, m.person_count = $person_count, m.sig_count = $sig_count,
    m.edge_count = $edge_count, m.ready = true
"""


def source_commit() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ENHANCEMENTS_DIR), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def graph_counts(graph) -> dict:
    """Label and relationship-type counts (used to prove idempotency)."""
    counts = {}
    for row in graph.ro_query("MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY l").result_set:
        counts[f"node:{row[0]}"] = row[1]
    for row in graph.ro_query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t").result_set:
        counts[f"edge:{row[0]}"] = row[1]
    return counts


def ensure_indexes(graph) -> None:
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as exc:
            if "already indexed" not in str(exc).lower():
                raise


def run_batches(graph, query: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), BATCH):
        graph.query(query, {"rows": rows[i:i + BATCH]})


def load(rebuild: bool = False) -> dict:
    if not (ENHANCEMENTS_DIR / "keps").exists():
        raise SystemExit(f"Corpus not found at {ENHANCEMENTS_DIR}. Run `make clone-data` first.")
    records, failures = parse_corpus(ENHANCEMENTS_DIR)
    if failures:
        raise SystemExit(f"{len(failures)} kep.yaml files failed to parse; refusing a partial load: {failures[:3]}")
    built = build_rows(records, source_commit())
    rows, meta = built["rows"], built["meta"]

    db = connect()
    graph = db.select_graph(BRAIN_GRAPH)
    existing = graph.ro_query("MATCH (m:Meta {key: 'brain'}) RETURN m.data_hash LIMIT 1").result_set \
        if BRAIN_GRAPH in db.list_graphs() else []
    if existing and existing[0][0] != meta["data_hash"] and not rebuild:
        print(f"NOTE: data changed (graph {existing[0][0][:12]} -> source {meta['data_hash'][:12]}). MERGE adds/"
              "updates but never deletes; use --rebuild to drop stale nodes/edges.")
    if rebuild and BRAIN_GRAPH in db.list_graphs():
        graph.delete()
        graph = db.select_graph(BRAIN_GRAPH)

    t0 = time.time()
    graph.query("MERGE (m:Meta {key: 'brain'}) SET m.ready = false")
    ensure_indexes(graph)
    for key, query in NODE_QUERIES.items():
        run_batches(graph, query, rows[key])
    for rel, query in EDGE_QUERIES.items():
        run_batches(graph, query, rows["edges"][rel])
    graph.query(META_QUERY, meta)
    counts = graph_counts(graph)
    print(f"Loaded {meta['kep_count']} KEPs into '{BRAIN_GRAPH}' in {time.time() - t0:.1f}s "
          f"(data_hash {meta['data_hash'][:12]}, commit {str(meta['source_commit'])[:12]})")
    for k, v in counts.items():
        print(f"  {k:22s} {v}")
    print("Parse stats:", built["stats"])
    return counts


if __name__ == "__main__":
    load(rebuild="--rebuild" in sys.argv)
