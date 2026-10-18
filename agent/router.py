"""Question -> (tool, typed params) -> result. Two routers: deterministic keywords, or an LLM that only picks a tool.

Either way, parameters are validated by pydantic here before anything touches the graph, and the response
always says which router was used. The LLM never sees or writes Cypher and never computes a verdict.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent import queries as q
from agent.bailiff import preflight

KEP_ID = r"^(?:\d{1,5}|[a-z0-9-]+/[a-z0-9.-]+)$"


class Params(BaseModel):
    """Typed tool parameters. Unknown fields are rejected, so the model can't smuggle anything extra in."""

    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


class KepParams(Params):
    kep_id: str = Field(description="KEP number, e.g. '1287' (no 'KEP-' prefix needed)")

    @field_validator("kep_id")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip()
        v = v[4:] if v.upper().startswith("KEP-") else v
        v = v.lstrip("#").strip().lower()
        if not re.match(KEP_ID, v):
            raise ValueError(f"not a KEP id: {v!r}")
        return v


class SigParams(Params):
    sig: str = Field(description="SIG name, e.g. 'sig-node'")

    @field_validator("sig")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip().lower()
        v = v if v.startswith(("sig-", "wg-", "committee-")) else f"sig-{v}"
        if not re.match(r"^[a-z0-9-]{3,60}$", v):
            raise ValueError(f"not a SIG name: {v!r}")
        return v


TOOLS = {
    "kep_detail": (KepParams, lambda p: q.kep_detail(p.kep_id), "Metadata, owning SIG, status, people of a KEP."),
    "blast_radius": (KepParams, lambda p: q.blast_radius(p.kep_id), "KEPs within 3 REPLACES/SEE_ALSO hops."),
    "is_still_valid": (KepParams, lambda p: q.is_still_valid(p.kep_id),
                       "Is a KEP still valid: status, successors, stale dependencies."),
    "who_knows_kep": (KepParams, lambda p: q.who_knows(kep_id=p.kep_id), "Experts around a KEP."),
    "who_knows_sig": (SigParams, lambda p: q.who_knows(sig=p.sig), "Experts in a SIG."),
    "why": (KepParams, lambda p: q.why(p.kep_id), "Provenance chain of a KEP decision (sources, approvers, issue)."),
    "referrers": (KepParams, lambda p: q.referrers(p.kep_id), "KEPs that list this KEP in see-also."),
    "sig_overview": (SigParams, lambda p: q.sig_overview(p.sig), "How many KEPs a SIG owns."),
    "preflight": (KepParams, lambda p: preflight(p.kep_id), "Simulate superseding a KEP: deterministic verdict."),
    "central_keps": (NoParams, lambda p: q.central_keps(10),
                     "Most central KEPs by PageRank over see-also links (FalkorDB algorithm)."),
}


class RoutingError(ValueError):
    pass


def dispatch(tool: str, params: dict) -> dict:
    if tool not in TOOLS:
        raise RoutingError(f"unknown tool {tool!r}; available: {sorted(TOOLS)}")
    model, fn, _ = TOOLS[tool]
    try:
        typed = model(**params)
    except ValidationError as exc:
        raise RoutingError(f"invalid parameters for {tool}: {exc.errors()[0]['msg']}") from exc
    out = fn(typed)
    out["tool"] = tool
    out["tool_params"] = typed.model_dump()
    out["summary"] = summarize(tool, out)
    return out


_KEP = re.compile(r"\bKEP[- #]?(\d{1,5})\b", re.IGNORECASE)
_NUM = re.compile(r"\b(\d{2,5})\b")
_SIG = re.compile(r"\b((?:sig|wg|committee)-[a-z0-9-]+)\b", re.IGNORECASE)


