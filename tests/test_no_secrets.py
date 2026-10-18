"""Guards against committing credentials: .env is ignored, and no tracked file or commit holds a key-like string."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "OpenRouter key": r"sk-or-v1-[0-9a-fA-F]{16,}",
    "Anthropic key": r"sk-ant-[A-Za-z0-9_-]{16,}",
    "OpenAI-style key": r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}",
    "Google API key": r"AIza[0-9A-Za-z_-]{30,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "assigned LLM key": r"(?im)^\s*LLM_API_KEY\s*=\s*(?!<)[^\s#]{12,}",  # placeholders like <your key> are fine
}

pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists() or shutil.which("git") is None,
                                reason="needs a git checkout")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")


def hits(text: str) -> list[str]:
    return [name for name, pattern in PATTERNS.items() if re.search(pattern, text)]


def test_dotenv_is_ignored_and_not_tracked():
    assert git("check-ignore", "-q", ".env").returncode == 0, ".env must be listed in .gitignore"
    assert ".env" not in git("ls-files").stdout.split()


def test_no_tracked_file_contains_a_credential():
    offenders = []
    for rel in git("ls-files").stdout.splitlines():
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [f"{rel}: {name}" for name in hits(text)]
    assert not offenders, offenders


def test_no_commit_in_history_ever_contained_a_credential():
    history = git("log", "--all", "-p", "--no-color").stdout
    assert history, "git log returned nothing"
    assert not hits(history), hits(history)


def test_the_patterns_do_catch_what_they_are_meant_to():
    fake_key = "sk-or-v1-" + "0123456789abcdef" * 3
    assert "OpenRouter key" in hits(fake_key)
    assert hits("LLM_API_KEY=abcdefghijklmnop") == ["assigned LLM key"]
    assert hits("LLM_API_KEY=<your key>") == [] and hits("LLM_API_KEY=") == []
