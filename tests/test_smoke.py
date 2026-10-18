import os
import subprocess
import sys
from pathlib import Path

from ingest import config
from tests.conftest import TEST_EPISODIC_GRAPH

ROOT = Path(__file__).resolve().parent.parent


def test_default_graph_names_are_the_documented_ones():
    # A clean environment (no QUORUM_* overrides, no .env values for them) must yield the names in the README.
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUORUM_")}
    code = "from ingest import config; print(config.BRAIN_GRAPH, config.EPISODIC_GRAPH)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["company_brain", "agent_episodic_memory"]


def test_graph_names_are_distinct():
    assert config.BRAIN_GRAPH != config.EPISODIC_GRAPH


def test_suite_never_points_at_the_demo_episodic_graph():
    # conftest.py redirects the whole session; if this fails, tests could write to the real demo memory.
    assert config.EPISODIC_GRAPH == TEST_EPISODIC_GRAPH != "agent_episodic_memory"
