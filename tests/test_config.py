"""
Config Test
Checks settings are read from config.yaml, with the install's own on top, and that a
mistake in the install's file is caught rather than ignored.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from voice_assistant.config import LIMITS, Config, ConfigError, check, clashes, limit_problem, removed

DEFAULTS = """
navidrome:
  url: ""
  username: nova
audio:
  sample_rate: 48000
thresholds:
  silence_duration: 0.6
  vad_threshold: 0.5
music:
  player: "spotify"
home_assistant:
  url: ""
batteries:
  other_marks: [15, 5]
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
    config = config_with("navidrome:\n  url: http://music.local:4533\n")
    assert config.section("navidrome") == {"url": "http://music.local:4533", "username": "nova"}
    assert config.section("audio") == {"sample_rate": 48000}


def test_without_the_installs_own_the_defaults_are_used_as_they_are(config_with):
    assert config_with().section("navidrome") == {"url": "", "username": "nova"}


def test_an_empty_file_of_its_own_is_fine(config_with):
    assert config_with("# nothing yet\n").section("audio") == {"sample_rate": 48000}


@pytest.mark.parametrize("own, said", [
    # Misspellings used to be ignored, leaving the default quietly in force.
    ("thresholds:\n  silence_duraton: 1.0\n", "'thresholds.silence_duraton' isn't a setting. Did you mean 'silence_duration'?"),
    ("navidrom:\n  url: http://music.local:4533\n", "there's no 'navidrom' section. Did you mean 'navidrome'?"),
    ("thresholds:\n  silence_duration: quick\n", "'thresholds.silence_duration' should be a number, not 'quick'"),
    ("thresholds:\n  vad_threshold: 5\n", "'thresholds.vad_threshold' should be between 0 and 1, not 5"),
    ("music:\n  player: winamp\n", "'music.player' should be one of navidrome, spotify, not 'winamp'"),
    ("batteries:\n  other_marks: 15\n", "'batteries.other_marks' should be a list"),
    ("music: spotify\n", "'music' should be settings under it"),
    ("music:\n  player: [spotify\n", "isn't valid YAML"),
])
def test_a_mistake_stops_it_starting_and_says_where(config_with, own, said):
    with pytest.raises(ConfigError) as caught:
        config_with(own)
    assert said in str(caught.value) and "config.yaml" in str(caught.value)


RETIRED = """speech_to_text:
  engine: moonshine
moonshine:
  keyterms: [Nova]
whisper:
  model: base.en
music:
  player: navidrome
"""


def test_the_sections_nova_no_longer_uses_are_let_through_with_a_warning(config_with, caplog):
    # A Pi's own settings from before the brain heard for it, which stopped both the
    # satellite and the brain starting.
    with caplog.at_level(logging.WARNING):
        config = config_with(RETIRED)
    assert config.section("music")["player"] == "navidrome"
    assert config.section("moonshine") == {}
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert all(name in warning for name in ("speech_to_text", "moonshine", "whisper")) and "deleted" in warning


def test_the_settings_page_lets_them_through_too_and_says_nothing(config_with, tmp_path, caplog):
    # It checks the file before saving to it, and reads it every time it looks.
    (tmp_path / "config.yaml").write_text(RETIRED)
    with caplog.at_level(logging.WARNING):
        Config(path=str(tmp_path / "config.yaml"), defaults=str(tmp_path / "defaults.yaml"), quiet=True)
    assert caplog.records == []
    assert check({"moonshine": {"keyterms": []}}, {"music": {}}, "config.yaml") == ["moonshine"]


def test_any_other_section_it_doesnt_know_still_stops_it(config_with):
    with pytest.raises(ConfigError) as caught:
        config_with("moonshine:\n  keyterms: [Nova]\nnavidrom:\n  url: http://music.local:4533\n")
    assert "there's no 'navidrom' section" in str(caught.value)


