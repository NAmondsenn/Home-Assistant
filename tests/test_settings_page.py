"""
Settings Page Test
Checks the settings page shows what it should - and never a key - saves only what
Nova would accept when it starts, and only lets the user in, and only changes made
from the page itself: over a real server on this machine, with stand-ins for the
rest of Nova, and its files in a folder of the test's own.
"""

import json
import logging
import os
import stat
import time
import urllib.error
import urllib.request

import pytest
import yaml

from voice_assistant import paths
from voice_assistant import settings_page as page_module
from voice_assistant.config import Config
from voice_assistant.routines import Routines
from voice_assistant.settings_page import SettingsPage, restart_service

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

    def overview(self):
        return {"timers": [{"id": 1, "name": "pizza reminder", "due_at": 1_800_000_000}], "alarms": []}

    def remove(self, kind, item_id):
        self.removed.append((kind, item_id))
        return {"success": True, "message": "Removed."}

    def set_alarm(self, hour, minute, repeat=None):
        self.alarms.append((hour, minute, repeat))
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

    def overview(self):
        return {"success": True, "lights": [{"id": "light.kitchen", "name": "Kitchen", "on": True}],
                "routines": [{"id": "scene.relax", "name": "Relax", "kind": "scene"}]}

    def set_light(self, entity_id, on=None, brightness=None):
        self.changed.append((entity_id, on, brightness))
        return {"success": True, "message": "Done."}

    def run(self, entity_id):
        self.changed.append((entity_id, "run", None))
        return {"success": True, "message": "Set Relax."}


class FakeWake:
    """Stands in for the wake word detector, which keeps the microphone's mute."""

    def __init__(self):
        self.muted = False

    def set_muted(self, muted):
        self.muted = muted


def _page(running=None, **parts):
    # The login as Tailscale might capitalise it, which mustn't matter.
    made = dict(running=running, volume=FakeVolume(), clock=FakeClock(), memory=FakeMemory(),
                restart=lambda: "Restarting.", started=1_800_000_000)
    made.update(parts)
    return SettingsPage([LOGIN.upper()], port=0, **made)


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
    assert {"weather.location", "ntfy.alerts", "backend.address"} <= {setting["key"] for setting in state["settings"]}
    # What can only be one of a few words comes with them, to pick from.
    player = next(setting for setting in state["settings"] if setting["key"] == "music.player")
    assert player["choices"] == ["navidrome", "spotify"]


def test_the_latest_problems_in_the_log_are_shown_newest_first(files):
    (files / "logs").mkdir()
    (files / "logs" / "smart_assistant.log").write_text(
        "2026-10-20 16:50:01,100 - main - INFO - Transcription complete: 'what's on today'\n"
        "2026-10-20 16:51:58,131 - voice_assistant.dropouts - WARNING - The speaker ran dry\n"
        "2026-10-20 17:02:00,001 - voice_assistant.ntfy - ERROR - Couldn't say the alert\n")
    problems = _page().state()["status"]["problems"]

    assert [(problem["time"], problem["level"]) for problem in problems] == [("2026-10-20 17:02", "error"),
                                                                              ("2026-10-20 16:51", "warning")]
    # What's said is only ever logged as information, so it's never shown here.
    assert "what's on today" not in json.dumps(problems)


def test_a_restart_is_asked_for_once_whats_saved_differs_from_whats_running(files):
    running = Config()
    page = _page(running=running)
    assert not page.state()["restart_needed"]
    page.save_settings({"weather.location": "Bristol"}, LOGIN)
    assert page.state()["restart_needed"]


def test_looking_at_the_settings_isnt_logged(files, caplog):
    # The page asks every 20 seconds while it's open, which was a "Settings loaded"
    # line each time - thousands a day, burying what the log is for.
    (files / "config.yaml").write_text("weather:\n  location: Bristol\n")
    page = _page(running=Config())
    with caplog.at_level(logging.INFO):
        caplog.clear()
        page.state()
    assert "Settings loaded" not in caplog.text


