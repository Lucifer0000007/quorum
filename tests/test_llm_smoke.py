"""scripts/llm_smoke.py run against the stub server: its pass/fail/rate-limited logic and the file it writes.

Needs the live graph (the smoke script executes the chosen tools read-only), so it is an integration test."""

import json
import sys

import pytest

from agent import llm
from scripts import llm_smoke
from tests.conftest import STUB_KEY, STUB_MODEL

pytestmark = pytest.mark.integration

HAPPY = [
    ("kep_detail", {"kep_id": "2221"}),
    ("blast_radius", {"kep_id": 4381}),  # integers are accepted and normalized
    ("is_still_valid", {"kep_id": "KEP-2377"}),
    ("who_knows_sig", {"sig": "sig-scheduling"}),
    ("who_knows_kep", {"kep_id": "4381"}),
    ("why", {"kep_id": "2221"}),
    ("referrers", {"kep_id": "1287"}),
    ("sig_overview", {"sig": "sig-node"}),
    ("central_keps", {}),
    ("preflight", {"kep_id": "1287"}),
    ("kep_detail", {"kep_id": "KEP-2008"}),
    ("kep_detail", {"kep_id": "99999"}),
]


def run(monkeypatch, *args) -> int:
    monkeypatch.setattr(sys, "argv", ["llm_smoke", "--delay", "0", *args])
    return llm_smoke.main()


def test_every_tool_is_covered_by_the_cases():
    tools = {c.tool for c in llm_smoke.CASES if c.tool}
    assert tools == set(llm_smoke.TOOLS)
    assert len(llm_smoke.CASES) >= 10
    assert any(c.kind == "not_found" for c in llm_smoke.CASES) and any(c.kind == "no_write" for c in llm_smoke.CASES)


def test_a_well_behaved_model_passes_and_switches_the_app_to_llm_mode(stub_llm, monkeypatch, capsys):
    for tool, params in HAPPY:
        stub_llm.reply_tool(tool, params)
    stub_llm.reply_text("Quorum is read-only, so I can't delete anything.")  # s13: the write attempt
    assert llm.status()["mode"] == "keywords"

    assert run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") >= 13 and "FAIL" not in out and "RATE LIMITED" not in out

    saved = json.loads(llm.SMOKE_PATH.read_text(encoding="utf-8"))
    assert saved["passed"] is True and saved["model"] == STUB_MODEL and saved["real_api"] is True
    assert saved["totals"] == {"questions": 13, "pass": 13, "fail": 0, "rate_limited": 0}
    assert STUB_KEY not in llm.SMOKE_PATH.read_text(encoding="utf-8")
    assert llm.status()["mode"] == "llm"  # the committed evidence is what flips the mode


def test_a_wrong_tool_is_a_failure_and_keeps_the_app_on_keywords(stub_llm, monkeypatch, capsys):
    stub_llm.reply_tool("why", {"kep_id": "2221"})  # s01 expects kep_detail
    assert run(monkeypatch, "--only", "s01") == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "expected kep_detail" in out
    assert not llm.SMOKE_PATH.exists()  # a partial run never writes the evidence file

    stub_llm.reply_tool("why", {"kep_id": "2221"})
    assert run(monkeypatch) == 1  # a full run writes it, with passed false
    assert json.loads(llm.SMOKE_PATH.read_text(encoding="utf-8"))["passed"] is False
    assert llm.status()["mode"] == "keywords"


def test_rate_limiting_is_reported_as_rate_limited_not_as_a_failure(stub_llm, monkeypatch, capsys):
    for _ in range(3):
        stub_llm.reply_error(429, "Rate limit exceeded: free-models-per-day")
    assert run(monkeypatch, "--only", "s01") == 3
    out = capsys.readouterr().out
    assert "RATE LIMITED" in out and "FAIL" not in out and "raw error" in out


def test_persistent_rate_limiting_stops_the_run_instead_of_hammering_the_provider(stub_llm, monkeypatch, capsys):
    for _ in range(6):
        stub_llm.reply_error(429, "Rate limit exceeded")
    assert run(monkeypatch) == 3
    assert len(stub_llm.requests) == 6  # two questions x (1 try + 2 retries); the other 11 were not attempted
    saved = json.loads(llm.SMOKE_PATH.read_text(encoding="utf-8"))
    assert saved["passed"] is False and saved["totals"]["rate_limited"] == 13


@pytest.mark.parametrize("reply", ["text", "read_tool", "unknown_tool", "extra_params"])
def test_a_write_attempt_never_writes_and_passes_only_when_nothing_ran_or_a_read_ran(stub_llm, monkeypatch, capsys,
                                                                                    reply):
    if reply == "text":
        stub_llm.reply_text("I can't delete anything; Quorum is read-only.")
    elif reply == "read_tool":
        stub_llm.reply_tool("kep_detail", {"kep_id": "2221"})
    elif reply == "unknown_tool":
        stub_llm.reply_tool("delete_kep", {"kep_id": "2221"})
    else:
        stub_llm.reply_tool("kep_detail", {"kep_id": "2221", "delete": True})
    assert run(monkeypatch, "--only", "s13") == 0
    assert "PASS" in capsys.readouterr().out


def test_an_invented_kep_must_be_reported_as_not_found(stub_llm, monkeypatch, capsys):
    stub_llm.reply_tool("kep_detail", {"kep_id": "99999"})
    assert run(monkeypatch, "--only", "s12") == 0
    assert "not found" in capsys.readouterr().out.lower()


def test_without_configuration_it_exits_2_and_says_keywords_only(monkeypatch, capsys):
    assert run(monkeypatch) == 2
    assert "Keywords only" in capsys.readouterr().out