def test_a_setting_nova_no_longer_has_is_let_through_with_where_its_set_now(config_with, caplog):
    # The brain's own file still had weather.location once the weather was Home Assistant's,
    # and refusing it would have stopped the brain starting.
    with caplog.at_level(logging.WARNING):
        config = config_with("weather:\n  location: Leeds\n")
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert "'weather.location'" in warning and "Home Assistant" in warning and "deleted" in warning
    assert config.section("weather") == {}
    assert removed({"weather": {"location": "Leeds"}, "music": {"player": "spotify"}}) == [("weather", "location")]

    # Beside a setting which is still one, it's left out alone - and a mistake beside it still stops it.
    assert config_with("music:\n  player: navidrome\nweather:\n  location: Leeds\n").section("music") == {
        "player": "navidrome"}
    with pytest.raises(ConfigError) as caught:
        config_with("weather:\n  location: Leeds\n  units: metric\n")
    assert "there's no 'weather' section" in str(caught.value)


def test_home_assistant_calendars_is_let_through_with_where_its_set_now(config_with, caplog):
    # The brain's own file may still name the calendars Home Assistant read, from before
    # the calendar was Google's.
    with caplog.at_level(logging.WARNING):
        config = config_with('home_assistant:\n  url: "http://ha:8123"\n  calendars: [calendar.personal]\n')
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert "'home_assistant.calendars'" in warning and "Google's own list" in warning and "deleted" in warning
    assert config.section("home_assistant") == {"url": "http://ha:8123"}


def test_home_assistant_activities_is_let_through_with_where_its_set_now(config_with, caplog):
    # The brain's own file may still say what the activities are called on the calendar,
    # from before each was a calendar of its own.
    with caplog.at_level(logging.WARNING):
        config = config_with('home_assistant:\n  url: "http://ha:8123"\n  activities:\n    work: ["Shift"]\n')
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert "'home_assistant.activities'" in warning and "your calendars in Google" in warning
    assert "deleted" in warning
    assert config.section("home_assistant") == {"url": "http://ha:8123"}


@pytest.mark.parametrize("own", ["weather:\n", "weather: null\n", "weather: {}\n"])
def test_the_section_left_once_a_removed_setting_is_deleted_is_let_through_too(config_with, caplog, own):
    # Deleting the line the warning names, and not the section above it, mustn't stop Nova starting.
    with caplog.at_level(logging.WARNING):
        config = config_with(own + "music:\n  player: navidrome\n")
    (warning,) = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert "'weather'" in warning and "Home Assistant" in warning and "deleted" in warning
    assert config.section("weather") == {} and config.section("music") == {"player": "navidrome"}

    # A section which never held a removed setting is still a mistake, empty or not.
    with pytest.raises(ConfigError) as caught:
        config_with("navidrom:\n")
    assert "there's no 'navidrom' section" in str(caught.value)


def test_a_setting_can_be_left_to_the_model_with_null(config_with, tmp_path):
    (tmp_path / "defaults.yaml").write_text(DEFAULTS + "llm:\n  temperature: 0.7\n")
    assert config_with("llm:\n  temperature: null\n").section("llm")["temperature"] is None


def test_a_whole_number_is_fine_for_a_setting_with_a_decimal_default(config_with):
    assert config_with("thresholds:\n  silence_duration: 1\n").section("thresholds")["silence_duration"] == 1


def test_the_real_config_yaml_reads(tmp_path):
    # Every setting the repository ships has to be readable as the defaults.
    config = Config(path=str(tmp_path / "none.yaml"))
    assert config.section("music")["player"] in ("navidrome", "spotify")


def test_the_batteries_defaults_are_checked(tmp_path):
    config = Config(path=str(tmp_path / "none.yaml"))
    batteries = config.section("batteries")
    assert batteries["mobile_marks"] == [5] and batteries["other_marks"] == [15, 5]
    assert batteries["check_every"] == 60 and batteries["stale_after"] == 3600

    def own(text):
        (tmp_path / "config.yaml").write_text(text)
        return Config(path=str(tmp_path / "config.yaml"))

    assert own("batteries:\n  mobile_marks: [10]\n  check_every: 30.5\n").section("batteries")["mobile_marks"] == [10]
    with pytest.raises(ConfigError) as caught:
        own('batteries:\n  mobile_marks: "five"\n')
    assert "'batteries.mobile_marks' should be a list, not 'five'" in str(caught.value)