# What it changes

def test_a_volume_change_is_heard_at_its_new_level(files):
    # So the user hears how loud it now is, on Nova's own speaker, as they drag.
    booped = []
    page = _page(boop=booped.append)
    page.set_volume("alarm", 30, LOGIN)
    assert page.volume.levels["alarm"] == 30 and booped == [0.3]


def test_the_music_volume_is_the_players_own_so_a_playing_song_changes_too(files):
    player, booped = FakePlayer(), []
    page = _page(music=player, boop=booped.append)
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and booped == [0.25]
    assert page.state()["volumes"]["music"] == 25


def test_spotifys_volume_is_shown_as_last_set_without_asking_spotify(files):
    # Asking Spotify's servers every time the page - or Home Assistant - looks would be
    # slow, so Nova keeps its own note of the level, which follows each change.
    class Spotify(FakePlayer):
        needs_internet = True

        def get_volume(self):
            raise AssertionError("Spotify was asked for its volume")

    player = Spotify()
    page = _page(music=player)
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and page.state()["volumes"]["music"] == 25


def test_the_page_can_tell_what_nova_is_doing(files):
    assert _page(activity=lambda: "listening").live() == {"activity": "listening"}
    assert _page().live() == {"activity": "idle"}


def test_the_page_is_told_whats_playing_and_the_playlists_are_kept_a_while(files):
    player = FakePlayer()
    page = _page(music=player)
    state = page.music_state()
    assert state["player"] == "Navidrome" and state["now"]["track"] == "Gooba" and state["playlists"] == ["Chill", "Workout"]
    page.music_state()
    assert player.asked_for_playlists == 1
    assert _page().music_state() == {"available": False}


def test_the_music_buttons_go_through_the_same_actions_as_asking_out_loud(files):
    actions = FakeActions()
    page = _page(music=FakePlayer(), actions=actions)
    page.play("play", LOGIN, query=" Gooba by 6ix9ine ")
    page.play("play", LOGIN)
    page.play("playlist", LOGIN, playlist="Workout")
    page.play("pause", LOGIN)
    assert actions.asked == [("play", "Gooba by 6ix9ine"), ("play", None), ("playlist", "Workout"), ("pause", None)]
    assert page.play("eject", LOGIN) == {"success": False, "message": "That isn't something the music does."}
    assert not _page().play("pause", LOGIN)["success"]


def test_the_lights_and_scenes_come_from_home_assistant_and_go_back_to_it(files):
    home = FakeHome()
    page = _page(home=home)
    assert page.home_state()["lights"][0]["id"] == "light.kitchen"
    page.set_light("light.kitchen", LOGIN, on=False)
    page.set_light("light.kitchen", LOGIN, brightness="40")
    page.run("scene.relax", LOGIN)
    assert home.changed == [("light.kitchen", False, None), ("light.kitchen", None, 40), ("scene.relax", "run", None)]
    # Only a yes or no switches a light.
    assert not page.set_light("light.kitchen", LOGIN, on="off")["success"]
    assert _page().home_state() == {"available": False}
    assert not _page().run("scene.relax", LOGIN)["success"]


def test_a_routine_is_made_run_and_deleted_from_the_page(files):
    ran = []
    page = _page(routines=Routines(path=str(files / "routines.json")), run_routine=ran.append)
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


def test_settings_are_saved_as_only_whats_different_with_a_copy_of_the_old_file(files):
    (files / "config.yaml").write_text("# My own settings\nweather:\n  location: Leeds\n")
    result = _page().save_settings({"weather.location": "Bristol", "ntfy.alerts": "homelab, servers",
                                    "thresholds.silence_duration": "0.7", "logging.log_transcripts": False,
                                    # The same as config.yaml's, so left out of the file.
                                    "music.player": "spotify"}, LOGIN)

    assert result["success"]
    assert yaml.safe_load((files / "config.yaml").read_text()) == {
        "weather": {"location": "Bristol"}, "ntfy": {"alerts": ["homelab", "servers"]},
        "thresholds": {"silence_duration": 0.7}, "logging": {"log_transcripts": False}}
    assert (files / "config.yaml.bak").read_text().startswith("# My own settings")


