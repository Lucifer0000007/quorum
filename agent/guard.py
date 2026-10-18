"""Static guard for agent read queries: no writes, allow-listed procedures only, LIMIT <= 100, timeout <= 3 s.

This runs on every template at import time and again before every execution. FalkorDB's GRAPH.RO_QUERY
also rejects writes server-side, so a write would have to get past two independent layers.
"""

from __future__ import annotations

import re

MAX_LIMIT = 100
MAX_TIMEOUT_MS = 3000
ALLOWED_PROCEDURES = {"algo.pagerank"}

_WRITE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b", re.IGNORECASE)
_CALL = re.compile(r"\bCALL\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_FINAL_LIMIT = re.compile(r"\bLIMIT\s+(\d+|\$([A-Za-z_]\w*))\s*;?\s*$", re.IGNORECASE)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


class GuardError(ValueError):
    """A query was refused before reaching the database."""


def _strip(cypher: str) -> str:
    return _STRINGS.sub("''", _COMMENTS.sub(" ", cypher)).strip()


def check_query(cypher: str, params: dict | None = None, *, require_params: bool = True) -> None:
    """Raise GuardError unless the query is a bounded read. `require_params` checks a `$limit` value too."""
    code = _strip(cypher)
    m = _WRITE.search(code)
    if m:
        raise GuardError(f"write keyword '{m.group(1).upper()}' is not allowed in agent reads")
    for proc in _CALL.findall(code):
        if proc.lower() not in ALLOWED_PROCEDURES:
            raise GuardError(f"procedure '{proc}' is not allow-listed")
    m = _FINAL_LIMIT.search(code)
    if not m:
        raise GuardError("query must end with LIMIT <= 100")
    if m.group(2):
        if require_params:
            value = (params or {}).get(m.group(2))
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIMIT:
                raise GuardError(f"${m.group(2)} must be an int in 1..{MAX_LIMIT}, got {value!r}")
    elif not 1 <= int(m.group(1)) <= MAX_LIMIT:
        raise GuardError(f"LIMIT {m.group(1)} exceeds {MAX_LIMIT}")


def check_timeout(timeout_ms: int) -> None:
    if not isinstance(timeout_ms, int) or not 0 < timeout_ms <= MAX_TIMEOUT_MS:
        raise GuardError(f"timeout must be 1..{MAX_TIMEOUT_MS} ms, got {timeout_ms!r}")
