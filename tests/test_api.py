"""API flow on the live graph: ask -> preflight -> approve (twice: one precedent) -> prior precedent visible.

Runs against a throwaway episodic graph (see conftest.py); the demo's real episodic memory is never touched.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from agent.api import app
from ingest import config

pytestmark = pytest.mark.integration
client = TestClient(app)


def _drop_test_graph():
    assert config.EPISODIC_GRAPH.endswith("_test"), "refusing to delete a non-test episodic graph"
    db = config.connect()
    if config.EPISODIC_GRAPH in db.list_graphs():
        db.select_graph(config.EPISODIC_GRAPH).delete()


@pytest.fixture(scope="module", autouse=True)
def _clean_episodic_graph():
    _drop_test_graph()
    yield
    _drop_test_graph()


def test_health_reports_ready_and_llm_state():
    r = client.get("/health").json()
    assert r["graph_ready"] is True and r["counts"]["node:KEP"] == r["meta"]["kep_count"]
    assert "configured" in r["llm"] and "never posts" in r["posting"]


@pytest.mark.parametrize(
    "question,tool",
    [
        ("Which SIG owns KEP-2221?", "kep_detail"),
        ("Does KEP-2377 have a successor?", "is_still_valid"),
        ("Which KEPs list KEP-1287 in see-also?", "referrers"),
        ("How many KEPs does sig-node own?", "sig_overview"),
        ("Who knows about sig-scheduling?", "who_knows_sig"),
        ("What is the blast radius of KEP-4381?", "blast_radius"),
        ("Why does KEP-2221 exist?", "why"),
        ("Which KEPs are the most central?", "central_keps"),
        ("Simulate superseding KEP-1287", "preflight"),
    ],
)
def test_keyword_router(question, tool):
    r = client.post("/ask", json={"question": question, "router": "keyword"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == tool and body["router"].startswith("keyword")
    assert all(qr["cypher"] for qr in body["queries"])


def test_central_keps_uses_pagerank_procedure():
    body = client.post("/ask", json={"question": "Which KEPs are the most central?", "router": "keyword"}).json()
    assert "algo.pageRank" in body["queries"][-1]["cypher"]
    assert body["result"] and all(row["score"] > 0 for row in body["result"])


def test_unroutable_question_is_an_error_not_a_guess():
    r = client.post("/ask", json={"question": "tell me something nice", "router": "keyword"})
    assert r.status_code == 422 and r.json()["error"] == "routing_error"


def test_preflight_approve_is_idempotent_and_recorded():
    pf = client.post("/preflight", json={"kep_id": "1287"}).json()
    assert pf["verdict"] == "HUMAN_GATE_REQUIRED" and pf["staged_notice"]["posted"] is False
    session = f"test-{uuid.uuid4().hex[:8]}"
    a1 = client.post("/approve", json={"notice_id": pf["staged_notice"]["id"], "session_id": session}).json()
    a2 = client.post("/approve", json={"notice_id": pf["staged_notice"]["id"], "session_id": session}).json()
    assert a1["precedent"]["id"] == a2["precedent"]["id"] and a1["posted_externally"] is False
    assert a1["graph"] == config.EPISODIC_GRAPH
    again = client.post("/preflight", json={"kep_id": "1287"}).json()
    assert any(p["id"] == pf["staged_notice"]["id"] for p in again["prior_precedents"])
    # exactly one precedent node and one session edge exist for the double approval
    g = config.connect().select_graph(config.EPISODIC_GRAPH)
    count = g.ro_query("MATCH (p:Precedent {id: $id}) RETURN count(p)", {"id": a1["precedent"]["id"]})
    assert count.result_set == [[1]]


def test_unknown_notice_rejected():
    r = client.post("/approve", json={"notice_id": "nope", "session_id": "s"})
    assert r.status_code == 404


def test_llm_router_picks_tool_and_code_validates_params(stub_anthropic):
    stub_anthropic.reply_tool("kep_detail", {"kep_id": "KEP-2221"})  # the model's raw output is normalized by code
    body = client.post("/ask", json={"question": "tell me about the dockershim removal decision",
                                     "router": "llm"}).json()
    assert body["tool"] == "kep_detail" and body["tool_params"] == {"kep_id": "2221"}
    assert body["router"].startswith("llm (")
    assert body["result"]["kep"]["id"] == "2221"
    sent = stub_anthropic.requests[0]["body"]
    assert sent["tool_choice"]["type"] == "auto"  # forced tool_choice is rejected by this model family


def test_llm_router_rejects_invalid_params_from_the_model(stub_anthropic):
    stub_anthropic.reply_tool("kep_detail", {"kep_id": "1; MATCH (n) DETACH DELETE n"})
    r = client.post("/ask", json={"question": "anything", "router": "llm"})
    assert r.status_code == 422 and r.json()["error"] == "routing_error"


def test_llm_router_rejects_unknown_tool_from_the_model(stub_anthropic):
    stub_anthropic.reply_tool("run_cypher", {"query": "MATCH (n) RETURN n"})
    r = client.post("/ask", json={"question": "anything", "router": "llm"})
    assert r.status_code == 422 and "unknown tool" in r.json()["detail"]


def test_llm_failure_is_surfaced_not_replaced_by_keywords(stub_anthropic):
    stub_anthropic.reply_error(400, "model overloaded")
    r = client.post("/ask", json={"question": "Which SIG owns KEP-2221?", "router": "auto"})
    assert r.status_code == 502 and r.json()["error"] == "llm_error"
    assert "400" in r.json()["detail"]
