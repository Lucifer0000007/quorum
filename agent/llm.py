"""Optional LLM layer, provider-agnostic: any OpenAI-compatible chat-completions API with tool calling.

Configuration comes ONLY from environment variables, loaded from .env by `ingest.config` before first use:

    LLM_BASE_URL   e.g. https://openrouter.ai/api/v1
    LLM_API_KEY    never logged, printed, stored or returned by this module (see `scrub`)
    LLM_MODEL      a model id that supports tool calling

All three are required. There is deliberately no default base URL, so a key can never be sent to a provider the
operator did not name.

What the model may do: (1) pick one tool and fill typed parameters (validated in agent.router), and (2) phrase a
short summary of a staged notice, which is checked against the notice before it is shown. It never sees Cypher,
never computes a verdict, and has no write tool.

What happens when it can't be used is explicit, never silent: no key or no passing smoke test -> "Keywords only"
(see `status`); a model without tool calling -> loud error, then "Keywords only"; rate limiting -> a short retry,
then a "rate limited" error.
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlparse

from ingest import config as _config  # noqa: F401  (importing it loads .env before any variable is read)
from ingest.config import ROOT

ENV_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
SMOKE_PATH = ROOT / "evals" / "llm_smoke.json"
RETRY_DELAYS = (1.5, 3.0)  # seconds to wait before the 2nd and 3rd attempt after an HTTP 429
MAX_RETRY_AFTER = 10.0  # never wait longer than this on a Retry-After header
TIMEOUT_S = 30.0
KNOWN_PROVIDERS = {
    "openrouter.ai": "OpenRouter",
    "generativelanguage.googleapis.com": "Google Gemini",
    "api.openai.com": "OpenAI",
}

SYSTEM = (
    "You route read-only questions about Kubernetes Enhancement Proposals (KEPs) to tools. "
    "For every question answer by calling exactly one tool with its parameters, never in prose. "
    "KEP ids are numbers like 1287. SIG names look like sig-node. "
    "Quorum cannot create, change or delete anything: if the user asks for that, call no tool and say it is read-only."
)

_UNSUPPORTED = re.compile(
    r"no endpoints found that support tool|does not support (tools?|tool use|function)|"
    r"tool use is not supported|tools? (is|are) not supported|function calling.{0,40}(not|un)supported|"
    r"not support(ed)?.{0,30}(tool|function call)",
    re.IGNORECASE,
)
_disabled_reason: str | None = None


class LLMError(RuntimeError):
    """The LLM could not be used for this request. The message is safe to show (the key is scrubbed)."""


class RateLimited(LLMError):
    """HTTP 429 persisted through the short retry."""


class AuthRejected(LLMError):
    """The provider rejected the credentials (HTTP 401 or 403), e.g. after the key was rotated."""


class ToolCallingUnsupported(LLMError):
    """The configured model/endpoint does not support tool calling."""


# ---------------------------------------------------------------- configuration


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def missing() -> list[str]:
    return [n for n in ENV_VARS if not _env(n)]


def configured() -> bool:
    return not missing()


def model() -> str:
    return _env("LLM_MODEL")


def base_url() -> str:
    return _env("LLM_BASE_URL")


def host() -> str:
    return (urlparse(base_url()).hostname or "").lower()


def provider() -> str:
    return KNOWN_PROVIDERS.get(host(), host() or "unknown provider")


def scrub(text: object) -> str:
    """Remove the API key (and provider account ids echoed in error bodies) before text is shown, logged or saved."""
    s = str(text)
    key = _env("LLM_API_KEY")
    s = s.replace(key, "***") if key else s
    return re.sub(r"\buser_[0-9A-Za-z]{16,}", "user_***", s)


# ---------------------------------------------------------------- mode


def load_smoke() -> dict | None:
    try:
        return json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}


def disable(reason: str) -> None:
    """Turn the LLM off for this process (used when the model turns out not to support tool calling)."""
    global _disabled_reason
    _disabled_reason = scrub(reason)


def reset() -> None:
    global _disabled_reason
    _disabled_reason = None


def status() -> dict:
    """The active mode and why. mode is "llm" only if configured AND the smoke test passed for THIS provider+model."""
    out = {"mode": "keywords", "label": "Keywords only", "configured": configured(), "provider": None,
           "model": None, "reason": "", "smoke": None}
    miss = missing()
    if miss:
        out["reason"] = "no LLM configured (missing " + ", ".join(miss) + ")"
        return out
    out |= {"provider": provider(), "model": model()}
    smoke = load_smoke()
    if smoke and not smoke.get("unreadable"):
        t = smoke.get("totals", {})
        out["smoke"] = {"passed": bool(smoke.get("passed")), "pass": t.get("pass"), "fail": t.get("fail"),
                        "rate_limited": t.get("rate_limited"), "questions": t.get("questions"),
                        "ran_at": smoke.get("ran_at"), "model": smoke.get("model"), "provider": smoke.get("provider")}
    if _disabled_reason:
        out["reason"] = _disabled_reason
    elif smoke is None:
        out["reason"] = "the LLM smoke test has not been run (python -m scripts.llm_smoke)"
    elif smoke.get("unreadable"):
        out["reason"] = "evals/llm_smoke.json is unreadable; re-run python -m scripts.llm_smoke"
    elif smoke.get("model") != model() or smoke.get("base_url_host") != host():
        out["reason"] = (f"the last smoke test was for {smoke.get('model')} on {smoke.get('base_url_host')}, "
                         "not the configured model; re-run python -m scripts.llm_smoke")
    elif not smoke.get("passed"):
        t = smoke.get("totals", {})
        out["reason"] = (f"the last smoke test did not fully pass ({t.get('pass')} passed, {t.get('fail')} failed, "
                         f"{t.get('rate_limited')} rate limited of {t.get('questions')})")
    else:
        out |= {"mode": "llm", "label": "LLM", "reason": ""}
    return out


# ---------------------------------------------------------------- calls


def _client():
    if not configured():
        raise LLMError("No LLM configured (missing " + ", ".join(missing()) + "); use the keyword router.")
    from openai import OpenAI

    # max_retries=0: 429s are retried below, in one place, so they can be reported as "rate limited".
    return OpenAI(base_url=base_url(), api_key=_env("LLM_API_KEY"), timeout=TIMEOUT_S, max_retries=0)


def _message(exc: Exception) -> str:
    return scrub(getattr(exc, "message", None) or str(exc))


def _retry_after(exc: Exception, default: float) -> float:
    try:
        value = float(exc.response.headers.get("retry-after", ""))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return default
    return max(0.0, min(value, MAX_RETRY_AFTER))


def _complete(**kwargs):
    """chat.completions.create with a short, bounded retry on HTTP 429 and errors mapped to LLMError subclasses."""
    import openai

    client = _client()
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            resp = client.chat.completions.create(model=model(), **kwargs)
        except openai.RateLimitError as exc:
            if attempt < attempts - 1:
                time.sleep(_retry_after(exc, RETRY_DELAYS[attempt]))
                continue
            raise RateLimited(
                f"rate limited by {provider()} (HTTP 429) after {attempts} attempts: {_message(exc)}") from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise AuthRejected(
                f"{provider()} rejected the credentials (HTTP {exc.status_code}): check LLM_API_KEY. "
                f"{_message(exc)}") from exc
        except openai.APIStatusError as exc:
            msg = _message(exc)
            if exc.status_code in (400, 404, 422) and _UNSUPPORTED.search(msg):
                raise ToolCallingUnsupported(f"{model()} does not support tool calling on {provider()}: {msg}") from exc
            raise LLMError(f"{provider()} returned HTTP {exc.status_code}: {msg}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"{provider()} is unreachable: {_message(exc)}") from exc
        if getattr(resp, "choices", None):
            return resp
        err = getattr(resp, "error", None)  # some gateways answer HTTP 200 with an error body
        detail = scrub(json.dumps(err, default=str) if err else "empty response")
        code = err.get("code") if isinstance(err, dict) else getattr(err, "code", None)
        if str(code) == "429":
            if attempt < attempts - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise RateLimited(f"rate limited by {provider()} after {attempts} attempts: {detail}")
        raise LLMError(f"{provider()} returned no completion: {detail}")
    raise LLMError("unreachable")  # pragma: no cover


def _schema(params_model) -> dict | None:
    schema = params_model.model_json_schema()
    props = {
        name: {k: v for k, v in spec.items() if k != "title"}  # providers differ on JSON Schema extras: keep it minimal
        for name, spec in schema.get("properties", {}).items()
    }
    if not props:
        return None
    return {"type": "object", "properties": props, "required": schema.get("required", [])}


def _tool_defs(tools: dict) -> list[dict]:
    defs = []
    for name, (params_model, _fn, doc) in tools.items():
        fn = {"name": name, "description": doc}
        schema = _schema(params_model)
        if schema:  # a tool without parameters simply omits the schema
            fn["parameters"] = schema
        defs.append({"type": "function", "function": fn})
    return defs


def choose_tool(question: str, tools: dict) -> tuple[str, dict]:
    """Ask the model to pick exactly one tool. Returns (tool name, raw params); the caller validates them in code."""
    resp = _complete(
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}],
        tools=_tool_defs(tools),
        tool_choice="auto",
        max_tokens=1500,
    )
    choice = resp.choices[0]
    msg = choice.message
    calls = list(getattr(msg, "tool_calls", None) or [])
    if not calls:
        if choice.finish_reason == "content_filter" or getattr(msg, "refusal", None):
            raise LLMError("the LLM declined to route this question.")
        said = (getattr(msg, "content", None) or "")[:200]
        raise LLMError(scrub(f"the LLM did not choose a tool (finish_reason={choice.finish_reason}; said: {said!r})."))
    if len(calls) > 1:
        raise LLMError(f"the LLM returned {len(calls)} tool calls; exactly one is required.")
    fn = calls[0].function
    try:
        params = json.loads(fn.arguments or "{}")
    except (TypeError, ValueError) as exc:
        raise LLMError(
            scrub(f"the LLM's arguments for {fn.name} are not valid JSON: {str(fn.arguments)[:120]!r}")) from exc
    if not isinstance(params, dict):
        raise LLMError(f"the LLM's arguments for {fn.name} must be a JSON object, got {type(params).__name__}.")
    return fn.name, params


def summarize_notice(markdown: str) -> str:
    """2-3 sentence plain summary of a staged notice. Rejected if it mentions any KEP/SIG/handle not in the notice."""
    resp = _complete(
        messages=[
            {"role": "system", "content": "Summarize the impact notice in at most 3 sentences for the affected SIG "
                                          "leads. Use only facts stated in the notice. Do not add KEPs, SIGs or "
                                          "people that are not in it."},
            {"role": "user", "content": markdown},
        ],
        max_tokens=800,
    )
    text = (resp.choices[0].message.content or "").strip()
    for token in set(re.findall(r"KEP-\d+|sig-[a-z-]+|@[a-z0-9-]+", text, re.IGNORECASE)):
        if token.lower() not in markdown.lower():
            raise LLMError(f"LLM summary mentioned {token!r}, which is not in the staged notice; discarded.")
    return scrub(text)
