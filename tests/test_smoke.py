from ingest import config


def test_graph_names_are_distinct():
    assert config.BRAIN_GRAPH == "company_brain"
    assert config.EPISODIC_GRAPH == "agent_episodic_memory"
