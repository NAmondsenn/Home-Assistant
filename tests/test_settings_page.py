"""
Settings Page Test
Checks the settings page shows what it should - and never a key - saves only what
Nova would accept when it starts, and only lets the user in, and only changes made
from the page itself: over a real server on this machine, with stand-ins for the
rest of Nova, and its files in a folder of the test's own.
"""

import http.client
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
import yaml

from voice_assistant import logging_setup, paths
from voice_assistant.batteries import BatteryChoices, BatteryWatcher
from voice_assistant import settings_page as page_module
from voice_assistant.brain import Brain
from voice_assistant.config import Config
from voice_assistant.ntfy import Ntfy
from voice_assistant.routines import Routines
from voice_assistant.settings_page import LATER, NOW, SettingsPage

LOGIN = "you@example.com"


class FakeVolume:
    CATEGORIES = ("general", "alarm", "music")

    def __init__(self):
        self.levels = {"general": 60, "alarm": 80, "music": 40}

    def percent(self, category):
        return self.levels[category]

    def get(self, category):
        return self.levels[category] / 100

    def set(self, category, percent):
        self.levels[category] = percent
        return {"success": True, "message": f"Volume {percent}%."}


class FakeClock:
    def __init__(self):
        self.removed = []
        self.alarms = []

    def overview(self, satellite=None):
        self.overviewed = satellite
        return {"timers": [{"id": 1, "name": "pizza reminder", "due_at": 1_800_000_000}], "alarms": []}

    def remove(self, kind, item_id):
        self.removed.append((kind, item_id))
        return {"success": True, "message": "Removed."}

    def set_alarm(self, hour, minute, repeat=None, satellite=None):
        self.alarms.append((hour, minute, repeat, satellite))
        return {"success": True, "message": "Alarm set."}


class FakeMemory:
    def __init__(self):
        self.known = ["Your sister is called Amy."]

    def facts(self):
        return list(self.known)

    def forget(self, what):
        self.known.remove(what)
        return {"success": True, "message": "Forgotten."}


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Nova's settings, keys and logs, in a folder of the test's own."""
    monkeypatch.setattr(paths, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(paths, "SECRETS_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path


class FakePlayer:
    """Stands in for Navidrome, which keeps the music's volume itself."""

    needs_internet = False
    name = "Navidrome"

    def __init__(self):
        self.level = 40
        self.asked_for_playlists = 0

    def get_volume(self):
        return self.level

    def set_volume(self, percent):
        self.level = int(percent)
        return {"success": True, "message": f"Music volume {self.level} percent."}

    def now_playing(self):
        return {"track": "Gooba", "artist": "6ix9ine", "album": "Gooba", "playing": True, "position": 12.0,
                "duration": 125, "cover": "al-1"}

    def playlists(self):
        self.asked_for_playlists += 1
        return ["Chill", "Workout"]

    def cover(self):
        return b"a cover", "image/jpeg"


class FakeActions:
    """Stands in for the actions asking out loud goes through, noting what was asked."""

    def __init__(self):
        self.asked = []

    def play_music(self, query=None):
        self.asked.append(("play", query))
        return {"success": True, "message": f"Playing {query}" if query else "Resumed playback", "silent": True}

    def play_playlist(self, name=""):
        self.asked.append(("playlist", name))
        return {"success": True, "message": f"Playing {name}"}

    def control_playback(self, action=None):
        self.asked.append((action, None))
        return {"success": True, "message": "Done."}


class FakeHome:
    """Stands in for Home Assistant."""

    def __init__(self):
        self.changed = []
        self.area = "Bedroom"

    def overview(self):
        return {"success": True, "lights": [{"id": "light.kitchen", "name": "Kitchen", "on": True}],
                "routines": [{"id": "scene.relax", "name": "Relax", "kind": "scene"}]}

    def set_light(self, entity_id, on=None, brightness=None, hue=None, saturation=None, kelvin=None):
        self.changed.append((entity_id, on, brightness) if hue is saturation is kelvin is None
                            else (entity_id, hue, saturation, kelvin))
        return {"success": True, "message": "Done."}

    def run(self, entity_id):
        self.changed.append((entity_id, "run", None))
        return {"success": True, "message": "Set Relax."}


class FakeSession:
    """Stands in for one connected satellite's session: its volumes, music, lights and mute."""

    def __init__(self, music=None, actions=None, home=None, activity="idle"):
        self.volume = FakeVolume()
        self.music_player, self.actions, self.home = music, actions, home
        self.activity = activity
        self.muted = False
        self.booped = []

    def set_muted(self, muted):
        self.muted = muted

    def boop(self, level):
        self.booped.append(level)


class FakeBrain:
    """Stands in for the brain: what every satellite shares, and the sessions of those connected."""

    def __init__(self, sessions, known=None, **parts):
        self.sessions = sessions
        self.known = list(sessions) if known is None else known
        self.clock, self.memory = FakeClock(), FakeMemory()
        self.ntfy = self.stt = self.routines = self.batteries = self.config = None
        self._night = Ntfy("")
        self.ran = []
        for name, part in parts.items():
            setattr(self, name, part)

    def satellites(self):
        return list(self.known)

    def default_satellite(self):
        return self.known[0] if self.known else None

    def session_for(self, satellite):
        return self.sessions.get(satellite or self.default_satellite())

    def run_routine(self, routine):
        self.ran.append(routine)

    def apply_settings(self, changes):
        # The brain's own, so a change reaches ntfy and the logs as it does on the brain.
        return Brain.apply_settings(self, changes)


def _page(running=None, sessions=None, known=None, wake_word=None, **parts):
    """A page over a brain with one satellite connected, the bedroom - or the sessions given."""
    if sessions is None:
        sessions = {"bedroom": FakeSession()}
    brain = FakeBrain(sessions, known, **parts)
    # The login as Tailscale might capitalise it, which mustn't matter.
    page = SettingsPage([LOGIN.upper()], port=0, running=running, brain=brain, wake_word=wake_word,
                        restart=lambda: "Restarting.", started=1_800_000_000)
    return page


def _bedroom(page):
    return page.brain.sessions["bedroom"]


# What it shows

