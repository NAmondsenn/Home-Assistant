"""
Config Test
Checks settings are read from config.yaml, with the install's own on top, and that a
mistake in the install's file is caught rather than ignored.
"""

import json
import logging
import os
import signal
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
    # Past Python's 4,300 digits, which it won't read as a number.
    pytest.param("thresholds:\n  silence_duration: " + "9" * 5000 + "\n", "isn't valid YAML", id="too-long-a-number"),
])
def test_a_mistake_stops_it_starting_and_says_where(config_with, own, said):
    with pytest.raises(ConfigError) as caught:
        config_with(own)
    assert said in str(caught.value) and "config.yaml" in str(caught.value)


def test_a_file_which_isnt_utf_8_stops_it_starting_and_says_where(config_with, tmp_path):
    # Saved by an editor in Latin-1, "café" can't be read - said as a mistake, not a traceback.
    (tmp_path / "config.yaml").write_bytes("music:\n  player: café\n".encode("latin-1"))
    with pytest.raises(ConfigError) as caught:
        config_with()
    assert "isn't valid YAML" in str(caught.value) and "config.yaml" in str(caught.value)


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
    ("other_marks: [5.5, 5]", "other_marks"),
    ("other_marks: [true, 5]", "other_marks"),
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
    ("thresholds", "recording_timeout"): ([5, 30, 15, 7.5], [4, 30.1, True, 0, -1]),
    ("thresholds", "wake_word_confidence"): ([0.3, 0.95, 0.5], [0.2, 0.29, 0.97, 1, True, 0]),
    ("thresholds", "media_wake_word_confidence"): ([0.3, 0.95, 0.75], [0.2, 0.29, 0.97, 1, True, 0]),
    ("thresholds", "voice_check"): ([0, 0.2, 0.95], [-0.1, 0.96, 1, True]),
    ("conversation", "history_max_age"): ([0, 3600, 300], [-1, 3601, 1.5]),
    ("mqtt", "host"): (["192.168.1.20", "ha.local", ""], ["http://ha.local", "ha local"]),
    ("mqtt", "port"): ([1, 1883, 65535], [0, 65536, 1883.5]),
    ("voice", "speed"): ([0.75, 1.5, 1], [0.74, 1.51]),
    ("settings_page", "proxy_port"): ([0, 8766, 65535], [-1, 65536, 8766.5, True]),
    ("soulsync", "url"): (["http://soulsync.local:8008", ""], ["soulsync.local", "ftp://soulsync.local"]),
    ("music", "song_download_minutes"): ([1, 10, 60], [0, 61, 10.5, True]),
    ("music", "album_download_minutes"): ([1, 30, 240], [0, 241, 30.5, True]),
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


def test_the_download_settings_have_their_limits():
    assert limit_problem("music", "song_download_minutes", 0) == "should be a whole number from 1 to 60"
    assert limit_problem("music", "album_download_minutes", 241) == "should be a whole number from 1 to 240"
    assert limit_problem("music", "song_download_minutes", 10) is None
    assert limit_problem("soulsync", "url", "soulsync.local") == "should be an address starting http:// or https://"


def test_the_repositorys_config_has_the_download_settings(tmp_path):
    config = Config(path=str(tmp_path / "none.yaml"))
    music = config.section("music")
    assert music["offer_downloads"] is True
    assert (music["song_download_minutes"], music["album_download_minutes"]) == (10, 30)
    # No address until the user gives SoulSync's, so downloading says it isn't set up.
    assert config.section("soulsync")["url"] == ""


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


# What update.sh has from setup.sh, stood in for so its blocks run on their own.
UPDATE_STAND_INS = 'problem() { echo "PROBLEM $*" >&2; }\nas_user() { "$@"; }\n'


def _update_block(text, name):
    """The part of update.sh's text between its "# BEGIN name" and "# END name" lines."""
    return text[text.index(f"# BEGIN {name}"):text.index(f"# END {name}")]


