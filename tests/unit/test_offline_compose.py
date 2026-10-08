"""Offline startup must never resolve build contexts or registry images."""

import yaml

from agent.config import ROOT


def test_offline_compose_has_no_build_or_pull() -> None:
    config = yaml.safe_load((ROOT / "docker" / "docker-compose.offline.yml").read_text(encoding="utf-8"))
    assert set(config["services"]) == {"api", "mock", "qdrant"}
    for service in config["services"].values():
        assert "build" not in service
        assert service["pull_policy"] == "never"
        assert service["platform"] == "linux/amd64"
    assert config["services"]["api"]["environment"]["LLM_PROVIDER"] == "rules"