def test_the_page_shows_whats_set_and_never_a_key(files):
    (files / ".env").write_text('HA_TOKEN="the-secret-token"\nClaude_API_Key=sk-ant-secret\nNTFY_TOKEN=\n')
    state = _page().state()

    keys = {key["name"]: key["set"] for key in state["keys"]}
    # Claude's key counts under its older name too.
    assert keys["HA_TOKEN"] and keys["ANTHROPIC_API_KEY"] and not keys["NTFY_TOKEN"] and not keys["NAVIDROME_PASSWORD"]
    shown = json.dumps(state)
    assert "the-secret-token" not in shown and "sk-ant-secret" not in shown

    assert state["volumes"] == {"general": 60, "alarm": 80, "music": 40}
    assert state["memories"] == ["Your sister is called Amy."]
    assert state["clock"]["timers"][0]["name"] == "pizza reminder"
    assert {"navidrome.url", "ntfy.alerts"} <= {setting["key"] for setting in state["settings"]}
    # What belongs to a satellite - the brain's address, and the satellite's own password - isn't on the brain's page.
    assert "backend.address" not in {setting["key"] for setting in state["settings"]}
    assert "NOVA_BACKEND_TOKEN" not in keys
    # What can only be one of a few words comes with them, to pick from.
    player = next(setting for setting in state["settings"] if setting["key"] == "music.player")
    assert player["choices"] == ["navidrome", "spotify"]


@pytest.mark.parametrize("loaded, said", [(True, "ready"), (False, "missing")])
def test_speech_to_text_is_nemotron_on_the_brain(files, loaded, said):
    # It asked for a backend's reachable, which Nemotron hasn't got - so the tile said
    # "This Pi - No backend set up" since the brain took over.
    status = _page(stt=SimpleNamespace(loaded=loaded)).state()["status"]
    assert status["speech_to_text"] == said and "backend" not in status


def test_the_latest_problems_in_the_log_are_shown_newest_first(files):
    (files / "logs").mkdir()
    (files / "logs" / "backend.log").write_text(
        "2026-10-20 16:50:01,100 - main - INFO - Transcription complete: 'what's on today'\n"
        "2026-10-20 16:51:58,131 - voice_assistant.dropouts - WARNING - The speaker ran dry\n"
        "2026-10-20 17:02:00,001 - voice_assistant.ntfy - ERROR - Couldn't say the alert\n")
    problems = _page().state()["status"]["problems"]

    assert [(problem["time"], problem["level"]) for problem in problems] == [("2026-10-20 17:02", "error"),
                                                                              ("2026-10-20 16:51", "warning")]
    # What's said is only ever logged as information, so it's never shown here.
    assert "what's on today" not in json.dumps(problems)


def test_how_quickly_nova_replies_is_from_the_last_replies_alone(files, monkeypatch):
    # Read from the end of latency.jsonl, rather than the whole of it every 20 seconds.
    (files / "logs").mkdir()
    lines = [{"marks_ms": {"speech_ended": 0, "tts_done": 9000}}] * 50 + [
        {"marks_ms": {"speech_ended": 0, "tts_done": 1000}}] * page_module.REPLIES_COUNTED
    (files / "logs" / "latency.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    asked = []
    loaded = page_module.timeline.load
    monkeypatch.setattr(page_module.timeline, "load", lambda path, most=None: asked.append(most) or loaded(path, most))

    status = _page().state()["status"]
    assert (status["reply_ms"], status["replies"]) == (1000, page_module.REPLIES_COUNTED)
    assert asked == [page_module.REPLIES_COUNTED]


def test_a_restart_is_asked_for_once_whats_saved_differs_from_whats_running(files):
    running = Config()
    page = _page(running=running)
    assert not page.state()["restart_needed"]
    page.save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)
    assert page.state()["restart_needed"]


def test_looking_at_the_settings_isnt_logged(files, caplog):
    # The page asks every 20 seconds while it's open, which was a "Settings loaded"
    # line each time - thousands a day, burying what the log is for.
    (files / "config.yaml").write_text("navidrome:\n  url: http://music.local:4533\n")
    page = _page(running=Config())
    with caplog.at_level(logging.INFO):
        caplog.clear()
        page.state()
    assert "Settings loaded" not in caplog.text


# What it changes

def test_a_volume_change_is_heard_at_its_new_level(files):
    # So the user hears how loud it now is, on Nova's own speaker, as they drag.
    page = _page()
    page.set_volume("alarm", 30, LOGIN)
    assert _bedroom(page).volume.levels["alarm"] == 30 and _bedroom(page).booped == [0.3]


def test_the_music_volume_is_the_players_own_so_a_playing_song_changes_too(files):
    player = FakePlayer()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and _bedroom(page).booped == [0.25]
    assert page.state()["volumes"]["music"] == 25


def test_spotifys_volume_is_shown_as_last_set_without_asking_spotify(files):
    # Asking Spotify's servers every time the page - or Home Assistant - looks would be
    # slow, so Nova keeps its own note of the level, which follows each change.
    class Spotify(FakePlayer):
        needs_internet = True

        def get_volume(self):
            raise AssertionError("Spotify was asked for its volume")

    player = Spotify()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and page.state()["volumes"]["music"] == 25


def test_the_page_can_tell_what_nova_is_doing(files):
    assert _page(sessions={"bedroom": FakeSession(activity="listening")}).live() == {"activity": "listening"}
    assert _page().live() == {"activity": "idle"}
    # A satellite which isn't connected isn't doing anything.
    assert _page(sessions={}, known=["bedroom"]).live() == {"activity": "idle"}


def test_the_page_is_told_whats_playing_and_the_playlists_are_kept_a_while(files):
    player = FakePlayer()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    state = page.music_state()
    assert state["player"] == "Navidrome" and state["now"]["track"] == "Gooba" and state["playlists"] == ["Chill", "Workout"]
    page.music_state()
    assert player.asked_for_playlists == 1
    assert _page().music_state() == {"available": False}


def test_the_music_buttons_go_through_the_same_actions_as_asking_out_loud(files):
    actions = FakeActions()
    page = _page(sessions={"bedroom": FakeSession(music=FakePlayer(), actions=actions)})
    page.play("play", LOGIN, query=" Gooba by 6ix9ine ")
    page.play("play", LOGIN)
    page.play("playlist", LOGIN, playlist="Workout")
    page.play("pause", LOGIN)
    assert actions.asked == [("play", "Gooba by 6ix9ine"), ("play", None), ("playlist", "Workout"), ("pause", None)]
    assert page.play("eject", LOGIN) == {"success": False, "message": "That isn't something the music does."}
    assert not _page().play("pause", LOGIN)["success"]


def test_the_lights_and_scenes_come_from_home_assistant_and_go_back_to_it(files):
    home = FakeHome()
    page = _page(sessions={"bedroom": FakeSession(home=home)})
    assert page.home_state()["lights"][0]["id"] == "light.kitchen"
    page.set_light("light.kitchen", LOGIN, on=False)
    page.set_light("light.kitchen", LOGIN, brightness="40")
    page.run("scene.relax", LOGIN)
    assert home.changed == [("light.kitchen", False, None), ("light.kitchen", None, 40), ("scene.relax", "run", None)]
    # Only a yes or no switches a light.
    assert not page.set_light("light.kitchen", LOGIN, on="off")["success"]
    assert page.home_state()["area"] == "Bedroom"
    assert _page().home_state() == {"available": False}
    assert not _page().run("scene.relax", LOGIN)["success"]


