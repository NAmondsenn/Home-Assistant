"""
Action Executor Test
Tests how actions are dispatched, using the real VolumeControl (saving into a
temporary folder) and a fake Spotify controller, so nothing plays or goes online.
"""

import pytest

from voice_assistant import volume as volume_module
from voice_assistant.actions import Action, ActionExecutor, ActionResult, confirm
from voice_assistant.volume import VolumeControl


class FakeSpotify:
    """Stands in for SpotifyController, remembering the music volume it's given."""

    name = "Spotify"

    def __init__(self, playing, level=50):
        self.playing = playing
        self.level = level
        self.calls = []
        self.resume_cancelled = False

    def pause(self):
        self.calls.append("pause")
        return {"success": True, "message": "Paused", "chime": True}

    def cancel_resume(self):
        self.resume_cancelled = True

    def was_playing(self):
        return self.playing

    def get_volume(self):
        return self.level

    def set_volume(self, percent):
        self.level = int(percent)
        return {"success": True, "message": f"Volume {self.level} percent."}


@pytest.fixture
def make_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(volume_module, "VOLUMES_FILE", str(tmp_path / "volumes.json"))

    def factory(spotify=None, **others):
        return ActionExecutor(music=spotify, volume=VolumeControl(), **others)

    return factory


def test_bare_volume_change_with_music_on_changes_the_music(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    result = executor.execute(Action("set_volume", {"direction": "down"}))
    assert result.ok and spotify.level == 40
    # The assistant's own voice is left where it was.
    assert executor.volume.percent("general") == 70


def test_bare_volume_change_with_nothing_playing_changes_the_assistant(make_executor):
    spotify = FakeSpotify(playing=False, level=50)
    executor = make_executor(spotify)
    executor.execute(Action("set_volume", {"direction": "up"}))
    assert executor.volume.percent("general") == 80 and spotify.level == 50


def test_saying_which_volume_still_wins(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    executor.execute(Action("set_volume", {"category": "general", "level": 30}))
    assert executor.volume.percent("general") == 30 and spotify.level == 50


def test_volume_without_spotify_falls_back_to_the_assistant(make_executor):
    executor = make_executor()
    executor.execute(Action("set_volume", {"level": 55}))
    assert executor.volume.percent("general") == 55


def test_volume_changes_are_confirmed_with_a_beep(make_executor):
    executor = make_executor(FakeSpotify(playing=False))
    result = executor.execute(Action("set_volume", {"category": "alarm", "direction": "down"}))
    assert (result.confirm, result.sound, result.sound_category) == ("sound", "volume_beep.wav", "alarm")


def test_unknown_tool_is_handled(make_executor):
    assert make_executor().execute(Action("play_netflix")).ok is False


def test_a_parameter_the_model_made_up_doesnt_stop_it_running(make_executor):
    result = make_executor().execute(Action("set_volume", {"level": 30, "loudness": "very"}))
    assert result.ok


# Each tool declared once

def test_every_tool_has_a_description_and_only_takes_what_it_declares():
    # Adding a tool used to mean three edits in two files, which could drift apart.
    import inspect
    for name, method in ActionExecutor.tools().items():
        declared = set(method.tool["parameters"])
        taken = set(list(inspect.signature(method).parameters)[1:])
        assert method.tool["description"], name
        assert declared == taken, name
        assert set(method.tool["required"]) <= declared, name


def test_only_tools_which_can_work_are_offered(make_executor):
    # The lights tool used to be offered while it could only fail.
    offered = {schema["name"] for schema in ActionExecutor(clock=object()).tool_schemas()}
    assert "set_timer" in offered and "play_music" not in offered and "get_weather" not in offered


def test_a_tool_asked_for_without_what_it_needs_says_so(make_executor):
    result = ActionExecutor().execute(Action("play_music", {"query": "Drake"}))
    assert result.ok is False and result.say == "The music isn't available right now."


def test_an_explicit_pause_keeps_the_music_paused_afterwards(make_executor):
    spotify = FakeSpotify(playing=True)
    result = make_executor(spotify).execute(Action("control_playback", {"action": "pause"}))
    assert result.confirm == "chime" and spotify.resume_cancelled


@pytest.mark.parametrize("params, repeat", [
    ({"hour": 7, "minute": 0}, None),
    ({"hour": 7, "minute": 0, "repeat": "weekdays"}, "weekdays"),
    ({"hour": 7, "minute": 0, "repeat": "custom", "days": ["monday", "wednesday"]}, ["monday", "wednesday"]),
    # "custom" with no days is passed through as-is, so the clock asks which days.
    ({"hour": 7, "minute": 0, "repeat": "custom"}, "custom"),
])
def test_an_alarms_repeat_is_passed_to_the_clock(params, repeat):
    class FakeClock:
        def set_alarm(self, hour, minute, repeat):
            self.set = (hour, minute, repeat)
            return {"success": True, "message": "Alarm set."}

    clock = FakeClock()
    ActionExecutor(clock=clock).execute(Action("set_alarm", params))
    assert clock.set == (7, 0, repeat)


# Confirming what was done

def test_a_controllers_flags_are_read_into_one_way_of_confirming():
    assert ActionResult.from_reply({"success": True, "silent": True, "message": "Next"}).confirm == "music"
    assert ActionResult.from_reply({"success": True, "chime": True, "message": "Paused"}).confirm == "chime"
    assert ActionResult.from_reply({"success": True, "message": "Timer set."}).confirm == "speak"
    # A failure is always said, whatever it was going to confirm with.
    assert ActionResult.from_reply({"success": False, "chime": True, "message": "No"}).confirm == "speak"


def test_an_action_which_confirms_itself_has_nothing_said_about_it():
    # Even when there's something else to say: a reminder offer after a skipped
    # song used to have the skip's own message read out in front of it.
    skipped = ActionResult(True, "Skipped", confirm="music")
    assert confirm([skipped], fallback="Okay.") == ([], [])


@pytest.mark.parametrize("result, sounds", [
    (ActionResult(True, confirm="sound", sound="volume_beep.wav", sound_category="alarm"),
     [("volume_beep.wav", "alarm")]),
    (ActionResult(True, "Paused", confirm="chime"), [("success_chime.wav", "general")]),
])
def test_an_action_with_a_sound_is_confirmed_by_it(result, sounds):
    assert confirm([result], chime_enabled=True) == (sounds, [])


def test_a_chimed_action_is_said_out_loud_when_the_chime_is_off():
    assert confirm([ActionResult(True, "Paused", confirm="chime")], chime_enabled=False) == ([], ["Paused"])


def test_a_failed_action_says_why_or_falls_back_to_the_models_reply():
    assert confirm([ActionResult(False, "The music isn't available right now.")],
                   fallback="Okay.") == ([], ["The music isn't available right now."])
    assert confirm([ActionResult(False)], fallback="Okay.") == ([], ["Okay."])


def test_several_actions_are_each_confirmed_their_own_way():
    paused = ActionResult(True, "Paused", confirm="chime")
    timer = ActionResult(True, "Timer set for 10 minutes.")
    assert confirm([paused, timer]) == ([("success_chime.wav", "general")], ["Timer set for 10 minutes."])


def test_weather_is_passed_to_the_forecast():
    class FakeWeather:
        def forecast(self, day):
            return {"success": True, "message": f"Forecast for {day}"}

    executor = ActionExecutor(weather=FakeWeather())
    assert executor.execute(Action("get_weather", {"day": "tomorrow"})).say == "Forecast for tomorrow"


def test_offline_spotify_says_so_straight_away(make_executor, monkeypatch):
    from voice_assistant import actions as actions_module
    monkeypatch.setattr(actions_module.network, "online", lambda: False)
    spotify = FakeSpotify(playing=True)
    executor = make_executor(spotify=spotify)
    offline = "Spotify needs the internet, and I can't reach it right now."
    assert executor.execute(Action("control_playback", {"action": "pause"})).say == offline
    assert executor.execute(Action("set_volume", {"category": "music", "direction": "down"})).say == offline
    assert spotify.calls == []


def test_the_calendar_needs_a_day_it_can_read():
    class FakeHome:
        def agenda(self, first, days):
            self.asked = (first.isoformat(), days)
            return {"success": True, "message": "You've got nothing on."}

    home = FakeHome()
    executor = ActionExecutor(home=home)
    assert executor.execute(Action("get_calendar", {"start": "Thursday"})).ok is False
    executor.execute(Action("get_calendar", {"start": "2026-10-22", "days": 90}))
    assert home.asked == ("2026-10-22", 14)


def test_one_thing_is_looked_for_rather_than_reading_whole_days():
    class FakeHome:
        def find(self, wanted, first):
            self.asked = (wanted, first.isoformat())
            return {"success": True, "message": "You've got Car MOT on Monday."}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(
        Action("get_calendar", {"start": "2026-10-20", "days": 14, "search": "MOT"}))
    assert home.asked == ("MOT", "2026-10-20") and result.say == "You've got Car MOT on Monday."


def test_without_home_assistant_the_lights_say_so():
    result = ActionExecutor().execute(Action("control_home", {"command": "lights on"}))
    assert result.say == "Home Assistant isn't set up yet."
    assert "control_home" in {schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()}


def test_an_activity_is_asked_of_home_assistant_for_the_days_given():
    class FakeHome:
        def activity(self, name, first, days):
            self.asked = (name, first.isoformat(), days)
            return {"success": True, "message": "You haven't got uni tomorrow."}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("get_activity", {"activity": "uni", "start": "2026-10-21"}))
    assert home.asked == ("uni", "2026-10-21", 1) and result.say == "You haven't got uni tomorrow."


def test_the_next_times_are_looked_for_across_a_fortnight():
    class FakeHome:
        def activity(self, name, first, days, count=None):
            self.asked = (name, first.isoformat(), days, count)
            return {"success": True, "message": "You're next at work tomorrow."}

    home = FakeHome()
    ActionExecutor(home=home).execute(Action("get_activity", {"activity": "work", "start": "2026-10-20", "count": 50}))
    assert home.asked == ("work", "2026-10-20", 14, 10)