def _run_update_check(tmp_path, own, python=None, project=None, updated_from=None):
    """
    Runs update.sh's own check block against a scratch config, as the Pi's user would -
    in the project given, a scratch clone, and as just updated from the commit given.
    """
    root = Path(__file__).parent.parent
    text = (root / "update.sh").read_text()
    # The take-back block first, as settings_check calls its take_back.
    block = _update_block(text, "take back") + _update_block(text, "settings check")
    (tmp_path / "config.yaml").write_text(own)
    script = (UPDATE_STAND_INS + block + "settings_check\necho \"status $?\"\n")
    env = {**os.environ, "NOVA_CONFIG": str(tmp_path), "PROJECT_DIRECTORY": str(project or root),
           "PYTHON": python or sys.executable,
           # The code checked is this one's, wherever the project is.
           "PYTHONPATH": os.pathsep.join(filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
    env.pop("NOVA_UPDATED_FROM", None)
    if updated_from:
        env["NOVA_UPDATED_FROM"] = updated_from
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def _updated_clone(tmp_path):
    """A scratch clone just pulled, as update.sh leaves it: the commit before, and the update."""
    clone = tmp_path / "clone"
    clone.mkdir()

    def git(*arguments):
        return subprocess.run(["git", "-C", str(clone), "-c", "user.name=Nova", "-c", "user.email=nova@example.com",
                               *arguments], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (clone / "version").write_text("before\n")
    git("add", "version")
    git("commit", "-q", "-m", "Before")
    before = git("rev-parse", "HEAD")
    (clone / "version").write_text("after\n")
    git("commit", "-q", "-a", "-m", "The update")
    return clone, before, git


def test_update_check_refuses_bad_settings_and_passes_good_ones(tmp_path):
    good = _run_update_check(tmp_path, "thresholds:\n  silence_duration: 1\n")
    assert "status 0" in good.stdout and "PROBLEM" not in good.stderr

    bad = _run_update_check(tmp_path, "thresholds:\n  silence_duration: 5\n")
    assert "status 1" in bad.stdout
    assert "settings won't start the new code" in bad.stderr and "is in place" in bad.stderr
    assert "'thresholds.silence_duration' should be from 0.2 to 3, not 5" in bad.stderr
    assert "Traceback" not in bad.stderr


def test_update_check_tells_broken_code_from_bad_settings(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/bash\necho \"ModuleNotFoundError: No module named 'yaml'\"\nexit 2\n")
    fake.chmod(0o755)
    broken = _run_update_check(tmp_path, "", python=str(fake))
    assert "status 1" in broken.stdout
    assert "whatever the settings" in broken.stderr and "settings won't start" not in broken.stderr


@pytest.mark.parametrize("own, said", [("thresholds:\n  silence_duration: 5\n", "settings won't start the new code"),
                                       ("", "whatever the settings")], ids=["settings", "code"])
def test_a_refused_update_takes_the_new_code_back_out(tmp_path, own, said):
    # Nova goes on running the old code, so whatever restarts it next - the page's
    # Restart now, the watchdog, a power cut - must start that too, not the code refused.
    clone, before, git = _updated_clone(tmp_path)
    python = None
    if not own:
        python = tmp_path / "python"
        python.write_text("#!/bin/bash\necho \"ModuleNotFoundError: No module named 'yaml'\"\nexit 2\n")
        python.chmod(0o755)
    refused = _run_update_check(tmp_path, own, python=python and str(python), project=clone, updated_from=before)

    assert "status 1" in refused.stdout and said in refused.stderr
    assert "taken back out" in refused.stderr
    assert git("rev-parse", "HEAD") == before and (clone / "version").read_text() == "before\n"
    # Still on its branch, so the next update pulls the code again.
    assert git("symbolic-ref", "--short", "HEAD") == "main"


def test_an_update_whose_settings_are_good_keeps_the_new_code(tmp_path):
    clone, before, git = _updated_clone(tmp_path)
    passed = _run_update_check(tmp_path, "thresholds:\n  silence_duration: 1\n", project=clone, updated_from=before)
    assert "status 0" in passed.stdout and (clone / "version").read_text() == "after\n"


def _run_steps(tmp_path, step, project, updated_from=None, wait_for=None, python=None):
    """
    Runs update.sh's take-back and settings-check blocks, then one step watched as
    update.sh watches its steps and its settings check - in the project given, a scratch
    clone, and as just updated from the commit given.

    Args:
        tmp_path: The test's own scratch folder.
        step: The shell command standing in for update.sh's steps.
        project: The scratch clone the steps run in.
        updated_from: The commit the clone was updated from, if it was pulled.
        wait_for: A line to wait for on stdout, to signal the steps mid-way; with it,
            the script is started in a process group of its own and returned running.
        python: A stand-in for Nova's Python, for a step that runs the settings check.

    Returns:
        The finished run, or with wait_for, the running process, its line read.
    """
    text = (Path(__file__).parent.parent / "update.sh").read_text()
    script = ("set -euo pipefail\n" + UPDATE_STAND_INS + _update_block(text, "take back")
              + _update_block(text, "settings check") + f"watch_steps\n{step}\nsteps_finished\necho finished\n")
    env = {**os.environ, "PROJECT_DIRECTORY": str(project), "PYTHON": python or sys.executable}
    env.pop("NOVA_UPDATED_FROM", None)
    if updated_from:
        env["NOVA_UPDATED_FROM"] = updated_from
    if wait_for is None:
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path)
    # A session of its own, so its process group is this script and its step alone -
    # the one group the test may signal.
    proc = subprocess.Popen(["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=tmp_path, start_new_session=True)
    try:
        assert proc.stdout.readline().strip() == wait_for
    except BaseException:
        _end_group(proc)
        raise
    return proc


def _end_group(proc):
    """Kills the process group the test started, only while its leader is unreaped and so still ours."""
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()


def test_a_failed_step_after_the_pull_takes_the_new_code_back_out(tmp_path):
    # A package install or a model download failing leaves Nova running the old code,
    # so a reboot must start that too, not the new code it never checked.
    clone, before, git = _updated_clone(tmp_path)
    failed = _run_steps(tmp_path, "false", clone, updated_from=before)

    assert failed.returncode != 0 and "finished" not in failed.stdout
    assert "A step after fetching the new code failed" in failed.stderr and "taken back out" in failed.stderr
    assert git("rev-parse", "HEAD") == before and (clone / "version").read_text() == "before\n"
    # Still on its branch, so the next update pulls the code again.
    assert git("symbolic-ref", "--short", "HEAD") == "main"


def test_a_failed_step_with_no_pull_leaves_the_code_as_it_is(tmp_path):
    # Run with --no-pull, on code checked out by hand, so there's nothing to go back to.
    clone, before, git = _updated_clone(tmp_path)
    after = git("rev-parse", "HEAD")
    failed = _run_steps(tmp_path, "false", clone)

    assert failed.returncode != 0
    assert "A step of the update failed" in failed.stderr and "is in place" in failed.stderr
    # Nothing was fetched, so there's no new code to speak of, nor old code it might not suit.
    assert "fetching" not in failed.stderr and "may not start" not in failed.stderr
    assert "taken back out" not in failed.stderr
    assert git("rev-parse", "HEAD") == after and git("symbolic-ref", "--short", "HEAD") == "main"


def test_a_failure_before_any_package_is_installed_doesnt_say_packages_stay_installed(tmp_path):
    # A role file that can't be read fails the run started again on the new code before
    # any step has run, so there is nothing yet for the line to be true of.
    clone, before, git = _updated_clone(tmp_path)
    early = _run_steps(tmp_path, "false", clone, updated_from=before)
    late = _run_steps(tmp_path, "packages_started=true\nfalse", clone, updated_from=before)

    assert early.returncode != 0 and "taken back out" in early.stderr
    assert "Python packages" not in early.stderr
    assert late.returncode != 0 and "taken back out" in late.stderr
    assert "Python packages it installed stay installed" in late.stderr


def test_the_take_back_message_always_ends_its_sentence(tmp_path):
    # Said on its own when nothing follows it, as after a step failed, so it can't end in a
    # colon which leads nowhere.
    clone, before, git = _updated_clone(tmp_path)
    runs = [
        _run_steps(tmp_path, "false", clone, updated_from=before),
        _run_steps(tmp_path, "packages_started=true\nfalse", clone, updated_from=before),
        _run_steps(tmp_path, "false", clone),
        _run_update_check(tmp_path, "thresholds:\n  silence_duration: 5\n", project=clone, updated_from=before),
        _run_update_check(tmp_path, "thresholds:\n  silence_duration: 5\n", project=clone),
    ]

    for run in runs:
        assert "PROBLEM" in run.stderr
        assert not [line for line in run.stderr.splitlines() if line.rstrip().endswith(":")], run.stderr


@pytest.mark.parametrize("sig, status", [(signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)],
                         ids=["hangup", "interrupt", "terminate"])
def test_an_update_cut_off_mid_step_takes_the_new_code_back_out(tmp_path, sig, status):
    # The likeliest failure: the phone's session dropping mid-step. A dropped terminal
    # signals its whole group, and bash waits for the step in the foreground before it
    # runs a trap, so signalling bash alone would only wait out the sleep.
    clone, before, git = _updated_clone(tmp_path)
    proc = _run_steps(tmp_path, "bash -c 'echo started; sleep 30'", clone, updated_from=before, wait_for="started")
    try:
        assert os.getpgid(proc.pid) == proc.pid
        os.killpg(proc.pid, sig)
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        _end_group(proc)

    assert proc.returncode == status and "finished" not in stdout
    assert "taken back out" in stderr
    assert git("rev-parse", "HEAD") == before


def _stand_in_python(tmp_path, body):
    """A stand-in for Nova's Python, which runs the shell given whatever it's asked."""
    python = tmp_path / "python"
    python.write_text(f"#!/bin/bash\n{body}\n")
    python.chmod(0o755)
    return str(python)


def test_an_update_cut_off_during_the_settings_check_takes_the_new_code_back_out(tmp_path):
    # The check takes a second or so to start Python, and the new code hasn't passed it
    # yet, so a session dropped then must leave the old code for a reboot as well.
    clone, before, git = _updated_clone(tmp_path)
    # The check keeps what Python says to itself, so the stand-in says it has started on
    # a copy of the script's own stdout.
    python = _stand_in_python(tmp_path, "echo started >&3\nsleep 30")
    proc = _run_steps(tmp_path, "exec 3>&1\nsettings_check || exit 1", clone, updated_from=before,
                      wait_for="started", python=python)
    try:
        assert os.getpgid(proc.pid) == proc.pid
        os.killpg(proc.pid, signal.SIGHUP)
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        _end_group(proc)

    assert proc.returncode == 129 and "finished" not in stdout
    assert "taken back out" in stderr
    assert git("rev-parse", "HEAD") == before


def test_settings_refused_while_watched_are_taken_back_and_said_once(tmp_path):
    # The settings check says its own piece, so the watch over the steps stands down
    # first rather than taking the code back, and saying so, a second time.
    clone, before, git = _updated_clone(tmp_path)
    python = _stand_in_python(tmp_path, "echo \"'thresholds.silence_duration' should be from 0.2 to 3, not 5\"\nexit 1")
    refused = _run_steps(tmp_path, "settings_check || exit 1", clone, updated_from=before, python=python)

    assert refused.returncode == 1 and "finished" not in refused.stdout
    assert "settings won't start the new code" in refused.stderr
    assert refused.stderr.count("taken back out") == 1 and "A step" not in refused.stderr
    assert git("rev-parse", "HEAD") == before


def test_the_settings_check_never_leaves_a_hangup_unwatched_before_its_take_back():
    # Standing the watch down puts the signals back to bash's own, which ends the run
    # with nothing taken back - so they're ignored before the watch's traps go, not after.
    block = _update_block((Path(__file__).parent.parent / "update.sh").read_text(), "settings check")
    refused = block[block.index('    if [ "$status" = 1 ]; then'):block.index("    take_back ")]
    assert "\n    steps_finished\n" not in refused
    assert refused.index("trap '' HUP INT TERM") < refused.index("steps_done=true") < refused.index("trap - EXIT")


def test_steps_that_finish_leave_the_new_code_in(tmp_path):
    clone, before, git = _updated_clone(tmp_path)
    finished = _run_steps(tmp_path, "true", clone, updated_from=before)

    assert finished.returncode == 0 and "finished" in finished.stdout
    assert "PROBLEM" not in finished.stderr and (clone / "version").read_text() == "after\n"


def test_the_trap_covers_the_steps_and_the_settings_check():
    # Watched from before decide_role in the run started again on the new code (exec drops
    # the traps, so the run before can't), and from the steps in any other; and until the
    # settings check has passed, before Nova is stopped.
    text = (Path(__file__).parent.parent / "update.sh").read_text()
    assert text.index("# END take back") < text.index("    watch_steps\n") < text.index("\ndecide_role\n")
    assert text.index('if [ "$PULL" = false ] && [ -n "${NOVA_UPDATED_FROM:-}" ]; then\n    watch_steps\n') >= 0
    # With the newline, as the comment at the top names run_steps too.
    assert text.index('done_ "On') < text.index("\nwatch_steps\n") < text.index("\nrun_steps ")
    assert (text.index("\nsettings_check || exit 1\n") < text.index("\nsteps_finished\n")
            < text.index("\nstop_service\n"))


def test_update_says_what_it_updated_from_when_it_starts_again_on_the_new_code():
    text = (Path(__file__).parent.parent / "update.sh").read_text()
    assert text.index('export NOVA_UPDATED_FROM="$before"') < text.index('exec bash "${PROJECT_DIRECTORY}/update.sh"')


@pytest.mark.parametrize("role, shown", [
    ("brain", "root_do journalctl -u nova-backend -n 12 --no-pager -o cat"),
    ("satellite", "journalctl --user-unit voice-assistant -n 12 --no-pager -o cat"),
])
def test_update_reads_the_brains_journal_as_root(role, shown):
    # Read when Nova didn't start - and a user outside the journal's group would be shown
    # nothing of why, as the brain's other service steps are done as root for them.
    text = (Path(__file__).parent.parent / "update.sh").read_text()
    chosen = text[text.index('if [ "$ROLE" = "brain" ]; then\n    stop_service()'):
                  text.index("# The new code reads the settings")]
    script = ('root_do() { echo "root_do $*"; }\njournalctl() { echo "journalctl $*"; }\nsystemctl() { :; }\n'
              f"ROLE={role} BRAIN_SERVICE_NAME=nova-backend SERVICE_NAME=voice-assistant\n{chosen}show_log 12\n")
    shown_by = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert shown_by.stdout.strip() == shown, shown_by.stderr


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
