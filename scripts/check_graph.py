"""Fail loudly if the company_brain graph is missing, empty, not ready, or missing labels/edges."""

import sys

from ingest.config import BRAIN_GRAPH, connect
from ingest.load import graph_counts

REQUIRED_LABELS = ("KEP", "Person", "SIG", "Meta")
REQUIRED_EDGES = ("AUTHORED", "REVIEWED", "APPROVED", "OWNED_BY", "SEE_ALSO")


def main() -> int:
    db = connect()
    if BRAIN_GRAPH not in db.list_graphs():
        print(f"FAIL: graph '{BRAIN_GRAPH}' does not exist. Run `make ingest`.")
        return 1
    graph = db.select_graph(BRAIN_GRAPH)
    meta = graph.ro_query("MATCH (m:Meta {key: 'brain'}) RETURN m.ready, m.data_hash, m.kep_count LIMIT 1").result_set
    counts = graph_counts(graph)
    problems = []
    if not meta or meta[0][0] is not True:
        problems.append("Meta.ready is not true (load incomplete or never run)")
    for label in REQUIRED_LABELS:
        if counts.get(f"node:{label}", 0) == 0:
            problems.append(f"no :{label} nodes")
    for rel in REQUIRED_EDGES:
        if counts.get(f"edge:{rel}", 0) == 0:
            problems.append(f"no :{rel} edges")
    if meta and meta[0][2] != counts.get("node:KEP"):
        problems.append(f"Meta.kep_count {meta[0][2]} != KEP nodes {counts.get('node:KEP')}")
    for k, v in counts.items():
        print(f"  {k:22s} {v}")
    if problems:
        print("FAIL: " + "; ".join(problems))
        return 1
    print(f"OK: '{BRAIN_GRAPH}' ready, data_hash {meta[0][1][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
