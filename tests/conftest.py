"""Test isolation and shared fixtures.

* Integration tests approve notices, which writes Precedents. They must never touch the demo's real episodic graph,
  so the whole session runs against a throwaway one. This runs before any test module imports ingest.config.
* The developer's .env (which may hold a real LLM key) is loaded into the process on import. Tests must never see or
  use it: an autouse fixture clears every LLM_* variable, so only the local stub server below can be reached.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

TEST_EPISODIC_GRAPH = "agent_episodic_memory_test"
os.environ["QUORUM_EPISODIC_GRAPH"] = TEST_EPISODIC_GRAPH

LLM_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
STUB_KEY = "test-key-not-a-secret"
STUB_MODEL = "stub-model"


@pytest.fixture(autouse=True)
def _never_use_the_real_llm(monkeypatch, tmp_path):
    from agent import llm

    for name in LLM_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "SMOKE_PATH", tmp_path / "llm_smoke.json")  # never read the committed smoke result
    monkeypatch.setattr(llm, "RETRY_DELAYS", (0.0, 0.0))  # keep 429 tests fast
    llm.reset()
    yield
    llm.reset()


@pytest.fixture
def stub_llm(monkeypatch):
    """A local server speaking the OpenAI-compatible chat-completions protocol.

    It records every request and replays queued replies, so the real `openai` client, the adapter and the API run end
    to end without a key, network or cost. LLM_BASE_URL/LLM_API_KEY/LLM_MODEL point at it for the test.
    """
    requests: list[dict] = []
    replies: list[tuple[int, dict, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body})
            status, payload, headers = replies.pop(0) if replies else (
                500, {"error": {"message": "stub has no reply queued", "code": 500}}, {})
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_API_KEY", STUB_KEY)
    monkeypatch.setenv("LLM_MODEL", STUB_MODEL)

    def completion(message: dict, finish_reason: str = "stop") -> tuple[int, dict, dict]:
        return 200, {"id": "chatcmpl-stub", "object": "chat.completion", "created": 0, "model": STUB_MODEL,
                     "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, {}

    def call(name: str, arguments: str, index: int = 1) -> dict:
        return {"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": arguments}}

    class Stub:
        pass

    stub = Stub()
    stub.requests = requests
    stub.reply_tool = lambda name, params: replies.append(completion(
        {"role": "assistant", "content": None, "tool_calls": [call(name, json.dumps(params))]}, "tool_calls"))
    stub.reply_raw_tool = lambda name, arguments: replies.append(completion(
        {"role": "assistant", "content": None, "tool_calls": [call(name, arguments)]}, "tool_calls"))
    stub.reply_tools = lambda pairs: replies.append(completion(
        {"role": "assistant", "content": None,
         "tool_calls": [call(n, json.dumps(p), i) for i, (n, p) in enumerate(pairs, 1)]}, "tool_calls"))
    stub.reply_text = lambda text, finish="stop": replies.append(
        completion({"role": "assistant", "content": text}, finish))
    stub.reply_error = lambda status, message, headers=None: replies.append(
        (status, {"error": {"message": message, "type": "error", "code": status}}, headers or {}))
    stub.reply_body = lambda status, payload: replies.append((status, payload, {}))
    yield stub
    server.shutdown()
