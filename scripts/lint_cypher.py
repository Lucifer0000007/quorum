"""Static lint for the agent read path.

1. Every registered template passes agent.guard (no writes, LIMIT <= 100, allow-listed CALLs only).
2. No module under agent/ calls a write-capable `.query(` except agent/episodic.py (the approval gate),
   and agent/db.py only uses `ro_query`.
3. agent/episodic.py only writes to the agent_episodic_memory graph.
"""

import re
import sys
from pathlib import Path

from agent.guard import GuardError, check_query
from agent.templates import TEMPLATES

AGENT = Path(__file__).resolve().parent.parent / "agent"
WRITE_ALLOWED = {"episodic.py"}


def main() -> int:
    problems = []
    for name, t in TEMPLATES.items():
        try:
            check_query(t["cypher"], require_params=False)
        except GuardError as exc:
            problems.append(f"template {name}: {exc}")
    for py in sorted(AGENT.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?<!ro_)\.query\(", line) and py.name not in WRITE_ALLOWED:
                problems.append(f"{py.name}:{lineno}: write-capable .query( outside the approval gate")
        if py.name == "episodic.py" and "BRAIN_GRAPH" in text:
            problems.append("episodic.py references BRAIN_GRAPH: the gate may only write agent_episodic_memory")
    for p in problems:
        print("FAIL:", p)
    if not problems:
        print(f"OK: {len(TEMPLATES)} templates are read-only and LIMIT-capped; no write paths outside episodic.py")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
