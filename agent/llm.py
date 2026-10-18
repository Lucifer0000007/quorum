"""Optional LLM layer. It may ONLY (1) pick a tool + typed params (validated in agent.router) and
(2) phrase a short summary of a staged notice, which is checked against the notice before display.
It never sees Cypher, never computes a verdict, and is never silently substituted: errors surface to the UI.
"""

from __future__ import annotations

import os
import re

from ingest import config as _config  # noqa: F401  (importing it loads .env before the key and model are read)

SYSTEM = (
    "You route questions about Kubernetes Enhancement Proposals (KEPs) to exactly one tool. "
    "Always answer by calling exactly one tool with its parameters; never answer in prose. "
    "KEP ids are numbers like 1287. SIG names look like sig-node."
)


class LLMError(RuntimeError):
    pass


def model() -> str:
    return os.environ.get("QUORUM_LLM_MODEL", "claude-opus-5-5")


def configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _client():
    if not configured():
        raise LLMError("No ANTHROPIC_API_KEY configured; use the keyword router (all tools still work).")
    import anthropic

    return anthropic.Anthropic(timeout=30.0)


def _tool_defs(tools: dict) -> list[dict]:
    defs = []
    for name, (params_model, _fn, doc) in tools.items():
        schema = params_model.model_json_schema()
        defs.append({
            "name": name,
            "description": doc,
            "strict": True,
            "input_schema": {"type": "object", "properties": schema.get("properties", {}),
                             "required": schema.get("required", []), "additionalProperties": False},
        })
    return defs


def choose_tool(question: str, tools: dict) -> tuple[str, dict]:
    """Ask the model to pick one tool. Claude Opus 5.5 rejects forced tool_choice, so: auto + strict + one call."""
    import anthropic

    try:
        resp = _client().beta.messages.create(
            model=model(),
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM,
            tools=_tool_defs(tools),
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": question}],
        )
    except anthropic.APIStatusError as exc:
        raise LLMError(f"LLM API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"LLM unreachable: {exc}") from exc
    if resp.stop_reason == "refusal":
        raise LLMError("LLM declined to route this question.")
    for block in resp.content:
        if block.type == "tool_use":
            return block.name, dict(block.input)
    text = " ".join(b.text for b in resp.content if b.type == "text")[:200]
    raise LLMError(f"LLM did not choose a tool (said: {text!r}).")


def summarize_notice(markdown: str) -> str:
    """2-3 sentence plain summary of a staged notice. Rejected if it mentions any KEP/SIG/handle not in the notice."""
    import anthropic

    try:
        resp = _client().messages.create(
            model=model(),
            max_tokens=2000,
            output_config={"effort": "low"},
            system="Summarize the impact notice in at most 3 sentences for the affected SIG leads. Use only facts "
                   "stated in the notice. Do not add KEPs, SIGs or people that are not in it.",
            messages=[{"role": "user", "content": markdown}],
        )
    except anthropic.APIError as exc:
        raise LLMError(f"LLM error: {exc}") from exc
    text = " ".join(b.text for b in resp.content if b.type == "text").strip()
    for token in set(re.findall(r"KEP-\d+|sig-[a-z-]+|@[a-z0-9-]+", text, re.IGNORECASE)):
        if token.lower() not in markdown.lower():
            raise LLMError(f"LLM summary mentioned {token!r}, which is not in the staged notice; discarded.")
    return text
