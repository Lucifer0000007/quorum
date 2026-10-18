"""Test isolation and shared fixtures.

Integration tests approve notices, which writes Precedents. They must never touch the demo's real episodic graph,
so the whole test session runs against a throwaway one. This runs before any test module imports ingest.config.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

TEST_EPISODIC_GRAPH = "agent_episodic_memory_test"
os.environ["QUORUM_EPISODIC_GRAPH"] = TEST_EPISODIC_GRAPH


@pytest.fixture
def stub_anthropic(monkeypatch):
    """A local HTTP server that stands in for the Anthropic API: records requests, replays queued replies.

    Lets the LLM path run end to end through the real SDK without a key, network or cost.
    """
    requests: list[dict] = []
    replies: list[tuple[int, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body})
            status, payload = replies.pop(0) if replies else (
                500, {"type": "error", "error": {"type": "api_error", "message": "stub has no reply queued"}})
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    def message(content: list[dict], stop_reason: str = "end_turn") -> tuple[int, dict]:
        return 200, {"id": "msg_stub", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                     "content": content, "stop_reason": stop_reason, "stop_sequence": None,
                     "usage": {"input_tokens": 10, "output_tokens": 5}}

    class Stub:
        pass

    stub = Stub()
    stub.requests = requests
    stub.reply_tool = lambda name, params: replies.append(
        message([{"type": "tool_use", "id": "toolu_stub", "name": name, "input": params}], "tool_use"))
    stub.reply_text = lambda text: replies.append(message([{"type": "text", "text": text}]))
    stub.reply_refusal = lambda: replies.append(message([], "refusal"))
    stub.reply_error = lambda status, text: replies.append(
        (status, {"type": "error", "error": {"type": "invalid_request_error", "message": text}}))
    yield stub
    server.shutdown()