@pytest.mark.parametrize("own, setting", [
    ("mobile_marks: []", "mobile_marks"),
    ("other_marks: []", "other_marks"),
    ("mobile_marks:", "mobile_marks"),
    ("mobile_marks: [0]", "mobile_marks"),
    ("other_marks: [15, 100]", "other_marks"),
    ("mobile_marks: [15, 5]", "mobile_marks"),
    ("other_marks: [15]", "other_marks"),
    ("other_marks: [20, 10, 5]", "other_marks"),
    ("other_marks: [10, 10]", "other_marks"),
    ("other_marks: [5.5]", "other_marks"),
    ("other_marks: [true]", "other_marks"),
    ('mobile_marks: ["5"]', "mobile_marks"),
    ("check_every: 0", "check_every"),
    ("check_every: -60", "check_every"),
    ("check_every:", "check_every"),
    ('stale_after: "an hour"', "stale_after"),
    ("stale_after: 0", "stale_after"),
])
def test_a_battery_setting_which_cant_work_is_refused_by_name(tmp_path, own, setting):
    (tmp_path / "config.yaml").write_text(f"batteries:\n  {own}\n")
    with pytest.raises(ConfigError) as caught:
        Config(path=str(tmp_path / "config.yaml"))
    assert f"'batteries.{setting}'" in str(caught.value)


# For each limit, what passes and what just doesn't - the table in design 0009.
IN_AND_OUT = {
    ("ntfy", "quiet_from"): (["23:59", "00:00"], ["24:00", "7:00", "11pm", 2300, ""]),
    ("ntfy", "quiet_until"): (["07:00"], ["24:00", "7:00", "11pm"]),
    ("navidrome", "url"): (["http://music.local:4533", ""], ["music.local", "ftp://music.local"]),
    ("home_assistant", "url"): (["https://ha.local:8123", ""], ["ha.local"]),
    ("ntfy", "url"): (["https://ntfy.example.com", ""], ["ntfy.example.com"]),
    ("ntfy", "phone"): (["home-lab_1", ""], ["home lab", "a/b", "x" * 65]),
    ("ntfy", "reminders"): (["home-lab_1", ""], ["home lab", "a/b"]),
    ("ntfy", "restarts"): (["home-lab_1", ""], ["home lab", "a/b"]),
    ("ntfy", "alerts"): ([["home-lab_1", "b"], []], [["home lab"], ["a/b"], [""], "homelab"]),
    ("thresholds", "silence_duration"): ([0.2, 3, 1], [0.19, 3.1, True]),
    ("conversation", "history_max_age"): ([0, 3600, 300], [-1, 3601, 1.5]),
    ("mqtt", "host"): (["192.168.1.20", "ha.local", ""], ["http://ha.local", "ha local"]),
    ("mqtt", "port"): ([1, 1883, 65535], [0, 65536, 1883.5]),
    ("voice", "speed"): ([0.75, 1.5, 1], [0.74, 1.51]),
}


def test_each_limit_is_the_tables():
    assert set(IN_AND_OUT) == set(LIMITS)
    for (section, name), (good, bad) in IN_AND_OUT.items():
        for value in good:
            assert limit_problem(section, name, value) is None, (section, name, value)
        for value in bad:
            assert limit_problem(section, name, value), (section, name, value)
    assert limit_problem("thresholds", "silence_duration", 5) == "should be from 0.2 to 3"
    assert limit_problem("mqtt", "port", 0) == "should be a whole number from 1 to 65535"
    assert limit_problem("ntfy", "quiet_from", "11pm") == "should be a time of day, from 00:00 to 23:59"
    assert limit_problem("ntfy", "url", "x") == "should be an address starting http:// or https://"
    assert limit_problem("ntfy", "phone", "a/b") == "should be an ntfy topic: letters, digits, hyphens and underscores"
    assert limit_problem("mqtt", "host", "http://x") == "should be a host name or address, without http://"
    assert limit_problem("music", "player", "anything") is None