def test_a_routine_is_made_run_and_deleted_from_the_page(files):
    page = _page(routines=Routines(path=str(files / "routines.json")))
    ran = page.brain.ran
    made = page.save_routine({"name": "Good morning", "phrases": "good morning", "steps": "what's the weather"}, LOGIN)
    assert made == {"success": True, "message": "Saved Good morning."}
    routine = page.state()["routines"][0]

    assert page.start_routine(routine["id"], LOGIN) == {"success": True, "message": "Running Good morning."}
    deadline = time.monotonic() + 2
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [running["name"] for running in ran] == ["Good morning"]

    assert page.remove_routine(routine["id"], LOGIN)["success"] and page.state()["routines"] == []
    assert not page.start_routine(routine["id"], LOGIN)["success"]
    assert not page.save_routine("Good morning", LOGIN)["success"]


def test_the_page_says_what_nova_is_waiting_to_hear(files):
    # openWakeWord's stand-in, when Nova's own model is missing.
    assert _page(wake_word=lambda: "Hey Jarvis").state()["wake_word"] == "Hey Jarvis"
    assert _page().state()["wake_word"] == "Hey Nova"


def test_a_key_replaces_the_old_one_and_stays_private(files, caplog):
    (files / ".env").write_text("HA_TOKEN=old-token\nNAVIDROME_PASSWORD=keep-me\n")
    page = _page()

    assert page.set_key("HA_TOKEN", 'new"token', LOGIN)["success"]

    saved = (files / ".env").read_text()
    assert 'HA_TOKEN="new\\"token"' in saved and "old-token" not in saved and "NAVIDROME_PASSWORD=keep-me" in saved
    assert stat.S_IMODE(os.stat(files / ".env").st_mode) == 0o600
    assert 'new"token' not in caplog.text
    assert page.state()["restart_needed"]


@pytest.mark.parametrize("name, value", [("PATH", "/tmp"), ("HA_TOKEN", "two\nlines"), ("HA_TOKEN", "   "),
                                         # Which python-dotenv would read as a variable, even in quotes.
                                         ("HA_TOKEN", "with ${HOME} in it")])
def test_only_novas_own_keys_are_saved_and_nothing_odd(files, name, value):
    assert not _page().set_key(name, value, LOGIN)["success"]
    assert not (files / ".env").exists()


def test_the_page_takes_googles_keys(files, caplog):
    page = _page()
    labels = {key["name"]: key["label"] for key in page.state()["keys"]}
    assert labels["GOOGLE_CLIENT_ID"] == "Google client ID"
    assert labels["GOOGLE_CLIENT_SECRET"] == "Google client secret"

    assert page.set_key("GOOGLE_CLIENT_SECRET", "made-up-client-secret", LOGIN)["success"]

    assert 'GOOGLE_CLIENT_SECRET="made-up-client-secret"' in (files / ".env").read_text()
    assert stat.S_IMODE(os.stat(files / ".env").st_mode) == 0o600
    assert "made-up-client-secret" not in caplog.text and "made-up-client-secret" not in json.dumps(page.state())


def test_settings_are_saved_as_only_whats_different_with_a_copy_of_the_old_file(files):
    (files / "config.yaml").write_text("# My own settings\nnavidrome:\n  url: http://old.local:4533\n")
    result = _page().save_settings({"navidrome.url": "http://music.local:4533", "ntfy.url": "http://ntfy.local",
                                    "ntfy.alerts": "homelab, servers",
                                    "thresholds.silence_duration": "0.7", "logging.log_transcripts": False,
                                    # The same as config.yaml's, so left out of the file.
                                    "music.player": "spotify"}, LOGIN)

    assert result["success"]
    assert yaml.safe_load((files / "config.yaml").read_text()) == {
        "navidrome": {"url": "http://music.local:4533"}, "ntfy": {"url": "http://ntfy.local",
                                                                  "alerts": ["homelab", "servers"]},
        "thresholds": {"silence_duration": 0.7}, "logging": {"log_transcripts": False}}
    assert (files / "config.yaml.bak").read_text().startswith("# My own settings")


def test_the_page_can_save_with_a_removed_setting_still_in_the_file(files):
    # The brain's own file still has weather.location from before the weather was Home Assistant's.
    (files / "config.yaml").write_text("weather:\n  location: Leeds\n")
    page = _page()
    assert "weather.location" not in {setting["key"] for setting in page.state()["settings"]}
    assert page.save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)["success"]
    assert yaml.safe_load((files / "config.yaml").read_text())["navidrome"] == {"url": "http://music.local:4533"}
    assert not page.save_settings({"weather.location": "Leeds"}, LOGIN)["success"]


def test_saving_settings_keeps_the_header_setup_wrote(files):
    # yaml drops comments, so the first save from the page lost it.
    _page().save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)
    assert (files / "config.yaml").read_text().startswith(page_module.SETTINGS_HEADER)


@pytest.mark.parametrize("save, first, second, kept", [
    ("save_settings", {"navidrome.url": "http://music.local:4533"}, {"ntfy.quiet_from": "22:00"},
     lambda files: yaml.safe_load((files / "config.yaml").read_text())),
    ("set_key", ("HA_TOKEN", "first-token"), ("NTFY_TOKEN", "second-token"),
     lambda files: (files / ".env").read_text()),
])
def test_two_quick_saves_dont_lose_either(files, monkeypatch, save, first, second, kept):
    # From two tabs, or two keys pasted quickly: each read the file, changed it and
    # wrote it back, so the second could write over the first.
    page = _page()
    written = page_module.storage.save_text
    others = []

    def save_slowly(path, text):
        if not others:
            # The second save starts while the first is still writing.
            others.append(threading.Thread(target=_save, args=(page, save, second)))
            others[0].start()
            others[0].join(0.3)
        written(path, text)

    monkeypatch.setattr(page_module.storage, "save_text", save_slowly)
    _save(page, save, first)
    others[0].join(5)

    saved = str(kept(files))
    for change in (first, second):
        assert str(list(change.values())[0] if isinstance(change, dict) else change[1]) in saved


def _save(page, save, change):
    if save == "save_settings":
        assert page.save_settings(change, LOGIN)["success"]
    else:
        assert page.set_key(*change, LOGIN)["success"]