@pytest.mark.parametrize("changes, said", [({"music.player": "winamp"}, "navidrome, spotify"),
                                           ({"thresholds.silence_duration": "soon"}, "number"),
                                           ({"llm.model": "claude-opus-5-5"}, "can't change")])
def test_a_setting_nova_wouldnt_start_with_is_never_saved(files, changes, said):
    result = _page().save_settings(changes, LOGIN)
    assert not result["success"] and said in result["message"]
    assert not (files / "config.yaml").exists()


def test_a_restart_outside_systemd_is_left_to_the_user(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert restart_service() == "Nova isn't running as a service here, so restart it yourself."


def test_its_only_there_when_switched_on(make_config):
    assert SettingsPage.from_config(make_config(settings_page={"port": 0})) is None
    page = SettingsPage.from_config(make_config(settings_page={"port": 8765, "allow": [LOGIN]}))
    assert (page.port, page.allow) == (8765, [LOGIN])


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
    assert served.volume.levels["music"] == 40

    status, _, body = _ask(served, "/api/volume", change, X_Nova="page", Sec_Fetch_Site="same-origin")
    assert status == 200 and json.loads(body)["success"] and served.volume.levels["music"] == 20


def test_each_change_reaches_the_right_part_of_nova(served):
    def change(path, body):
        return json.loads(_ask(served, path, body, X_Nova="page")[2])

    change("/api/alarms", {"time": "07:30", "repeat": "weekdays"})
    change("/api/remove", {"kind": "timer", "id": 1})
    change("/api/forget", {"fact": "Your sister is called Amy."})

    assert served.clock.alarms == [(7, 30, "weekdays")] and served.clock.removed == [("timer", 1)]
    assert served.memory.known == []
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
    served.music = FakePlayer()
    status, headers, body = _ask(served, "/api/cover?for=al-1")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body == b"a cover"


def test_music_and_home_assistant_changes_reach_them(served):
    served.music, served.actions, served.home = FakePlayer(), FakeActions(), FakeHome()
    assert json.loads(_ask(served, "/api/music")[2])["now"]["artist"] == "6ix9ine"
    assert json.loads(_ask(served, "/api/home")[2])["routines"][0]["name"] == "Relax"

    _ask(served, "/api/music", {"action": "skip"}, X_Nova="page")
    _ask(served, "/api/light", {"id": "light.kitchen", "brightness": 70}, X_Nova="page")
    _ask(served, "/api/run", {"id": "scene.relax"}, X_Nova="page")
    assert served.actions.asked == [("skip", None)]
    assert served.home.changed == [("light.kitchen", None, 70), ("scene.relax", "run", None)]


def test_the_settings_page_shows_and_changes_mute(served):
    def state():
        return json.loads(_ask(served, "/api/state")[2])

    served.wake = FakeWake()
    assert state()["muted"] is False
    status, _, body = _ask(served, "/api/mute", {"muted": True}, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]
    assert served.wake.muted and state()["muted"] is True

    # Only a yes or no mutes it.
    assert not json.loads(_ask(served, "/api/mute", {"muted": "no"}, X_Nova="page")[2])["success"]
    assert served.wake.muted


def test_nonsense_is_answered_rather_than_crashing_on(served):
    assert json.loads(_ask(served, "/api/volume", {}, X_Nova="page")[2]) == {
        "success": False, "message": "That didn't make sense."}
    assert _ask(served, "/api/nothing", {}, X_Nova="page")[0] == 400
    assert _ask(served, "/nothing")[0] == 404
