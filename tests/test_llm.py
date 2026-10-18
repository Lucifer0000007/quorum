"""The optional LLM layer, exercised through the real Anthropic SDK against a local stub server (no key, no cost)."""

import json

import pytest

from agent import llm
from agent.router import TOOLS


def test_unconfigured_llm_says_so_and_names_the_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert llm.configured() is False
    with pytest.raises(llm.LLMError, match="keyword router"):
        llm.choose_tool("anything", TOOLS)


def test_choose_tool_request_shape(stub_anthropic):
    stub_anthropic.reply_tool("blast_radius", {"kep_id": "4381"})
    tool, params = llm.choose_tool("what breaks if we drop DRA?", TOOLS)
    assert (tool, params) == ("blast_radius", {"kep_id": "4381"})

    req = stub_anthropic.requests[0]
    body = req["body"]
    assert req["path"].startswith("/v1/messages")
    assert body["model"] == llm.model()
    assert body["tool_choice"]["type"] == "auto"  # forced tool use is a 400 on this model family
    assert body["tool_choice"].get("disable_parallel_tool_use") is True
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"]["anthropic-beta"]
    assert req["headers"]["x-api-key"] == "test-key"
    names = {t["name"] for t in body["tools"]}
    assert names == set(TOOLS)
    for t in body["tools"]:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False
    assert body["messages"] == [{"role": "user", "content": "what breaks if we drop DRA?"}]


def test_the_model_is_never_shown_cypher(stub_anthropic):
    stub_anthropic.reply_tool("kep_detail", {"kep_id": "1"})
    llm.choose_tool("hi", TOOLS)
    sent = json.dumps(stub_anthropic.requests[0]["body"])
    for needle in ("MATCH (", "RETURN ", "MERGE", "GRAPH.", "RO_QUERY"):
        assert needle not in sent


def test_refusal_is_reported(stub_anthropic):
    stub_anthropic.reply_refusal()
    with pytest.raises(llm.LLMError, match="declined"):
        llm.choose_tool("x", TOOLS)


def test_prose_instead_of_a_tool_call_is_reported(stub_anthropic):
    stub_anthropic.reply_text("I think the answer is sig-node.")
    with pytest.raises(llm.LLMError, match="did not choose a tool"):
        llm.choose_tool("x", TOOLS)


def test_api_error_is_reported_with_status(stub_anthropic):
    stub_anthropic.reply_error(400, "bad things")
    with pytest.raises(llm.LLMError, match="400"):
        llm.choose_tool("x", TOOLS)


NOTICE = (
    "STAGED. Proposal to supersede KEP-4381. Verdict HUMAN_GATE_REQUIRED. "
    "SIGs: sig-node, sig-scheduling. @dchen1107"
)


def test_notice_summary_passes_when_it_only_uses_notice_facts(stub_anthropic):
    stub_anthropic.reply_text("Superseding KEP-4381 needs sign-off from sig-node and sig-scheduling (@dchen1107).")
    assert "KEP-4381" in llm.summarize_notice(NOTICE)


@pytest.mark.parametrize("invented", ["KEP-9999", "sig-network", "@someone-else"])
def test_notice_summary_is_discarded_when_it_invents_entities(stub_anthropic, invented):
    stub_anthropic.reply_text(f"Superseding KEP-4381 also affects {invented}.")
    with pytest.raises(llm.LLMError, match="not in the staged notice"):
        llm.summarize_notice(NOTICE)
