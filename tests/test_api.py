"""API flow on the live graph: ask -> preflight -> approve (twice: one precedent) -> prior precedent visible."""

import uuid

import pytest
from fastapi.testclient import TestClient

from agent.api import app

pytestmark = pytest.mark.integration
client = TestClient(app)


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
        ("Simulate superseding KEP-1287", "preflight"),
    ],
)
def test_keyword_router(question, tool):
    r = client.post("/ask", json={"question": question, "router": "keyword"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == tool and body["router"].startswith("keyword")
    assert all(qr["cypher"] for qr in body["queries"])


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
    again = client.post("/preflight", json={"kep_id": "1287"}).json()
    assert any(p["id"] == pf["staged_notice"]["id"] for p in again["prior_precedents"])


def test_unknown_notice_rejected():
    r = client.post("/approve", json={"notice_id": "nope", "session_id": "s"})
    assert r.status_code == 404
