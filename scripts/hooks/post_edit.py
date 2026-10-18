"""Claude Code PostToolUse hook: after a .py edit, run ruff on that file and the fast test suite.

Exit code 2 feeds failures back to the assistant. Non-Python edits are ignored.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py") or not PY.exists():
        return 0
    problems = []
    lint = subprocess.run([str(PY), "-m", "ruff", "check", path], cwd=ROOT, capture_output=True, text=True)
    if lint.returncode:
        problems.append("ruff:\n" + lint.stdout[-2000:])
    tests = subprocess.run(
        [str(PY), "-m", "pytest", "-m", "not integration", "-x", "-q", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if tests.returncode not in (0, 5):  # 5 = no tests collected
        problems.append("pytest (fast):\n" + tests.stdout[-2500:])
    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