@pytest.mark.parametrize("changes, said", [({"music.player": "winamp"}, "navidrome, spotify"),
                                           ({"thresholds.silence_duration": "soon"}, "number"),
                                           ({"llm.model": "claude-opus-5-5"}, "can't change")])
def test_a_setting_nova_wouldnt_start_with_is_never_saved(files, changes, said):
    result = _page().save_settings(changes, LOGIN)
    assert not result["success"] and said in result["message"]
    assert not (files / "config.yaml").exists()


def _running(files, text="", **parts):
    """
    A page over a brain running on the settings in the file, as Nova's does, with ntfy -
    the brain's own apply_settings telling its parts of each change.
    """
    (files / "config.yaml").write_text("ntfy:\n  url: http://ntfy.test\n" + text)
    running = Config()
    parts.setdefault("ntfy", Ntfy("http://ntfy.test"))
    return _page(running=running, config=running, **parts), running


def test_a_straight_away_change_is_used_with_no_restart(files):
    page, _ = _running(files)
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    assert result == {"success": True, "message": "Saved. It's used straight away.", "restart_needed": False}
    assert page.brain.ntfy.quiet_from == "22:00"
    assert not page.state()["restart_needed"]


def test_a_mixed_save_uses_some_now_and_says_the_rest_waits(files):
    page, running = _running(files, "navidrome:\n  url: http://music.local:4533\n")
    result = page.save_settings({"ntfy.quiet_from": "22:00", "music.player": "navidrome"}, LOGIN)

    assert result["message"] == "Saved. What plays the music is used once Nova restarts, and the rest straight away."
    assert running.section("ntfy")["quiet_from"] == "22:00" and page.brain.ntfy.quiet_from == "22:00"
    # A player is a connection, swapped only at a restart.
    assert running.section("music")["player"] == "spotify"
    assert page.state()["restart_needed"]


def _unreachable(*args):
    raise RuntimeError("ntfy has gone")


def test_a_change_that_cant_be_used_now_is_kept_for_the_restart(files):
    page, _ = _running(files)
    page.brain.ntfy.retopic = _unreachable
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    assert yaml.safe_load((files / "config.yaml").read_text())["ntfy"]["quiet_from"] == "22:00"
    # Never "used straight away" for what wasn't.
    assert result["message"] == "Saved. It's used once Nova restarts, as it couldn't be used straight away."
    assert result["restart_needed"] and page.state()["restart_needed"]


def test_a_mixed_save_whose_straight_away_part_fails_says_it_waits(files):
    page, _ = _running(files)
    page.brain.ntfy.retopic = _unreachable
    result = page.save_settings({"ntfy.quiet_from": "22:00", "thresholds.silence_duration": 1.2}, LOGIN)

    assert result["message"] == ("Saved. Night starts at, as 23:00 is used once Nova restarts, and the rest "
                                 "straight away. Some of it couldn't be used straight away.")
    assert result["restart_needed"]


def test_the_brain_failing_to_use_a_save_at_all_keeps_it_for_the_restart(files):
    page, _ = _running(files)

    def broken(changes):
        raise RuntimeError("the brain is in trouble")

    page.brain.apply_settings = broken
    result = page.save_settings({"thresholds.silence_duration": 1.2}, LOGIN)

    assert yaml.safe_load((files / "config.yaml").read_text())["thresholds"]["silence_duration"] == 1.2
    assert result == {"success": True, "restart_needed": True,
                      "message": "Saved. It's used once Nova restarts, as it couldn't be used straight away."}
    assert page.state()["restart_needed"]


def test_a_value_which_failed_and_is_passed_on_again_is_used(files):
    # ntfy is always told all five of its settings, so a later save of another passes
    # the one which failed on again - and once it's used, no restart is owed for it.
    page, _ = _running(files)
    retopic = page.brain.ntfy.retopic
    page.brain.ntfy.retopic = _unreachable
    page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)
    page.brain.ntfy.retopic = retopic
    result = page.save_settings({"ntfy.quiet_until": "06:30"}, LOGIN)

    assert (page.brain.ntfy.quiet_from, page.brain.ntfy.quiet_until) == ("22:00", "06:30")
    assert not result["restart_needed"] and not page.state()["restart_needed"]


def test_a_restart_still_owed_from_an_earlier_save_is_still_said(files):
    page, _ = _running(files, "navidrome:\n  url: http://music.local:4533\n")
    page.save_settings({"music.player": "navidrome"}, LOGIN)
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    assert result["message"] == "Saved. It's used straight away."
    assert result["restart_needed"]


@pytest.mark.parametrize("given", [5, 10 ** 400], ids=["five", "too big for a float"])
def test_an_error_names_the_setting_as_the_page_does(files, given):
    # 10 ** 400 is a whole number too big for a float, which the page's JSON can send.
    result = _page().save_settings({"thresholds.silence_duration": given}, LOGIN)
    assert result == {"success": False,
                      "message": "Seconds of quiet which end what you're saying should be from 0.2 to 3."}
    assert not (files / "config.yaml").exists()


def test_settings_that_need_each_other_are_refused_on_saving(files):
    result = _page().save_settings({"music.player": "navidrome"}, LOGIN)
    assert result == {"success": False, "message": "Navidrome as the player needs Navidrome's address."}
    assert not (files / "config.yaml").exists()


def test_each_setting_says_how_its_used_and_its_limits(files):
    settings = {setting["key"]: setting for setting in _page().state()["settings"]}
    silence, night = settings["thresholds.silence_duration"], settings["ntfy.quiet_from"]

    assert silence["used"] == "Used straight away" and (silence["min"], silence["max"]) == (0.2, 3)
    assert night["used"] == "Used straight away" and night["form"] == "time"
    assert settings["music.player"]["used"] == "Used once Nova restarts"


def test_the_safe_settings_are_on_the_page(files):
    settings = {setting["key"]: setting for setting in _page().state()["settings"]}
    expected = {
        "sounds.listening_enabled": ("The blip when Nova's listening", NOW),
        "sounds.chime_enabled": ("The chime for done", NOW),
        "sounds.reminder_enabled": ("The sound before alerts and warnings", NOW),
        "web_search.enabled": ("Search the web when asked to", NOW),
        "conversation.history_max_age": ("Seconds a conversation's thread is kept", NOW),
        "spotify.pause_while_speaking": ("Pause Spotify while Nova talks", NOW),
        "mqtt.host": ("Home Assistant's MQTT host", LATER),
        "mqtt.port": ("Its port", LATER),
        "mqtt.username": ("Nova's MQTT user", LATER),
    }
    for key, (label, used) in expected.items():
        assert (settings[key]["label"], settings[key]["used"]) == (label, used)


