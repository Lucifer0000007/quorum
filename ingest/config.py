"""Connection settings and graph names shared by ingest, agent and scripts."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ENHANCEMENTS_DIR = DATA_DIR / "enhancements"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

# Names are overridable so tests and smoke runs can use throwaway graphs instead of the demo's real ones.
BRAIN_GRAPH = os.environ.get("QUORUM_BRAIN_GRAPH", "company_brain")
EPISODIC_GRAPH = os.environ.get("QUORUM_EPISODIC_GRAPH", "agent_episodic_memory")
if BRAIN_GRAPH == EPISODIC_GRAPH:
    raise RuntimeError("QUORUM_BRAIN_GRAPH and QUORUM_EPISODIC_GRAPH must name different graphs.")

REPO_TREE_URL = "https://github.com/kubernetes/enhancements/tree/master/"
ISSUE_URL = "https://github.com/kubernetes/enhancements/issues/"


def connect():
    """Return a FalkorDB client or raise a clear error. Never falls back to anything else."""
    from falkordb import FalkorDB

    db = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT, socket_timeout=10)
    try:
        db.connection.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            f"FalkorDB is not reachable at {FALKORDB_HOST}:{FALKORDB_PORT} ({exc}). Run `make up`."
        ) from exc
    return db
