"""
Paths Test
Checks what an older install kept in the repository folder is moved to where it now
belongs, once, without ever overwriting anything.
"""

import os

import pytest

from voice_assistant import paths


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A repository folder as an older install left it, and empty places for its things."""
    repository = tmp_path / "repo"
    (repository / "data").mkdir(parents=True)
    (repository / "logs").mkdir()
    (repository / "data" / "timers.json").write_text('{"timers": [1]}')
    (repository / "data" / "alarms.json").write_text('{"alarms": []}')
    (repository / ".spotify_cache").write_text("token")
    (repository / ".env").write_text("ANTHROPIC_API_KEY=key")
    (repository / "config.local.yaml").write_text("weather:\n  location: Leeds\n")
    (repository / "logs" / "smart_assistant.log").write_text("old log")

    config, data = tmp_path / "config", tmp_path / "data"
    monkeypatch.setattr(paths, "CONFIG_DIR", str(config))
    monkeypatch.setattr(paths, "DATA_DIR", str(data))
    monkeypatch.setattr(paths, "LOGS_DIR", str(data / "logs"))
    monkeypatch.setattr(paths, "OLD_PLACES", [
        ("config.local.yaml", str(config / "config.yaml")),
        (".env", str(config / ".env")),
        ("data/timers.json", str(data / "timers.json")),
        ("data/alarms.json", str(data / "alarms.json")),
        ("data/volumes.json", str(data / "volumes.json")),
        (".spotify_cache", str(data / "spotify_token.json")),
        ("logs", str(data / "logs")),
    ])
    return repository, config, data


def test_everything_is_moved_out_of_the_repository(install):
    repository, config, data = install
    moved = paths.migrate(str(repository))

    assert set(moved) == {"config.local.yaml", ".env", "data/timers.json", "data/alarms.json", ".spotify_cache", "logs"}
    assert (config / "config.yaml").read_text() == "weather:\n  location: Leeds\n"
    assert (data / "timers.json").read_text() == '{"timers": [1]}'
    assert (data / "spotify_token.json").read_text() == "token"
    assert (data / "logs" / "smart_assistant.log").read_text() == "old log"
    assert sorted(os.listdir(repository)) == []


def test_its_kept_private(install):
    repository, config, data = install
    paths.migrate(str(repository))
    assert (config / ".env").stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o700 and data.stat().st_mode & 0o777 == 0o700


def test_nothing_already_moved_is_overwritten(install):
    repository, config, data = install
    paths.migrate(str(repository))
    (repository / "data").mkdir()
    (repository / "data" / "timers.json").write_text("stale")

    assert paths.migrate(str(repository)) == []
    assert (data / "timers.json").read_text() == '{"timers": [1]}'
    assert (repository / "data" / "timers.json").exists()
