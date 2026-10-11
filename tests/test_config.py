"""Config tests: risky_actions must parse from raw .env/env values.

Regression: pydantic-settings treats list-typed fields as complex and tries
json.loads on the raw value, so an empty RISKY_ACTIONS= crashed at startup.
The NoDecode annotation routes the raw string through _parse_csv instead.
"""

from __future__ import annotations

from robotctl.config import Settings


def test_risky_actions_unset_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("RISKY_ACTIONS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.risky_actions == []


def test_risky_actions_empty_value_parses_to_empty_list(monkeypatch):
    monkeypatch.setenv("RISKY_ACTIONS", "")
    settings = Settings(_env_file=None)
    assert settings.risky_actions == []


def test_risky_actions_csv_value_parses(monkeypatch):
    monkeypatch.setenv("RISKY_ACTIONS", "move, wait")
    settings = Settings(_env_file=None)
    assert settings.risky_actions == ["move", "wait"]


def test_risky_actions_single_value_parses(monkeypatch):
    monkeypatch.setenv("RISKY_ACTIONS", "move")
    settings = Settings(_env_file=None)
    assert settings.risky_actions == ["move"]


def test_risky_actions_list_passthrough(monkeypatch):
    monkeypatch.delenv("RISKY_ACTIONS", raising=False)
    settings = Settings(_env_file=None, risky_actions=["move"])
    assert settings.risky_actions == ["move"]
