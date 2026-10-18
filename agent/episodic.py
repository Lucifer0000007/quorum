"""The approval gate: the ONLY write path of the agent, and it writes only to `agent_episodic_memory`.

Nothing here (or anywhere in Quorum) posts to GitHub or any external service.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.db import db
from ingest.config import EPISODIC_GRAPH

RECORD_PRECEDENT = """
MERGE (s:Session {id: $session_id})
MERGE (p:Precedent {id: $notice_id})
ON CREATE SET p.created_at = $created_at, p.verdict = $verdict, p.kep_id = $kep_id, p.action = $action,
              p.staged_notice = $markdown, p.data_hash = $data_hash
MERGE (s)-[:CHECKED]->(p)
MERGE (r:KEPRef {id: $kep_id})
MERGE (p)-[:ABOUT]->(r)
RETURN p.id AS id, p.created_at AS created_at, p.verdict AS verdict
"""


def record_precedent(notice: dict, session_id: str) -> dict:
    """Idempotent on the notice id: approving the same staged notice twice records one precedent."""
    if notice.get("posted"):
        raise ValueError("notice claims to be posted; Quorum never posts")
    graph = db().select_graph(EPISODIC_GRAPH)
    params = {
        "session_id": session_id,
        "notice_id": notice["id"],
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "verdict": notice["verdict"],
        "kep_id": notice["kep_id"],
        "action": notice["action"],
        "markdown": notice["markdown"],
        "data_hash": notice["data_hash"],
    }
    res = graph.query(RECORD_PRECEDENT, params, timeout=3000)
    row = dict(zip([h[1] for h in res.header], res.result_set[0], strict=False))
    return {"precedent": row, "graph": EPISODIC_GRAPH, "cypher": RECORD_PRECEDENT.strip(),
            "params": {k: v for k, v in params.items() if k != "markdown"}, "posted_externally": False}
