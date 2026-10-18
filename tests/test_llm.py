"""The optional LLM layer: provider-agnostic adapter + mode gating, tested through the real `openai` client against a
local stub server (no key, no network, no cost). See conftest.py for the stub and the guard against the real key."""

import json

import pytest

from agent import llm
from agent.router import TOOLS
from tests.conftest import STUB_KEY, STUB_MODEL


def write_smoke(**overrides):
    smoke = {"passed": True, "model": STUB_MODEL, "base_url_host": "127.0.0.1", "provider": "127.0.0.1",
             "ran_at": "2026-01-01T00:00:00+00:00",
             "totals": {"questions": 13, "pass": 13, "fail": 0, "rate_limited": 0}} | overrides
    llm.SMOKE_PATH.write_text(json.dumps(smoke), encoding="utf-8")


# ---------------------------------------------------------------- configuration and mode


def test_unconfigured_reports_every_missing_variable_and_uses_keywords():
    s = llm.status()
    assert s["mode"] == "keywords" and s["label"] == "Keywords only" and s["configured"] is False
    for name in llm.ENV_VARS:
        assert name in s["reason"]
    with pytest.raises(llm.LLMError, match="LLM_API_KEY"):
        llm.choose_tool("anything", TOOLS)


def test_a_partial_configuration_is_not_configured(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    assert llm.configured() is False and llm.missing() == ["LLM_BASE_URL"]  # no default URL: a key never goes astray


def test_mode_is_keywords_until_the_smoke_test_has_passed(stub_llm):
    s = llm.status()
    assert s["mode"] == "keywords" and "smoke test has not been run" in s["reason"]
    assert s["provider"] == "127.0.0.1" and s["model"] == STUB_MODEL


def test_mode_is_llm_after_a_fully_passing_smoke_test_for_this_model(stub_llm):
    write_smoke()
    s = llm.status()
    assert s["mode"] == "llm" and s["label"] == "LLM" and s["reason"] == ""
    assert s["smoke"]["pass"] == 13


@pytest.mark.parametrize("override,expected", [
    ({"passed": False, "totals": {"questions": 13, "pass": 12, "fail": 1, "rate_limited": 0}}, "did not fully pass"),
    ({"passed": False, "totals": {"questions": 13, "pass": 12, "fail": 0, "rate_limited": 1}}, "did not fully pass"),
    ({"model": "some-other-model"}, "not the configured model"),
    ({"base_url_host": "example.org"}, "not the configured model"),
])
def test_any_smoke_failure_or_mismatch_defaults_to_keywords(stub_llm, override, expected):
    write_smoke(**override)
    s = llm.status()
    assert s["mode"] == "keywords" and expected in s["reason"]


def test_unreadable_smoke_file_defaults_to_keywords(stub_llm):
    llm.SMOKE_PATH.write_text("{not json", encoding="utf-8")
    assert llm.status()["mode"] == "keywords"


def test_disabling_turns_the_llm_off_with_a_reason(stub_llm):
    write_smoke()
    llm.disable("model X does not support tool calling")
    s = llm.status()
    assert s["mode"] == "keywords" and "does not support tool calling" in s["reason"]


def test_status_never_contains_the_key(stub_llm):
    write_smoke()
    assert STUB_KEY not in json.dumps(llm.status())


# ---------------------------------------------------------------- the request the adapter sends


def test_choose_tool_request_shape(stub_llm):
    stub_llm.reply_tool("blast_radius", {"kep_id": "4381"})
    assert llm.choose_tool("what is reachable from DRA?", TOOLS) == ("blast_radius", {"kep_id": "4381"})

    req = stub_llm.requests[0]
    body = req["body"]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == f"Bearer {STUB_KEY}"
    assert body["model"] == STUB_MODEL and body["tool_choice"] == "auto"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"] == "what is reachable from DRA?"
    assert {t["function"]["name"] for t in body["tools"]} == set(TOOLS)
    assert all(t["type"] == "function" for t in body["tools"])
    by_name = {t["function"]["name"]: t["function"] for t in body["tools"]}
    kep = by_name["blast_radius"]["parameters"]
    assert kep["required"] == ["kep_id"] and kep["properties"]["kep_id"]["type"] == "string"
    assert "parameters" not in by_name["central_keps"]  # a tool without parameters omits the schema
    assert "title" not in json.dumps(body["tools"])  # providers differ on JSON Schema extras: keep it minimal


def test_the_model_is_never_shown_cypher(stub_llm):
    stub_llm.reply_tool("kep_detail", {"kep_id": "1"})
    llm.choose_tool("hi", TOOLS)
    sent = json.dumps(stub_llm.requests[0]["body"])
    for needle in ("MATCH (", "RETURN ", "MERGE", "GRAPH.", "RO_QUERY"):
        assert needle not in sent


# ---------------------------------------------------------------- what comes back


def test_no_tool_call_is_reported_not_guessed(stub_llm):
    stub_llm.reply_text("I think the answer is sig-node.")
    with pytest.raises(llm.LLMError, match="did not choose a tool"):
        llm.choose_tool("x", TOOLS)


def test_a_content_filter_decline_is_reported(stub_llm):
    stub_llm.reply_text("", finish="content_filter")
    with pytest.raises(llm.LLMError, match="declined"):
        llm.choose_tool("x", TOOLS)


def test_more_than_one_tool_call_is_rejected(stub_llm):
    stub_llm.reply_tools([("kep_detail", {"kep_id": "1"}), ("why", {"kep_id": "2"})])
    with pytest.raises(llm.LLMError, match="exactly one"):
        llm.choose_tool("x", TOOLS)


@pytest.mark.parametrize("arguments,expected", [("not json", "not valid JSON"), ("[1, 2]", "JSON object")])
def test_malformed_arguments_are_rejected(stub_llm, arguments, expected):
    stub_llm.reply_raw_tool("kep_detail", arguments)
    with pytest.raises(llm.LLMError, match=expected):
        llm.choose_tool("x", TOOLS)


# ---------------------------------------------------------------- failures


def test_a_model_without_tool_calling_has_its_own_error(stub_llm):
    stub_llm.reply_error(404, "No endpoints found that support tool use. Try disabling `tools`.")
    with pytest.raises(llm.ToolCallingUnsupported, match="does not support tool calling"):
        llm.choose_tool("x", TOOLS)


def test_an_unrelated_bad_request_is_not_mistaken_for_missing_tool_support(stub_llm):
    stub_llm.reply_error(400, "Invalid schema for function 'kep_detail': missing type")
    with pytest.raises(llm.LLMError, match="HTTP 400") as exc:
        llm.choose_tool("x", TOOLS)
    assert not isinstance(exc.value, llm.ToolCallingUnsupported)


def test_bad_credentials_are_reported_without_the_key(stub_llm):
    stub_llm.reply_error(401, f"Invalid key {STUB_KEY}")
    with pytest.raises(llm.LLMError, match="LLM_API_KEY") as exc:
        llm.choose_tool("x", TOOLS)
    assert STUB_KEY not in str(exc.value) and "***" in str(exc.value)


def test_a_rate_limit_is_retried_and_can_succeed(stub_llm):
    stub_llm.reply_error(429, "slow down")
    stub_llm.reply_tool("kep_detail", {"kep_id": "2221"})
    assert llm.choose_tool("x", TOOLS) == ("kep_detail", {"kep_id": "2221"})
    assert len(stub_llm.requests) == 2


def test_a_persistent_rate_limit_is_reported_as_rate_limited(stub_llm):
    for _ in range(3):
        stub_llm.reply_error(429, "Rate limit exceeded: free-models-per-day")
    with pytest.raises(llm.RateLimited, match="rate limited"):
        llm.choose_tool("x", TOOLS)
    assert len(stub_llm.requests) == 3  # one try plus two short retries, then it gives up


def test_a_gateway_error_inside_an_http_200_is_reported(stub_llm):
    stub_llm.reply_body(200, {"error": {"message": "upstream provider failed", "code": 502}})
    with pytest.raises(llm.LLMError, match="upstream provider failed"):
        llm.choose_tool("x", TOOLS)


def test_error_text_never_contains_the_api_key(stub_llm):
    stub_llm.reply_error(400, f"echoing the key {STUB_KEY} back")
    with pytest.raises(llm.LLMError) as exc:
        llm.choose_tool("x", TOOLS)
    assert STUB_KEY not in str(exc.value)


def test_unreachable_provider_is_reported(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")  # nothing listens on the discard port
    monkeypatch.setenv("LLM_API_KEY", STUB_KEY)
    monkeypatch.setenv("LLM_MODEL", STUB_MODEL)
    with pytest.raises(llm.LLMError, match="unreachable"):
        llm.choose_tool("x", TOOLS)


# ---------------------------------------------------------------- notice summaries


NOTICE = (
    "STAGED. Proposal to supersede KEP-4381. Verdict HUMAN_GATE_REQUIRED. "
    "SIGs: sig-node, sig-scheduling. @dchen1107"
)


def test_notice_summary_passes_when_it_only_uses_notice_facts(stub_llm):
    stub_llm.reply_text("Superseding KEP-4381 needs sign-off from sig-node and sig-scheduling (@dchen1107).")
    assert "KEP-4381" in llm.summarize_notice(NOTICE)


@pytest.mark.parametrize("invented", ["KEP-9999", "sig-network", "@someone-else"])
def test_notice_summary_is_discarded_when_it_invents_entities(stub_llm, invented):
    stub_llm.reply_text(f"Superseding KEP-4381 also affects {invented}.")
    with pytest.raises(llm.LLMError, match="not in the staged notice"):
        llm.summarize_notice(NOTICE)
