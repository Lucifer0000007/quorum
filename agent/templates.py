"""The only Cypher the agent can run: named, parameterized, read-only, LIMIT-capped templates.

Every template is checked by agent.guard at import time. README's query section is generated from this file.
"""

from agent.guard import check_query

BRAIN, EPISODIC = "brain", "episodic"

TEMPLATES: dict[str, dict] = {
    "meta": {
        "graph": BRAIN,
        "doc": "Load metadata and the readiness flag.",
        "cypher": """
MATCH (m:Meta {key: 'brain'})
RETURN m.ready AS ready, m.data_hash AS data_hash, m.source_commit AS source_commit, m.max_minor AS max_minor,
       m.kep_count AS kep_count, m.person_count AS person_count, m.sig_count AS sig_count, m.edge_count AS edge_count
LIMIT 1""",
    },
    "resolve_kep": {
        "graph": BRAIN,
        "doc": "Map a user-supplied KEP number or id to KEP node ids (detects ambiguous numbers).",
        "cypher": """
MATCH (k:KEP)
WHERE k.id = $kep_id OR k.number = $kep_id
RETURN k.id AS id, k.title AS title, k.path AS path
LIMIT 10""",
    },
    "search_keps": {
        "graph": BRAIN,
        "doc": "KEP picker search by number, title or folder.",
        "cypher": """
MATCH (k:KEP)
WHERE k.id = $q OR k.number = $q OR toLower(k.title) CONTAINS toLower($q) OR toLower(k.path) CONTAINS toLower($q)
RETURN k.id AS id, k.title AS title, k.status AS status, k.owning_sig AS owning_sig
ORDER BY CASE WHEN k.id = $q THEN 0 ELSE 1 END, id
LIMIT $limit""",
    },
    "kep_detail": {
        "graph": BRAIN,
        "doc": "KEP metadata, owning/participating SIGs, people by role, and direct REPLACES/SEE_ALSO neighbors.",
        "cypher": """
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:OWNED_BY]->(o:SIG)
OPTIONAL MATCH (k)-[:PARTICIPATES]->(ps:SIG)
WITH k, collect(DISTINCT o.name) AS owning_sigs, collect(DISTINCT ps.name) AS participating_sigs
OPTIONAL MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(k)
WITH k, owning_sigs, participating_sigs, collect(DISTINCT [type(r), p.handle]) AS people
OPTIONAL MATCH (k)-[n:REPLACES|SEE_ALSO]-(m:KEP)
RETURN properties(k) AS kep, owning_sigs, participating_sigs, people,
       collect(DISTINCT [startNode(n).id, type(n), endNode(n).id, m.id, m.title, m.status]) AS neighbors
LIMIT 1""",
    },
    "blast_radius": {
        "graph": BRAIN,
        "doc": "KEPs reachable via REPLACES/SEE_ALSO in either direction within 3 hops, with one shortest path each.",
        "cypher": """
MATCH (k:KEP {id: $kep_id})
MATCH p = (k)-[:REPLACES|SEE_ALSO*1..3]-(o:KEP)
WHERE o.id <> k.id
WITH o, p ORDER BY length(p)
WITH o, collect(p)[0] AS sp
OPTIONAL MATCH (o)-[:OWNED_BY]->(s:SIG)
RETURN o.id AS id, o.title AS title, o.status AS status, s.name AS owning_sig, o.source_url AS source_url,
       length(sp) AS hops, [n IN nodes(sp) | n.id] AS path_nodes,
       [r IN relationships(sp) | [startNode(r).id, type(r), endNode(r).id]] AS path_edges
ORDER BY hops, id
LIMIT 100""",
    },
    "is_still_valid": {
        "graph": BRAIN,
        "doc": "Status, successor chain, predecessors, and stale KEPs this KEP depends on via SEE_ALSO (3 hops).",
        "cypher": """
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH sp = (k)<-[:REPLACES*1..5]-(s:KEP)
WITH k, collect(DISTINCT [s.id, s.title, s.status, length(sp)]) AS successors
OPTIONAL MATCH pp = (k)-[:REPLACES*1..5]->(pr:KEP)
WITH k, successors, collect(DISTINCT [pr.id, pr.title, pr.status, length(pp)]) AS predecessors
OPTIONAL MATCH ap = (k)-[:SEE_ALSO*1..3]->(a:KEP)
WHERE a.status IN $stale_statuses
RETURN k.id AS id, k.title AS title, k.status AS status, k.status_raw AS status_raw, successors, predecessors,
       collect(DISTINCT [a.id, a.title, a.status, [n IN nodes(ap) | n.id]]) AS stale_ancestors
LIMIT 1""",
    },
    "who_knows_sig": {
        "graph": BRAIN,
        "doc": "Rank people by recency-weighted KEP contributions (author/review/approve) in a SIG.",
        "cypher": """
MATCH (s:SIG {name: $sig})<-[:OWNED_BY|PARTICIPATES]-(k:KEP)
WITH DISTINCT k
MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(k)
WITH p, k, type(r) AS role,
     CASE WHEN k.latest_minor IS NOT NULL AND k.latest_minor >= $recent_min_minor THEN 2 ELSE 1 END AS w
RETURN p.handle AS handle, sum(w) AS score, count(DISTINCT k) AS keps,
       sum(CASE WHEN w = 2 THEN 1 ELSE 0 END) AS recent_contributions,
       collect(DISTINCT role) AS roles, collect(DISTINCT k.id)[0..5] AS sample_keps
ORDER BY score DESC, handle
LIMIT $limit""",
    },
    "who_knows_kep": {
        "graph": BRAIN,
        "doc": "Rank people by recency-weighted contributions to a KEP and its 2-hop REPLACES/SEE_ALSO neighborhood.",
        "cypher": """
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:REPLACES|SEE_ALSO*1..2]-(n:KEP)
WITH k, collect(DISTINCT n) + [k] AS hood
UNWIND hood AS h
WITH DISTINCT h
MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(h)
WITH p, h, type(r) AS role,
     CASE WHEN h.latest_minor IS NOT NULL AND h.latest_minor >= $recent_min_minor THEN 2 ELSE 1 END AS w
RETURN p.handle AS handle, sum(w) AS score, count(DISTINCT h) AS keps,
       sum(CASE WHEN w = 2 THEN 1 ELSE 0 END) AS recent_contributions,
       collect(DISTINCT role) AS roles, collect(DISTINCT h.id)[0..5] AS sample_keps
ORDER BY score DESC, handle
LIMIT $limit""",
    },
    "recent_approvers": {
        "graph": BRAIN,
        "doc": "Which of a KEP's approvers are recently active (listed on any KEP with a recent latest-milestone).",
        "cypher": """
MATCH (a:Person)-[:APPROVED]->(k:KEP {id: $kep_id})
OPTIONAL MATCH (a)-[:AUTHORED|REVIEWED|APPROVED]->(r:KEP)
WHERE r.latest_minor IS NOT NULL AND r.latest_minor >= $recent_min_minor
RETURN a.handle AS handle, count(DISTINCT r) AS recent_keps, max(r.latest_milestone) AS latest_seen
ORDER BY handle
LIMIT 100""",
    },
    "why": {
        "graph": BRAIN,
        "doc": "Provenance chain: kep.yaml URL, tracking issue, owning SIG, approvers/authors, KEP lineage.",
        "cypher": """
MATCH (k:KEP {id: $kep_id})
OPTIONAL MATCH (k)-[:OWNED_BY]->(o:SIG)
OPTIONAL MATCH (a:Person)-[:APPROVED]->(k)
WITH k, o, collect(DISTINCT a.handle) AS approvers
OPTIONAL MATCH (au:Person)-[:AUTHORED]->(k)
WITH k, o, approvers, collect(DISTINCT au.handle) AS authors
OPTIONAL MATCH (k)-[l:REPLACES|SEE_ALSO]-(m:KEP)
RETURN k.id AS id, k.title AS title, k.status AS status, k.status_raw AS status_raw,
       k.creation_date AS creation_date, k.latest_milestone AS latest_milestone, k.stage AS stage,
       k.source_url AS source_url, k.issue_url AS issue_url, o.name AS owning_sig, approvers, authors,
       collect(DISTINCT [startNode(l).id, type(l), endNode(l).id, m.title]) AS lineage,
       k.unresolved_refs AS unresolved_refs
LIMIT 1""",
    },
    "referrers": {
        "graph": BRAIN,
        "doc": "KEPs that list this KEP in see-also.",
        "cypher": """
MATCH (k:KEP {id: $kep_id})<-[:SEE_ALSO]-(r:KEP)
RETURN r.id AS id, r.title AS title, r.status AS status, r.owning_sig AS owning_sig, r.source_url AS source_url
ORDER BY id
LIMIT 100""",
    },
    "sig_overview": {
        "graph": BRAIN,
        "doc": "How many KEPs a SIG owns, by status.",
        "cypher": """
MATCH (s:SIG {name: $sig})<-[:OWNED_BY]-(k:KEP)
RETURN s.name AS sig, count(k) AS owned, collect(DISTINCT k.status) AS statuses,
       sum(CASE WHEN k.status IN $active_statuses THEN 1 ELSE 0 END) AS active
LIMIT 1""",
    },
    "list_sigs": {
        "graph": BRAIN,
        "doc": "All SIGs with the number of KEPs they own.",
        "cypher": """
MATCH (s:SIG)
OPTIONAL MATCH (k:KEP)-[:OWNED_BY]->(s)
RETURN s.name AS name, count(k) AS owned
ORDER BY owned DESC, name
LIMIT 100""",
    },
    "central_keps": {
        "graph": BRAIN,
        "doc": "Most central KEPs by PageRank over SEE_ALSO (FalkorDB algorithm procedure).",
        "cypher": """
CALL algo.pageRank('KEP', 'SEE_ALSO') YIELD node, score
RETURN node.id AS id, node.title AS title, node.owning_sig AS owning_sig, score
ORDER BY score DESC
LIMIT $limit""",
    },
    "prior_precedents": {
        "graph": EPISODIC,
        "doc": "Approved precedents previously recorded for this KEP (second graph).",
        "cypher": """
MATCH (s:Session)-[:CHECKED]->(p:Precedent)-[:ABOUT]->(r:KEPRef {id: $kep_id})
RETURN p.id AS id, p.verdict AS verdict, p.created_at AS created_at, s.id AS session_id,
       p.data_hash AS data_hash, p.action AS action
ORDER BY created_at DESC
LIMIT 5""",
    },
}

for _name, _t in TEMPLATES.items():
    _t["cypher"] = _t["cypher"].strip()
    check_query(_t["cypher"], require_params=False)