def keyword_route(question: str) -> tuple[str, dict]:
    """Deterministic routing by keywords. Raises RoutingError when the question can't be mapped."""
    text = question.lower()
    m = _KEP.search(question) or _NUM.search(question)
    kep = m.group(1) if m else None
    s = _SIG.search(question)
    sig = s.group(1).lower().rstrip("?.,") if s else None

    def need_kep(tool):
        if not kep:
            raise RoutingError(f"'{tool}' needs a KEP number (e.g. KEP-1287) in the question")
        return tool, {"kep_id": kep}

    if re.search(r"pre-?flight|simulat|what if we supersede|supersede kep|supersed(e|ing) (it|this|kep)", text):
        return need_kep("preflight")
    if re.search(r"who (knows|should review|can review)|expert|reviewers? for|ask about", text):
        if kep:
            return "who_knows_kep", {"kep_id": kep}
        if sig:
            return "who_knows_sig", {"sig": sig}
        raise RoutingError("who_knows needs a KEP number or a SIG name (e.g. sig-node)")
    central = r"central|important|influential|\bhubs?\b|pagerank|most (referenced|linked|connected)"
    if not kep and re.search(central, text):
        return "central_keps", {}
    if re.search(r"see[- ]also|list kep|refer|cite", text):
        return need_kep("referrers")
    if re.search(r"successor|superseded|still valid|is .* valid|replaced by|obsolete|stale", text):
        return need_kep("is_still_valid")
    if re.search(r"blast|impact|affect|downstream|depend", text):
        return need_kep("blast_radius")
    if re.search(r"\bwhy\b|provenance|source|who approved|origin", text):
        return need_kep("why")
    if sig and re.search(r"how many|count|number of|own", text) and not kep:
        return "sig_overview", {"sig": sig}
    if kep:
        return "kep_detail", {"kep_id": kep}
    if sig:
        return "sig_overview", {"sig": sig}
    raise RoutingError("Could not route the question. Mention a KEP number (KEP-1287) or a SIG (sig-node).")


def summarize(tool: str, out: dict) -> str:
    """One-line deterministic answer built from the result fields (no LLM)."""
    r = out.get("result")
    if tool == "kep_detail":
        k = r["kep"]
        return (f"KEP-{k['id']} \"{k['title']}\" is {k['status']}, owned by {', '.join(r['owning_sigs']) or 'no SIG'}"
                f"; {len(r['people']['APPROVED'])} approver(s), {len(r['neighbors'])} direct KEP link(s).")
    if tool == "blast_radius":
        if r["no_path_found"]:
            return r["message"]
        active = [a for a in r["reachable"] if a["active"]]
        sigs = sorted({a["owning_sig"] for a in active if a["owning_sig"]})
        return (f"{r['reachable_count']} KEPs within 3 hops of KEP-{r['kep_id']}; {len(active)} active, "
                f"owned by {len(sigs)} SIG(s): {', '.join(sigs)}.")
    if tool == "is_still_valid":
        succ = ", ".join("KEP-" + s["id"] for s in r["successors"]) or "none"
        return (f"KEP-{r['kep_id']} is {r['status']} → {r['validity']}. Successors: {succ}. "
                f"Stale dependencies: {len(r['stale_ancestors'])}.")
    if tool in ("who_knows_kep", "who_knows_sig"):
        if r["no_result"]:
            return f"No people found for {r['scope']}."
        top = ", ".join(f"@{p['handle']} ({p['score']:g})" for p in r["people"][:5])
        return f"Top people for {r['scope']}: {top}."
    if tool == "why":
        return " → ".join(f"{c['step']}: {c['value']}" for c in r["chain"][:5])
    if tool == "referrers":
        return r["message"] or f"{r['count']} KEP(s) list KEP-{r['kep_id']} in see-also: " + ", ".join(
            "KEP-" + x["id"] for x in r["referrers"])
    if tool == "sig_overview":
        return f"{r['sig']} owns {r['owned']} KEPs ({int(r['active'])} active)."
    if tool == "preflight":
        return f"Verdict for superseding KEP-{out['kep']['id']}: {out['verdict']}."
    if tool == "central_keps":
        top = ", ".join(f"KEP-{k['id']} ({k['owning_sig']})" for k in r[:5])
        return f"Most central KEPs by PageRank over see-also links: {top}."
    return ""