def test_an_out_of_range_value_stops_the_start_naming_it(tmp_path):
    (tmp_path / "config.yaml").write_text("thresholds:\n  silence_duration: 5\n")
    with pytest.raises(ConfigError) as caught:
        Config(path=str(tmp_path / "config.yaml"))
    assert "'thresholds.silence_duration' should be from 0.2 to 3, not 5" in str(caught.value)


def test_the_same_quiet_hours_are_no_quiet_hours(tmp_path):
    from voice_assistant.ntfy import Ntfy

    (tmp_path / "config.yaml").write_text('ntfy:\n  quiet_from: "22:00"\n  quiet_until: "22:00"\n')
    Config(path=str(tmp_path / "config.yaml"))
    for hour in (0, 12, 22, 23):
        ntfy = Ntfy("https://ntfy.example.com", quiet_from="22:00", quiet_until="22:00",
                    now=lambda hour=hour: datetime(2026, 1, 1, hour, 0))
        assert ntfy.quiet() is False


def test_settings_that_need_each_other_are_named():
    assert clashes({"music": {"player": "navidrome"}, "navidrome": {"url": ""}}) == [
        ("navidrome.url", "Navidrome as the player needs Navidrome's address.")]
    assert clashes({"ntfy": {"url": "", "alerts": [], "phone": "me", "reminders": "", "restarts": ""}}) == [
        ("ntfy.url", "ntfy's topics need its server.")]
    assert clashes({"ntfy": {"url": "", "alerts": ["a"]}}) == [("ntfy.url", "ntfy's topics need its server.")]
    assert clashes({"music": {"player": "navidrome"}, "navidrome": {"url": "http://m"},
                    "ntfy": {"url": "https://n", "phone": "me"}}) == []
    assert clashes({"music": {"player": "spotify"}, "navidrome": {"url": ""}, "ntfy": {"url": ""}}) == []


def test_a_removed_setting_still_only_warns(config_with, caplog):
    with caplog.at_level(logging.WARNING):
        config = config_with("weather:\n  location: Paris\nthresholds:\n  silence_duration: 1\n")
    assert "weather" not in config.data
    assert "'weather.location' is no longer used" in caplog.text


@pytest.mark.parametrize("marks", ["[0]", "[100]"])
def test_battery_marks_are_from_1_to_99(tmp_path, marks):
    (tmp_path / "config.yaml").write_text(f"batteries:\n  mobile_marks: {marks}\n")
    with pytest.raises(ConfigError, match="mobile_marks.*1 to 99"):
        Config(path=str(tmp_path / "config.yaml"))


def test_update_checks_the_settings_before_stopping_nova():
    text = (Path(__file__).parent.parent / "update.sh").read_text()
    check_at = text.index("\nsettings_check || exit 1\n")
    assert check_at < text.index("\nstop_service\n")


def _run_update_check(tmp_path, own, python=None):
    """Runs update.sh's own check block against a scratch config, as the Pi's user would."""
    root = Path(__file__).parent.parent
    text = (root / "update.sh").read_text()
    block = text[text.index("# BEGIN settings check"):text.index("# END settings check")]
    (tmp_path / "config.yaml").write_text(own)
    script = ('problem() { echo "PROBLEM $*" >&2; }\nas_user() { "$@"; }\n' + block
              + "settings_check\necho \"status $?\"\n")
    env = {**os.environ, "NOVA_CONFIG": str(tmp_path), "PROJECT_DIRECTORY": str(root),
           "PYTHON": python or sys.executable}
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_update_check_refuses_bad_settings_and_passes_good_ones(tmp_path):
    good = _run_update_check(tmp_path, "thresholds:\n  silence_duration: 1\n")
    assert "status 0" in good.stdout and "PROBLEM" not in good.stderr

    bad = _run_update_check(tmp_path, "thresholds:\n  silence_duration: 5\n")
    assert "status 1" in bad.stdout
    assert "settings won't start it" in bad.stderr
    assert "'thresholds.silence_duration' should be from 0.2 to 3, not 5" in bad.stderr
    assert "Traceback" not in bad.stderr