def test_mqtt_waits_for_a_restart(files):
    page, running = _running(files)
    result = page.save_settings({"mqtt.host": "192.168.1.20"}, LOGIN)

    assert result == {"success": True, "message": "Saved. It's used once Nova restarts.", "restart_needed": True}
    assert running.section("mqtt")["host"] == ""


def test_the_transcripts_switch_is_used_straight_away(files):
    page, _ = _running(files)
    try:
        assert page.save_settings({"logging.log_transcripts": False}, LOGIN)["success"]
        assert not logging_setup.transcripts_logged()
        assert page.save_settings({"logging.log_transcripts": True}, LOGIN)["success"]
        assert logging_setup.transcripts_logged()
    finally:
        logging_setup.log_transcripts(True)


def test_two_quick_saves_both_reach_the_running_settings(files, monkeypatch):
    page, running = _running(files)
    written = page_module.storage.save_text
    others = []

    def save_slowly(path, text):
        if not others:
            # The second save starts while the first is still writing.
            others.append(threading.Thread(target=_save, args=(page, "save_settings",
                                                               {"thresholds.silence_duration": 1.2})))
            others[0].start()
            others[0].join(0.3)
        written(path, text)

    monkeypatch.setattr(page_module.storage, "save_text", save_slowly)
    _save(page, "save_settings", {"ntfy.quiet_from": "22:00"})
    others[0].join(5)

    saved = yaml.safe_load((files / "config.yaml").read_text())
    assert (saved["ntfy"]["quiet_from"], saved["thresholds"]["silence_duration"]) == ("22:00", 1.2)
    assert (running.section("ntfy")["quiet_from"], running.section("thresholds")["silence_duration"]) == (
        "22:00", 1.2)


def test_its_only_there_when_switched_on(make_config):
    brain = FakeBrain({})
    assert SettingsPage.from_config(make_config(settings_page={"port": 0}), brain) is None
    page = SettingsPage.from_config(make_config(settings_page={"port": 8765, "allow": [LOGIN]}), brain)
    assert (page.port, page.allow, page.brain) == (8765, [LOGIN], brain)


# Serving it

@pytest.fixture
def served(files, monkeypatch):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page()
    page.start()
    yield page
    page.stop()


def _ask(page, path, body=None, login=LOGIN, **headers):
    """Asks the page for something as a browser on the tailnet would, through Tailscale."""
    request = urllib.request.Request(f"http://127.0.0.1:{page.port}{path}",
                                     data=None if body is None else json.dumps(body).encode())
    if login:
        request.add_header("Tailscale-User-Login", login)
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_only_the_users_own_login_gets_in(served):
    status, headers, body = _ask(served, "/")
    assert status == 200 and b"<title>Nova</title>" in body
    assert _ask(served, "/", login="someone@else.com")[0] == 403
    # Not through Tailscale at all.
    assert _ask(served, "/api/state", login=None)[0] == 403


def test_the_page_comes_with_its_safety_headers(served):
    _, headers, _ = _ask(served, "/")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"


def test_a_change_has_to_come_from_the_page_itself(served):
    change = {"category": "music", "percent": 20}
    # As another website open in the user's browser would send it.
    assert _ask(served, "/api/volume", change)[0] == 403
    assert _ask(served, "/api/volume", change, X_Nova="page", Sec_Fetch_Site="cross-site")[0] == 403
    assert _bedroom(served).volume.levels["music"] == 40

    status, _, body = _ask(served, "/api/volume", change, X_Nova="page", Sec_Fetch_Site="same-origin")
    assert status == 200 and json.loads(body)["success"] and _bedroom(served).volume.levels["music"] == 20


def test_each_change_reaches_the_right_part_of_nova(served):
    def change(path, body):
        return json.loads(_ask(served, path, body, X_Nova="page")[2])

    change("/api/alarms", {"time": "07:30", "repeat": "weekdays"})
    change("/api/remove", {"kind": "timer", "id": 1})
    change("/api/forget", {"fact": "Your sister is called Amy."})

    # The alarm is the default satellite's, as none was named.
    assert served.brain.clock.alarms == [(7, 30, "weekdays", "bedroom")]
    assert served.brain.clock.removed == [("timer", 1)]
    assert served.brain.memory.known == []
    assert change("/api/restart", {})["message"] == "Restarting."


def test_its_typeface_icon_and_what_nova_is_doing_are_served(served):
    status, headers, body = _ask(served, "/Outfit.woff2")
    assert status == 200 and headers["Content-Type"] == "font/woff2" and body[:4] == b"wOF2"
    status, headers, body = _ask(served, "/icon.svg")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml" and body.startswith(b"<svg")
    assert json.loads(_ask(served, "/api/live")[2]) == {"activity": "idle"}


def test_the_cover_of_whats_playing_is_served(served):
    # Nothing's playing without a music player.
    assert _ask(served, "/api/cover")[0] == 404
    _bedroom(served).music_player = FakePlayer()
    status, headers, body = _ask(served, "/api/cover?for=al-1")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body == b"a cover"


def test_music_and_home_assistant_changes_reach_them(served):
    bedroom = _bedroom(served)
    bedroom.music_player, bedroom.actions, bedroom.home = FakePlayer(), FakeActions(), FakeHome()
    assert json.loads(_ask(served, "/api/music")[2])["now"]["artist"] == "6ix9ine"
    assert json.loads(_ask(served, "/api/home")[2])["routines"][0]["name"] == "Relax"

    _ask(served, "/api/music", {"action": "skip"}, X_Nova="page")
    _ask(served, "/api/light", {"id": "light.kitchen", "brightness": 70}, X_Nova="page")
    _ask(served, "/api/run", {"id": "scene.relax"}, X_Nova="page")
    assert bedroom.actions.asked == [("skip", None)]
    assert bedroom.home.changed == [("light.kitchen", None, 70), ("scene.relax", "run", None)]


def test_the_settings_page_shows_and_changes_mute(served):
    def state():
        return json.loads(_ask(served, "/api/state")[2])

    assert state()["muted"] is False
    status, _, body = _ask(served, "/api/mute", {"muted": True}, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]
    assert _bedroom(served).muted and state()["muted"] is True

    # Only a yes or no mutes it.
    assert not json.loads(_ask(served, "/api/mute", {"muted": "no"}, X_Nova="page")[2])["success"]
    assert _bedroom(served).muted


