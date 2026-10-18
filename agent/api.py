"""Quorum HTTP API + static UI. Run: uvicorn agent.api:app --port 8000"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from agent import llm, queries
from agent.bailiff import preflight
from agent.db import GraphNotReady, meta
from agent.episodic import record_precedent
from agent.guard import GuardError
from agent.router import TOOLS, RoutingError, dispatch, keyword_route
from ingest.config import ROOT
from ingest.load import graph_counts

UI = ROOT / "ui"
RESULTS = ROOT / "evals" / "results.json"
app = FastAPI(title="Quorum", description="Pre-flight checks for engineering decisions on a FalkorDB company brain")
STAGED: dict[str, dict] = {}  # notice id -> staged notice (in-memory; nothing is ever sent anywhere)


def _err(status: int, kind: str, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": kind, "detail": str(exc)})


@app.exception_handler(GraphNotReady)
async def _not_ready(_: Request, exc: GraphNotReady):
    return _err(503, "graph_not_ready", exc)


@app.exception_handler(RedisConnectionError)
async def _down(_: Request, exc: RedisConnectionError):
    return _err(503, "falkordb_unreachable", RuntimeError(f"FalkorDB unreachable: {exc}. Run `make up`."))


@app.exception_handler(RuntimeError)
async def _runtime(_: Request, exc: RuntimeError):
    return _err(503 if "FalkorDB" in str(exc) else 500, "runtime_error", exc)


@app.exception_handler(queries.NotFound)
async def _nf(_: Request, exc: Exception):
    return _err(404, "not_found", exc)


@app.exception_handler(queries.Ambiguous)
async def _amb(_: Request, exc: Exception):
    return _err(409, "ambiguous", exc)


@app.exception_handler(RoutingError)
async def _route(_: Request, exc: Exception):
    return _err(422, "routing_error", exc)


@app.exception_handler(GuardError)
async def _guard(_: Request, exc: Exception):
    return _err(400, "guard_rejected", exc)


@app.exception_handler(llm.LLMError)
async def _llm(_: Request, exc: Exception):
    return _err(502, "llm_error", exc)


@app.exception_handler(ResponseError)
async def _db_err(_: Request, exc: Exception):
    return _err(500, "falkordb_error", exc)


def eval_summary(data_hash: str | None) -> dict | None:
    if not RESULTS.exists():
        return None
    r = json.loads(RESULTS.read_text(encoding="utf-8"))
    return {"passed": r["passed"], "total": r["total"], "rate": r["rate"], "ran_at": r["ran_at"],
            "stale": data_hash is not None and r.get("data_hash") != data_hash}


@app.get("/health")
def health():
    out = {"llm": {"configured": llm.configured(), "model": llm.model(),
                   "router": "llm available" if llm.configured() else "keyword router only (no ANTHROPIC_API_KEY)"},
           "posting": "disabled: Quorum never posts to GitHub or any external service"}
    try:
        m = meta(force=True)
        from agent.db import db
        from ingest.config import BRAIN_GRAPH

        out |= {"status": "ok", "graph_ready": True, "meta": m,
                "counts": graph_counts(db().select_graph(BRAIN_GRAPH))}
        out["eval"] = eval_summary(m["data_hash"])
    except GraphNotReady as exc:
        out |= {"status": "not_ready", "graph_ready": False, "detail": str(exc), "eval": eval_summary(None)}
    except (RedisConnectionError, RuntimeError) as exc:
        out |= {"status": "falkordb_down", "graph_ready": False, "detail": str(exc), "eval": eval_summary(None)}
    return JSONResponse(out, status_code=200 if out.get("graph_ready") else 503)


@app.get("/keps")
def search(q: str, limit: int = 20):
    return queries.search_keps(q, limit)


@app.get("/kep/{kep_id:path}")
def kep(kep_id: str):
    return queries.kep_detail(kep_id)


@app.get("/sigs")
def sigs():
    return queries.list_sigs()


@app.get("/central")
def central(limit: int = 10):
    return queries.central_keps(limit)


class AskBody(BaseModel):
    question: str
    router: str = "auto"  # auto | keyword | llm


@app.post("/ask")
def ask(body: AskBody):
    use_llm = body.router == "llm" or (body.router == "auto" and llm.configured())
    if use_llm:
        tool, params = llm.choose_tool(body.question, TOOLS)  # LLMError -> 502, shown in the UI (no silent fallback)
        router = f"llm ({llm.model()})"
    else:
        tool, params = keyword_route(body.question)
        router = "keyword" + ("" if llm.configured() else " (LLM not configured)")
    out = dispatch(tool, params)
    if tool == "preflight":
        STAGED[out["staged_notice"]["id"]] = out["staged_notice"]
    return out | {"router": router, "question": body.question}


class ToolBody(BaseModel):
    tool: str
    params: dict


@app.post("/tool")
def tool(body: ToolBody):
    out = dispatch(body.tool, body.params)
    if body.tool == "preflight":
        STAGED[out["staged_notice"]["id"]] = out["staged_notice"]
    return out | {"router": "direct"}


class PreflightBody(BaseModel):
    kep_id: str
    action: str = "supersede"


@app.post("/preflight")
def run_preflight(body: PreflightBody):
    out = preflight(body.kep_id, body.action)
    STAGED[out["staged_notice"]["id"]] = out["staged_notice"]
    return out


class ApproveBody(BaseModel):
    notice_id: str
    session_id: str


@app.post("/approve")
def approve(body: ApproveBody):
    notice = STAGED.get(body.notice_id)
    if notice is None:
        return _err(404, "unknown_notice", LookupError(
            f"No staged notice {body.notice_id!r} on this server. Run the pre-flight check first."))
    if notice["data_hash"] != meta(force=True)["data_hash"]:
        return _err(409, "stale_notice", LookupError("Graph data changed since this notice was staged; re-run it."))
    out = record_precedent(notice, body.session_id)
    out["prior_precedents"] = queries.prior_precedents(notice["kep_id"])["result"]
    return out


class NoticeBody(BaseModel):
    notice_id: str


@app.post("/notice/summarize")
def summarize_notice(body: NoticeBody):
    notice = STAGED.get(body.notice_id)
    if notice is None:
        return _err(404, "unknown_notice", LookupError("Unknown notice id; run the pre-flight check first."))
    return {"summary": llm.summarize_notice(notice["markdown"]), "model": llm.model(),
            "note": "LLM-phrased summary; checked to mention only KEPs, SIGs and people present in the notice."}


@app.get("/eval")
def eval_results():
    if not RESULTS.exists():
        return _err(404, "no_eval", FileNotFoundError("evals/results.json not found. Run `make eval`."))
    return json.loads(RESULTS.read_text(encoding="utf-8"))


@app.get("/")
def index():
    return FileResponse(UI / "index.html")


app.mount("/ui", StaticFiles(directory=Path(UI)), name="ui")