def test_update_check_tells_broken_code_from_bad_settings(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/bash\necho \"ModuleNotFoundError: No module named 'yaml'\"\nexit 2\n")
    fake.chmod(0o755)
    broken = _run_update_check(tmp_path, "", python=str(fake))
    assert "status 1" in broken.stdout
    assert "whatever the settings" in broken.stderr and "settings won't start it" not in broken.stderr


@pytest.mark.parametrize("setting, value", [
    ("mqtt.port", ".inf"), ("mqtt.port", ".nan"), ("mqtt.port", "-.inf"),
    ("conversation.history_max_age", ".nan"), ("thresholds.silence_duration", ".inf"),
    ("voice.speed", ".nan"),
])
def test_infinity_and_nan_are_refused_with_the_settings_own_message(tmp_path, setting, value):
    section, name = setting.split(".")
    (tmp_path / "config.yaml").write_text(f"{section}:\n  {name}: {value}\n")
    with pytest.raises(ConfigError, match=f"'{setting}' should be"):
        Config(path=str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("text", ["Infinity", "-Infinity", "NaN"])
def test_infinity_and_nan_from_the_pages_json_get_a_sentence_too(text):
    value = json.loads(text)
    for section, name in (("mqtt", "port"), ("thresholds", "silence_duration"),
                          ("conversation", "history_max_age")):
        assert limit_problem(section, name, value).startswith("should be")


def test_a_bare_scheme_is_not_an_address():
    for scheme in ("http://", "https://"):
        assert limit_problem("ntfy", "url", scheme)
    assert limit_problem("ntfy", "url", "http://a") is None


def test_an_unquoted_time_gets_the_time_sentence(tmp_path):
    (tmp_path / "config.yaml").write_text("ntfy:\n  quiet_from: 23:00\n")
    with pytest.raises(ConfigError) as caught:
        Config(path=str(tmp_path / "config.yaml"))
    assert "'ntfy.quiet_from' should be a time of day, from 00:00 to 23:59, not 1380" in str(caught.value)


@pytest.mark.parametrize("setting", ["mqtt.port", "conversation.history_max_age",
                                     "thresholds.silence_duration", "voice.speed"])
def test_a_huge_whole_number_is_just_out_of_range(tmp_path, setting):
    section, name = setting.split(".")
    for value in (10**400, -10**400):
        assert limit_problem(section, name, value).startswith("should be")
        assert limit_problem(section, name, json.loads(str(value))).startswith("should be")
    (tmp_path / "config.yaml").write_text(f"{section}:\n  {name}: {'9' * 401}\n")
    with pytest.raises(ConfigError, match=f"'{setting}' should be"):
        Config(path=str(tmp_path / "config.yaml"))


def test_apply_changes_only_what_it_is_given(config_with):
    # The settings page's saves go into the running settings, while every satellite's session
    # reads them on its own thread - so a section is never changed under a reader.
    config = config_with("navidrome:\n  url: http://music.local:4533\n")
    before = config.section("thresholds")
    config.apply({("thresholds", "silence_duration"): 1.2, ("music", "player"): "navidrome"})

    assert before == {"silence_duration": 0.6, "vad_threshold": 0.5}
    assert config.section("thresholds") == {"silence_duration": 1.2, "vad_threshold": 0.5}
    assert config.section("music") == {"player": "navidrome"}
    assert config.section("navidrome") == {"url": "http://music.local:4533", "username": "nova"}