def test_nonsense_is_answered_rather_than_crashing_on(served):
    assert json.loads(_ask(served, "/api/volume", {}, X_Nova="page")[2]) == {
        "success": False, "message": "That didn't make sense."}
    assert _ask(served, "/api/nothing", {}, X_Nova="page")[0] == 400
    assert _ask(served, "/nothing")[0] == 404
    # JSON's Infinity, which int() can't take.
    for path, body in (("/api/volume", {"category": "general", "percent": float("inf")}),
                       ("/api/remove", {"kind": "timer", "id": float("inf")})):
        assert json.loads(_ask(served, path, body, X_Nova="page")[2]) == {
            "success": False, "message": "That didn't make sense."}


@pytest.mark.parametrize("length", ["-1", "lots"])
def test_a_length_which_isnt_one_is_answered(served, length):
    # A negative one read to the end of the connection, which never came; one which
    # wasn't a number dropped the connection.
    connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=2)
    connection.request("POST", "/api/volume", body=b'{"category": "general", "percent": 30}',
                       headers={"Tailscale-User-Login": LOGIN, "X-Nova": "page", "Content-Length": length})
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())) == (400, {"success": False,
                                                                    "message": "That didn't make sense."})
    connection.close()


def test_a_change_which_cant_be_saved_is_answered(served, monkeypatch):
    def full(path, text):
        raise OSError("No space left on device")

    monkeypatch.setattr(page_module.storage, "save_text", full)
    for path, body in (("/api/keys", {"name": "HA_TOKEN", "value": "a-token"}),
                       ("/api/settings", {"changes": {"navidrome.url": "http://music.local:4533"}})):
        assert json.loads(_ask(served, path, body, X_Nova="page")[2]) == {
            "success": False, "message": "Couldn't save that."}


def test_a_lights_colour_and_warmth_reach_home_assistant(files):
    home = FakeHome()
    page = _page(sessions={"bedroom": FakeSession(home=home)})
    assert page.set_light("light.kitchen", LOGIN, hue=240, saturation="100")["success"]
    assert page.set_light("light.kitchen", LOGIN, kelvin=2700)["success"]
    assert home.changed == [("light.kitchen", 240.0, 100.0, None), ("light.kitchen", None, None, 2700.0)]


@pytest.mark.parametrize("sent", [{"brightness": "bright"}, {"hue": "red", "saturation": 50}, {"kelvin": [2700]},
                                  {"hue": True, "saturation": 50}, {"kelvin": float("nan")},
                                  {"brightness": float("inf")}, {"hue": 10, "saturation": {}}])
def test_a_light_is_never_set_to_something_which_isnt_a_number(files, sent):
    home = FakeHome()
    assert _page(sessions={"bedroom": FakeSession(home=home)}).set_light("light.kitchen", LOGIN, **sent) == {
        "success": False, "message": "That didn't make sense."}
    assert home.changed == []


# Every satellite

def _two_satellites(**kitchen):
    """The bedroom and the kitchen, both connected."""
    return {"bedroom": FakeSession(activity="speaking"), "kitchen": FakeSession(**kitchen)}


def test_the_page_lists_each_satellite(files):
    sessions = _two_satellites()
    sessions["kitchen"].muted = True
    # The garage has been added to the brain but isn't connected.
    page = _page(sessions=sessions, known=["bedroom", "kitchen", "garage"])
    state = page.state()

    assert state["satellites"] == [
        {"name": "bedroom", "connected": True, "activity": "speaking", "muted": False},
        {"name": "kitchen", "connected": True, "activity": "idle", "muted": True},
        {"name": "garage", "connected": False, "activity": "idle", "muted": False}]
    # What's shown below is the default's, unless another is asked for.
    assert state["muted"] is False and page.state("kitchen")["muted"] is True


def test_a_volume_change_goes_to_the_satellite_named(files):
    page = _page(sessions=_two_satellites())
    assert page.set_volume("general", 20, LOGIN, "kitchen")["success"]
    assert page.brain.sessions["kitchen"].volume.levels["general"] == 20
    assert page.brain.sessions["kitchen"].booped == [0.2]
    assert page.brain.sessions["bedroom"].volume.levels["general"] == 60 and not page.brain.sessions["bedroom"].booped
    # None named, so the default's.
    page.set_volume("general", 10, LOGIN)
    assert page.brain.sessions["bedroom"].volume.levels["general"] == 10

    page.set_muted(True, LOGIN, "kitchen")
    assert page.brain.sessions["kitchen"].muted and not page.brain.sessions["bedroom"].muted


def test_the_music_and_lights_are_the_satellite_named(files):
    actions, home = FakeActions(), FakeHome()
    page = _page(sessions=_two_satellites(music=FakePlayer(), actions=actions, home=home))
    assert page.music_state("kitchen")["available"] and page.home_state("kitchen")["available"]
    assert not page.music_state()["available"] and not page.home_state()["available"]
    page.play("skip", LOGIN, satellite="kitchen")
    page.set_light("light.kitchen", LOGIN, on=False, satellite="kitchen")
    assert actions.asked == [("skip", None)] and home.changed == [("light.kitchen", False, None)]
    assert not page.play("skip", LOGIN)["success"]


