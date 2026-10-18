from ingest import config
from tests.conftest import TEST_EPISODIC_GRAPH


def test_graph_names_are_distinct():
    assert config.BRAIN_GRAPH == "company_brain"
    assert config.EPISODIC_GRAPH != config.BRAIN_GRAPH


def test_suite_never_points_at_the_demo_episodic_graph():
    # conftest.py redirects the whole session; if this fails, tests could write to the real demo memory.
    assert config.EPISODIC_GRAPH == TEST_EPISODIC_GRAPH != "agent_episodic_memory"
