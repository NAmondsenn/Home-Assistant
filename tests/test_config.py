"""
Config Test
Checks settings are read from config.yaml, with the install's own on top, and that a
mistake in the install's file is caught rather than ignored.
"""

import pytest

from voice_assistant.config import Config, ConfigError

DEFAULTS = """
weather:
  location: ""
  units: metric
audio:
  sample_rate: 48000
thresholds:
  silence_duration: 0.6
  vad_threshold: 0.5
music:
  player: "spotify"
home_assistant:
  calendars: []
"""


@pytest.fixture
def config_with(tmp_path):
    """Reads DEFAULTS with the given text as the install's own settings."""
    (tmp_path / "defaults.yaml").write_text(DEFAULTS)

    def read(own=None):
        if own is not None:
            (tmp_path / "config.yaml").write_text(own)
        return Config(path=str(tmp_path / "config.yaml"), defaults=str(tmp_path / "defaults.yaml"))

    return read


def test_the_installs_settings_override_only_what_they_name(config_with):
    config = config_with("weather:\n  location: Newcastle upon Tyne\n")
    assert config.section("weather") == {"location": "Newcastle upon Tyne", "units": "metric"}
    assert config.section("audio") == {"sample_rate": 48000}


def test_without_the_installs_own_the_defaults_are_used_as_they_are(config_with):
    assert config_with().section("weather") == {"location": "", "units": "metric"}


def test_an_empty_file_of_its_own_is_fine(config_with):
    assert config_with("# nothing yet\n").section("audio") == {"sample_rate": 48000}


@pytest.mark.parametrize("own, said", [
    # Misspellings used to be ignored, leaving the default quietly in force.
    ("thresholds:\n  silence_duraton: 1.0\n", "'thresholds.silence_duraton' isn't a setting. Did you mean 'silence_duration'?"),
    ("wether:\n  location: Leeds\n", "there's no 'wether' section. Did you mean 'weather'?"),
    ("thresholds:\n  silence_duration: quick\n", "'thresholds.silence_duration' should be a number, not 'quick'"),
    ("thresholds:\n  vad_threshold: 5\n", "'thresholds.vad_threshold' should be between 0 and 1, not 5"),
    ("music:\n  player: winamp\n", "'music.player' should be one of navidrome, spotify, not 'winamp'"),
    ("home_assistant:\n  calendars: calendar.personal\n", "'home_assistant.calendars' should be a list"),
    ("weather: Leeds\n", "'weather' should be settings under it"),
    ("weather:\n  location: [Leeds\n", "isn't valid YAML"),
])
def test_a_mistake_stops_it_starting_and_says_where(config_with, own, said):
    with pytest.raises(ConfigError) as caught:
        config_with(own)
    assert said in str(caught.value) and "config.yaml" in str(caught.value)


def test_a_whole_number_is_fine_for_a_setting_with_a_decimal_default(config_with):
    assert config_with("thresholds:\n  silence_duration: 1\n").section("thresholds")["silence_duration"] == 1


def test_the_real_config_yaml_reads(tmp_path):
    # Every setting the repository ships has to be readable as the defaults.
    config = Config(path=str(tmp_path / "none.yaml"))
    assert config.section("music")["player"] in ("navidrome", "spotify")