def test_a_satellite_which_isnt_connected_is_said_so(files):
    page = _page(sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"])
    said = {"success": False, "message": "garage isn't connected."}
    assert page.set_volume("general", 20, LOGIN, "garage") == said
    assert page.set_muted(True, LOGIN, "garage") == said
    assert page.play("pause", LOGIN, satellite="garage") == said
    assert page.set_light("light.kitchen", LOGIN, on=True, satellite="garage") == said
    assert page.run("scene.relax", LOGIN, satellite="garage") == said
    # The default is named in the same way when it's the one which isn't connected.
    away = _page(sessions={}, known=["bedroom"])
    assert away.set_volume("general", 20, LOGIN) == {"success": False, "message": "bedroom isn't connected."}
    assert away.music_state() == {"available": False} and away.home_state() == {"available": False}
    assert away.cover() is None and away.state()["volumes"] == {}


def test_alarms_and_routines_work_with_no_satellite(files):
    page = _page(sessions={}, known=["bedroom"], routines=Routines(path=str(files / "routines.json")))

    assert page.add_alarm("07:30", None, LOGIN)["success"]
    # Set for the default satellite, which rings it once it's back.
    assert page.brain.clock.alarms == [(7, 30, None, "bedroom")]
    assert page.add_alarm("08:00", None, LOGIN, "bedroom")["success"] and page.brain.clock.alarms[1][3] == "bedroom"
    assert page.remove("timer", 1, LOGIN)["success"]
    assert page.forget("Your sister is called Amy.", LOGIN)["success"]
    assert page.save_routine({"name": "Good morning", "phrases": "good morning", "steps": "what's the weather"},
                             LOGIN)["success"]
    assert page.set_key("HA_TOKEN", "a-token", LOGIN)["success"]

    state = page.state()
    assert state["clock"]["timers"] and state["memories"] == [] and state["routines"][0]["name"] == "Good morning"
    assert state["volumes"] == {} and state["muted"] is False
    assert state["satellites"] == [{"name": "bedroom", "connected": False, "activity": "idle", "muted": False}]
    # Without any satellite added at all, too.
    assert _page(sessions={}, known=[]).state()["satellites"] == []


def test_the_clock_is_shown_for_the_satellite_asked_about(files):
    page = _page(sessions=_two_satellites())
    page.state()
    assert page.brain.clock.overviewed == "bedroom"
    page.state("kitchen")
    assert page.brain.clock.overviewed == "kitchen"


def test_the_page_asks_after_the_satellite_it_names(served):
    served.brain.sessions["kitchen"] = FakeSession(activity="thinking")
    served.brain.known.append("kitchen")
    assert json.loads(_ask(served, "/api/live?satellite=kitchen")[2]) == {"activity": "thinking"}
    assert json.loads(_ask(served, "/api/live")[2]) == {"activity": "idle"}
    assert json.loads(_ask(served, "/api/state?satellite=kitchen")[2])["satellite"] == "kitchen"

    _ask(served, "/api/volume", {"category": "general", "percent": 5, "satellite": "kitchen"}, X_Nova="page")
    assert served.brain.sessions["kitchen"].volume.levels["general"] == 5
    said = json.loads(_ask(served, "/api/mute", {"muted": True, "satellite": "porch"}, X_Nova="page")[2])
    assert said == {"success": False, "message": "porch isn't connected."}


def test_an_alarm_is_never_set_for_a_satellite_the_brain_doesnt_know(files):
    page = _page(sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"])
    for ghost in ("ghosst", 5):
        assert page.add_alarm("07:30", None, LOGIN, ghost) == {
            "success": False, "message": f"There's no satellite called {ghost}."}
    assert page.brain.clock.alarms == []
    # One which is merely away is the brain's to set alarms for.
    assert page.add_alarm("07:30", None, LOGIN, "garage")["success"]
    assert page.brain.clock.alarms == [(7, 30, None, "garage")]


def test_a_satellite_which_doesnt_exist_is_handled_everywhere(served):
    ghost = "ghost"
    said = {"success": False, "message": "ghost isn't connected."}

    def post(path, body):
        return json.loads(_ask(served, path, {**body, "satellite": ghost}, X_Nova="page")[2])

    assert post("/api/volume", {"category": "general", "percent": 5}) == said
    assert post("/api/mute", {"muted": True}) == said
    assert post("/api/music", {"action": "pause"}) == said
    assert post("/api/light", {"id": "light.kitchen", "on": True}) == said
    assert post("/api/run", {"id": "scene.relax"}) == said
    assert post("/api/alarms", {"time": "07:30"}) == {"success": False, "message": "There's no satellite called ghost."}
    assert _ask(served, "/api/state?satellite=ghost")[0] == 200
    assert json.loads(_ask(served, "/api/state?satellite=ghost")[2])["volumes"] == {}
    assert _ask(served, "/api/cover?satellite=ghost")[0] == 404
    assert json.loads(_ask(served, "/api/music?satellite=ghost")[2]) == {"available": False}
    assert json.loads(_ask(served, "/api/home?satellite=ghost")[2]) == {"available": False}
    assert json.loads(_ask(served, "/api/live?satellite=ghost")[2]) == {"activity": "idle"}
    assert served.brain.clock.alarms == []


def test_the_page_works_with_no_satellite_added_at_all(files):
    page = _page(sessions={}, known=[])
    state = page.state()
    assert state["satellites"] == [] and state["satellite"] is None and state["volumes"] == {}
    assert page.add_alarm("07:30", None, LOGIN)["success"]
    assert page.brain.clock.alarms == [(7, 30, None, None)]
    assert page.set_volume("general", 5, LOGIN) == {"success": False, "message": "There's no satellite to do that on."}


def test_restarting_asks_the_brain_to_stop_for_a_restart(files):
    asked = []
    page = _page()
    page.brain.restart = lambda: asked.append("restart") or "Restarting."
    page._restart = None
    assert page.restart(LOGIN) == {"success": True, "message": "Restarting."} and asked == ["restart"]


# The batteries

BATTERIES_NOW = 1_800_000_000.0


class FakeBatteryHome:
    """Answers batteries() as Home Assistant does, from what a test sets."""

    def __init__(self):
        self.fails = None
        self.people_now = []
        self.found = [
            # Not in name order, so the page's sorting is what puts the MacBook first.
            {"id": "sensor.s25_battery_level", "name": "s25", "level": 80, "changed": BATTERIES_NOW - 7200,
             "device": "d2", "maker": "samsung", "model": "SM-S938B", "charging": "Charging", "app": True},
            {"id": "sensor.macbook_battery_level", "name": "macbook", "level": 40, "changed": BATTERIES_NOW,
             "device": "d1", "maker": "Apple", "model": "Mac16,8", "charging": "Not Charging", "app": True},
        ]

    def batteries(self):
        if self.fails:
            raise self.fails
        return list(self.found)

    def people(self):
        return self.people_now


def _watched(tmp_path):
    """A page over a real battery watcher, which a test can find fault in Home Assistant with."""
    home = FakeBatteryHome()
    choices = BatteryChoices(str(tmp_path / "batteries.json"), mobile_marks=[5], other_marks=[15, 5])
    watcher = BatteryWatcher(home, choices, lambda message: True, lambda message, title: False, lambda: False,
                             check_every=60, stale_after=3600)
    return _page(batteries=watcher), home, choices


def _device(page, battery_id):
    return next(d for d in page.batteries_state()["devices"] if d["id"] == battery_id)


MAC = "sensor.macbook_battery_level"
PHONE = "sensor.s25_battery_level"


def test_the_batteries_are_listed_with_their_choices(files, tmp_path, monkeypatch):
    page, home, choices = _watched(tmp_path)
    monkeypatch.setattr(page_module.time, "time", lambda: BATTERIES_NOW)
    state = page.batteries_state()
    assert state["available"] is True and state["also_send_when_said"] is True
    # In name order, each with its choices and the age of a reading which has gone stale.
    assert [d["id"] for d in state["devices"]] == [MAC, PHONE]
    mac, phone = state["devices"]
    assert mac == {"id": MAC, "name": "MacBook", "level": 40, "charging": False, "stale": None, "kind": "other",
                   "enabled": True, "marks": [15, 5], "default_marks": [15, 5], "send_to_phone": True,
                   "changed": False}
    assert phone["kind"] == "mobile" and phone["charging"] is True and phone["stale"] == "2 hours"
    assert phone["marks"] == [5] and phone["send_to_phone"] is False
    choices.set(page.brain.batteries.read()[1], marks=[30, 10])
    assert _device(page, MAC)["marks"] == [30, 10] and _device(page, MAC)["default_marks"] == [15, 5]
    assert _device(page, MAC)["changed"] is True


def test_each_device_shows_who_decides_home_only_with_more_than_one_person(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    # One person (or none) decides everything, so there is nothing to show.
    assert all("person" not in d for d in page.batteries_state()["devices"])
    home.people_now = [{"id": "person.a", "name": "Amy", "state": "home", "devices": ["d2"]}]
    assert all("person" not in d for d in page.batteries_state()["devices"])
    # With two, the one whose trackers include the device, else the first - as the watcher chooses.
    home.people_now = [{"id": "person.a", "name": "Amy", "state": "home", "devices": ["d2"]},
                       {"id": "person.b", "name": "Ben", "state": "not_home", "devices": ["d1"]}]
    assert _device(page, MAC)["person"] == "Ben" and _device(page, PHONE)["person"] == "Amy"
    home.found[0]["device"] = None
    assert _device(page, PHONE)["person"] == "Amy"


def test_a_batterys_warnings_can_be_switched_off_and_it_works_straight_away(files, tmp_path, caplog):
    page, home, choices = _watched(tmp_path)
    batteries = page.brain.batteries.read()
    home.found[1]["level"] = 10
    with caplog.at_level(logging.INFO):
        result = page.set_battery(MAC, LOGIN, enabled=False)
    assert result["success"] and "restart" not in result["message"].lower()
    assert LOGIN in caplog.text and MAC in caplog.text
    assert not _device(page, MAC)["enabled"]
    assert choices.due(page.brain.batteries.read()) == []
    page.set_battery(MAC, LOGIN, enabled=True)
    assert [w.mark for w in choices.due(page.brain.batteries.read())] == [15]
    # Marks and the phone switch work the same way, and the defaults come back.
    assert page.set_battery(PHONE, LOGIN, marks=[50], send_to_phone=True)["success"]
    assert _device(page, PHONE)["marks"] == [50] and _device(page, PHONE)["send_to_phone"] is True
    assert page.set_battery(PHONE, LOGIN, reset=True)["success"]
    assert _device(page, PHONE)["marks"] == [5] and _device(page, PHONE)["send_to_phone"] is False
    assert len(batteries) == 2


@pytest.mark.parametrize("marks, said", [
    ([150, 5], "whole number"), ([5, 5], "must be different"), ([5, 10, 15], "needs two marks"),
    ([1.5, 10], "whole number"), ([0, 10], "whole number")])
def test_marks_which_dont_make_sense_are_refused(files, tmp_path, marks, said):
    page, home, choices = _watched(tmp_path)
    result = page.set_battery(MAC, LOGIN, marks=marks)
    assert not result["success"] and said in result["message"]
    assert _device(page, MAC)["marks"] == [15, 5]
    # One mark is too few for the Mac, and two too many for the phone.
    assert "needs two marks" in page.set_battery(MAC, LOGIN, marks=[5])["message"]
    assert "needs one mark" in page.set_battery(PHONE, LOGIN, marks=[5, 10])["message"]


def test_a_battery_change_which_cant_be_understood_or_saved_is_refused(files, tmp_path, monkeypatch):
    page, home, choices = _watched(tmp_path)
    # In Python bool("false") is True, so nothing is coerced.
    for bad in ({"enabled": "false"}, {"send_to_phone": 0}, {"marks": "15"}):
        assert page.set_battery(MAC, LOGIN, **bad) == {"success": False, "message": "That didn't make sense."}
    # A True in the marks is refused by the choices, with their own sentence.
    assert not page.set_battery(MAC, LOGIN, marks=[True, 5])["success"]
    assert page.set_battery_also_send("no", LOGIN)["success"] is False
    assert choices.also_send_when_said is True

    def full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(choices._store, "change", full)
    assert page.set_battery(MAC, LOGIN, enabled=False) == {"success": False, "message": "Couldn't save that."}
    assert page.set_battery(MAC, LOGIN, reset=True) == {"success": False, "message": "Couldn't save that."}
    assert page.set_battery_also_send(False, LOGIN) == {"success": False, "message": "Couldn't save that."}


def test_a_battery_home_assistant_doesnt_show_just_now_is_refused(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    result = page.set_battery("sensor.gone_battery_level", LOGIN, enabled=False)
    assert not result["success"] and "sensor.gone_battery_level" not in choices._state["devices"]
    import requests
    home.fails = requests.ConnectionError("down")
    assert not page.set_battery(MAC, LOGIN, enabled=False)["success"]
    assert MAC not in choices._state["devices"]
    assert page.brain.batteries is not None and _page().batteries_state()["available"] is False


def test_also_send_when_said_can_be_switched_off(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    assert page.set_battery_also_send(False, LOGIN)["success"]
    assert choices.also_send_when_said is False and page.batteries_state()["also_send_when_said"] is False
    assert page.set_battery_also_send(True, LOGIN)["success"] and choices.also_send_when_said


def test_the_section_says_when_home_assistant_cant_be_reached(files, tmp_path):
    import requests
    page, home, choices = _watched(tmp_path)
    home.fails = requests.ConnectionError("down")
    state = page.batteries_state()
    assert state["available"] is False and "Home Assistant" in state["message"]
    # Nor does it with nothing to reach it with.
    assert _page().batteries_state()["available"] is False
    home.fails = ValueError("not JSON")
    assert page.batteries_state()["available"] is False


def test_a_battery_change_needs_the_pages_own_header(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    page.start()
    try:
        body = {"id": MAC, "enabled": False}
        assert _ask(page, "/api/batteries", body)[0] == 403
        assert _ask(page, "/api/batteries/also", {"on": False})[0] == 403
        assert _ask(page, "/api/batteries", body, X_Nova="page", Sec_Fetch_Site="cross-site")[0] == 403
        assert choices.enabled(MAC) and choices.also_send_when_said
        assert json.loads(_ask(page, "/api/batteries")[2])["devices"][0]["id"] == MAC
        assert json.loads(_ask(page, "/api/batteries", body, X_Nova="page")[2])["success"]
        assert json.loads(_ask(page, "/api/batteries/also", {"on": False}, X_Nova="page")[2])["success"]
        assert not choices.enabled(MAC) and not choices.also_send_when_said
        # No server error for nonsense.
        assert json.loads(_ask(page, "/api/batteries", {"enabled": False}, X_Nova="page")[2])["success"] is False
    finally:
        page.stop()
