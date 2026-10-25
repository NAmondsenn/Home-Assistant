"""
Action Executor Test
Tests how actions are dispatched, using the real VolumeControl (saving into a
temporary folder) and a fake Spotify controller, so nothing plays or goes online.
"""

import time
from types import SimpleNamespace

import pytest
import requests

from voice_assistant import volume as volume_module
from voice_assistant.actions import Action, ActionExecutor, ActionResult, confirm
from voice_assistant.batteries import Battery
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
        def set_alarm(self, hour, minute, repeat, satellite=None):
            self.set = (hour, minute, repeat)
            return {"success": True, "message": "Alarm set."}

    clock = FakeClock()
    ActionExecutor(clock=clock).execute(Action("set_alarm", params))
    assert clock.set == (7, 0, repeat)


def test_timers_and_alarms_ring_on_the_satellite_they_were_set_from():
    class FakeClock:
        def __init__(self):
            self.set = []

        def set_timer(self, duration_seconds, label, is_reminder, at=None, satellite=None):
            self.set.append(("timer", satellite))
            return {"success": True, "message": "Timer set."}

        def set_alarm(self, hour, minute, repeat, satellite=None):
            self.set.append(("alarm", satellite))
            return {"success": True, "message": "Alarm set."}

    clock = FakeClock()
    actions = ActionExecutor(clock=clock, satellite="bedroom")
    actions.execute(Action("set_timer", {"duration_seconds": 600}))
    actions.execute(Action("set_alarm", {"hour": 7, "minute": 0}))
    assert clock.set == [("timer", "bedroom"), ("alarm", "bedroom")]


def test_only_what_rings_on_the_satellite_asked_is_dismissed():
    # "Okay, thanks" in the kitchen dismissed the bedroom's timer too, which nobody
    # there could hear.
    class FakeClock:
        def dismiss(self, satellite=None):
            self.dismissed = satellite
            return {"success": True, "message": "Okay.", "chime": True}

    clock = FakeClock()
    ActionExecutor(clock=clock, satellite="kitchen").execute(Action("dismiss_timer", {}))
    assert clock.dismissed == "kitchen"


# Confirming what was done

def test_a_controllers_flags_are_read_into_one_way_of_confirming():
    assert ActionResult.from_reply({"success": True, "silent": True, "message": "Next"}).confirm == "music"
    assert ActionResult.from_reply({"success": True, "chime": True, "message": "Paused"}).confirm == "chime"
    assert ActionResult.from_reply({"success": True, "message": "Timer set."}).confirm == "speak"
    # A failure is always said, whatever it was going to confirm with.
    assert ActionResult.from_reply({"success": False, "chime": True, "message": "No"}).confirm == "speak"


def test_a_request_assist_didnt_understand_is_told_apart_from_one_which_failed():
    assert ActionResult.from_reply({"success": False, "message": "x", "not_understood": True}).understood is False
    assert ActionResult.from_reply({"success": False, "message": "x"}).understood is True
    assert ActionResult.from_reply({"success": True}).understood is True


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
    for action in (Action("control_home", {"command": "lights on"}), Action("set_lights", {"on": True})):
        result = ActionExecutor().execute(action)
        assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet."), action.tool


def test_claude_isnt_offered_control_home_but_is_offered_set_lights():
    names = [schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()]
    assert "set_lights" in names and "control_home" not in names
    assert "set_lights" not in [schema["name"] for schema in ActionExecutor().tool_schemas()]


def test_dim_lights_isnt_offered_to_claude():
    names = [schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()]
    assert "dim_lights" not in names and "set_lights" in names


def test_dimming_is_passed_to_home_assistant():
    class FakeHome:
        def dim(self, direction, words):
            self.asked = (direction, words)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("dim_lights", {"direction": "up", "words": "the lamp"}))
    assert home.asked == ("up", "the lamp") and (result.ok, result.confirm) == (True, "chime")


def test_claude_cant_run_dim_lights_either():
    class FakeHome:
        def dim(self, direction, words):
            raise AssertionError("not for Claude")

    result = ActionExecutor(home=FakeHome()).execute(
        Action("dim_lights", {"direction": "down", "words": "the lights"}, from_model=True))
    assert result.ok is False


def test_without_home_assistant_dimming_says_so():
    result = ActionExecutor().execute(Action("dim_lights", {"direction": "down", "words": "the lights"}))
    assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet.")


def test_a_local_light_command_still_reaches_assist():
    # What Claude isn't offered can still be asked for: the local phrases ("lamp off") and the
    # offline keyword fallback both produce control_home, which goes to Assist as it always did.
    class FakeHome:
        def command(self, request, wider=False):
            self.asked = request
            return {"success": True, "message": "Turned off the lamp", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "lamp off"}))
    assert home.asked == "lamp off" and (result.ok, result.confirm) == (True, "chime")


def test_control_home_passes_the_wider_rule_on():
    class FakeHome:
        def command(self, request, wider=False):
            self.asked = (request, wider)
            return {"success": True, "message": "Done", "chime": True}

    home = FakeHome()
    ActionExecutor(home=home).execute(Action("control_home", {"command": "turn off the lights and the TV",
                                                              "wider": True}))
    assert home.asked == ("turn off the lights and the TV", True)
    ActionExecutor(home=home).execute(Action("control_home", {"command": "lamp off"}))
    assert home.asked == ("lamp off", False)


def test_a_refused_light_sentence_goes_to_novas_own_lights_and_nothing_else_does():
    class FakeHome:
        switched = None

        def command(self, request, wider=False):
            return {"success": False, "message": "I don't lock, unlock or open anything by voice.", "refused": True}

        def switch_lights(self, on, words, brightness=None):
            self.switched = (on, words, brightness)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "set the porch light to 40 percent", "on": True, "words": "the porch light", "brightness": 40}))
    assert home.switched == (True, "the porch light", 40) and (result.ok, result.confirm) == (True, "chime")

    # The offline fallback's sentence, which says nothing of switching lights, stays refused.
    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "turn off the front door"}))
    assert home.switched is None
    assert (result.ok, result.say) == (False, "I don't lock, unlock or open anything by voice.")


def test_a_refused_question_goes_to_claude_and_nothing_else_does():
    # "Is the garage door open?" is refused for its words, but Claude can answer it with
    # home_status, which only reads - never Assist, nor Nova's own lights.
    class FakeHome:
        switched = None
        asked = 0

        def command(self, request, wider=False):
            self.asked += 1
            return {"success": False, "message": "I don't lock, unlock or open anything by voice.", "refused": True}

        def switch_lights(self, on, words, brightness=None):
            self.switched = (on, words, brightness)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "is the garage door open", "wider": True, "question": True}))
    assert (result.ok, result.understood) == (False, False)
    assert home.asked == 1 and home.switched is None
    # A refusal that isn't a question is still said.
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "open the garage", "wider": True}))
    assert (result.ok, result.understood, result.say) == (False, True, "I don't lock, unlock or open anything by voice.")


def test_claude_cant_run_what_it_isnt_offered():
    # It can ask for any tool by name, offered or not - control_home passes free text to
    # Assist, so a made-up call to it mustn't run.
    class FakeHome:
        asked = None

        def command(self, request, wider=False):
            self.asked = request
            return {"success": True, "message": "Unlocked", "chime": True}

    home = FakeHome()
    result = ActionExecutor(home=home).execute(Action("control_home", {"command": "unlock the door"}, from_model=True))
    assert home.asked is None and result.ok is False


def test_claude_runs_a_script_or_scene_by_name_and_hears_what_happened():
    class FakeHome:
        asked = None

        def run_script(self, name):
            self.asked = name
            return {"success": True, "message": "Started Goodnight."}

    home = FakeHome()
    assert "run_home_script" in [schema["name"] for schema in ActionExecutor(home=home).tool_schemas()]
    result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": "goodnight"}, from_model=True))
    assert home.asked == "goodnight" and (result.ok, result.say, result.confirm) == (True, "Started Goodnight.", "speak")
    # Without a name, Home Assistant isn't asked.
    home.asked = None
    result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": " "}, from_model=True))
    assert home.asked is None and (result.ok, result.say) == (False, "I didn't catch which script or scene to run.")


def test_a_script_name_that_isnt_words_is_answered_as_no_name():
    class FakeHome:
        asked = None

        def run_script(self, name):
            self.asked = name
            return {"success": True, "message": "Started Goodnight."}

    home = FakeHome()
    for name in (5, ["goodnight"], None):
        result = ActionExecutor(home=home).execute(Action("run_home_script", {"name": name}, from_model=True))
        assert (result.ok, result.say) == (False, "I didn't catch which script or scene to run."), name
    assert home.asked is None


def test_without_home_assistant_no_script_is_offered_or_run():
    assert "run_home_script" not in [schema["name"] for schema in ActionExecutor().tool_schemas()]
    result = ActionExecutor().execute(Action("run_home_script", {"name": "goodnight"}, from_model=True))
    assert (result.ok, result.say) == (False, "Home Assistant isn't set up yet.")


def test_claude_works_the_lights_by_light_room_brightness_colour_and_warmth():
    class FakeHome:
        def set_lights(self, on, light=None, area=None, brightness=None, colour=None, warmth=None):
            self.asked = (on, light, area, brightness, colour, warmth)
            return {"success": True, "message": "Done.", "chime": True}

    home = FakeHome()
    executor = ActionExecutor(home=home)
    result = executor.execute(Action("set_lights", {"on": True, "area": "Kitchen", "brightness": 30, "colour": "blue",
                                                   "warmth": 2700, "volume": 11}))
    assert home.asked == (True, None, "Kitchen", 30, "blue", 2700)
    assert (result.ok, result.confirm) == (True, "chime")

    executor.execute(Action("set_lights", {"on": False, "light": "light.lamp"}))
    assert home.asked == (False, "light.lamp", None, None, None, None)


def test_the_lights_wont_be_switched_without_knowing_which_way():
    class FakeHome:
        def set_lights(self, on, **others):
            raise AssertionError("shouldn't be asked")

    result = ActionExecutor(home=FakeHome()).execute(Action("set_lights", {"area": "Kitchen"}))
    assert (result.ok, result.say) == (False, "I didn't catch whether to switch the lights on or off.")


def test_a_light_which_cant_be_worked_is_told_in_homes_words():
    class FakeHome:
        def set_lights(self, on, **others):
            return {"success": False, "message": "Which room?"}

    result = ActionExecutor(home=FakeHome()).execute(Action("set_lights", {"on": True}))
    assert (result.ok, result.say) == (False, "Which room?")


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


# Notifications

class FakeNtfy:
    """Stands in for the user's ntfy server, keeping what was sent to it."""

    def __init__(self, phone="clipboard", works=True):
        self.phone = phone
        self.works = works
        self.sent = []

    def send(self, topic, message, title="", priority=0):
        self.sent.append({"topic": topic, "title": title, "message": message})
        return self.works

    def unheard(self):
        return {"success": True, "message": "No new notifications."}

    def tell_more(self):
        return {"success": True, "message": "Backup failed. Error 5."}


def test_send_that_to_my_phone_sends_what_was_last_said_under_what_was_asked():
    ntfy = FakeNtfy()
    executor = ActionExecutor(ntfy=ntfy)
    executor.last_answer = ("What's the recipe for pancakes?", "Mix flour, eggs and milk, then fry them.")
    result = executor.execute(Action("send_to_phone"))

    assert (result.ok, result.confirm) == (True, "chime")
    assert ntfy.sent == [{"topic": "clipboard", "title": "What's the recipe for pancakes?",
                          "message": "Mix flour, eggs and milk, then fry them."}]


def test_claude_can_send_more_than_it_said():
    ntfy = FakeNtfy()
    ActionExecutor(ntfy=ntfy).execute(Action("send_to_phone", {"text": "100g flour, 2 eggs, 300ml milk...",
                                                              "title": "Pancake recipe"}))
    assert ntfy.sent == [{"topic": "clipboard", "title": "Pancake recipe", "message": "100g flour, 2 eggs, 300ml milk..."}]


def test_theres_nothing_to_send_before_anything_was_said():
    result = ActionExecutor(ntfy=FakeNtfy()).execute(Action("send_to_phone"))
    assert (result.ok, result.say) == (False, "There's nothing to send yet.")


def test_sending_says_when_it_couldnt():
    executor = ActionExecutor(ntfy=FakeNtfy(works=False))
    executor.last_answer = ("asked", "said")
    assert executor.execute(Action("send_to_phone")).say == "I couldn't reach your notifications."
    # Nor with nowhere set up to send it.
    assert ActionExecutor(ntfy=FakeNtfy(phone="")).execute(Action("send_to_phone")).ok is False


def test_notifications_are_only_offered_once_ntfy_is_set_up():
    def offered(executor):
        return {schema["name"] for schema in executor.tool_schemas()}

    tools = {"send_to_phone", "read_notifications", "tell_me_more"}
    assert tools <= offered(ActionExecutor(ntfy=FakeNtfy()))
    assert not tools & offered(ActionExecutor())
    assert ActionExecutor().execute(Action("read_notifications")).say == "Notifications aren't set up yet."


class FakeBatteries:
    """Stands in for the battery watcher, reading what it's given - or failing as Home Assistant can."""

    stale_after = 3600.0

    def __init__(self, batteries=(), fails=None):
        self.batteries, self.fails = list(batteries), fails

    def read(self):
        if self.fails is not None:
            raise self.fails
        return self.batteries


def _battery(name, level, kind="other", charging=False, age=0.0):
    return Battery(id=f"sensor.{name}", name=name, level=level, changed=time.time() - age, device=None,
                   kind=kind, phone=False, computer=False, charging=charging)


def test_get_batteries_says_each_one_found():
    executor = ActionExecutor(batteries=FakeBatteries([
        _battery("MacBook Pro", 72), _battery("S25 Ultra", 40, "mobile", False),
        _battery("Tab S11 Ultra", 88, "mobile", age=3 * 86400)]))
    everything = executor.execute(Action("get_batteries"))
    assert everything.ok
    assert everything.say == ("Your MacBook Pro's on 72%, and not charging. "
                              "Your S25 Ultra's on 40%, and not charging. "
                              "Your Tab S11 Ultra was on 88%, 3 days ago.")
    # The same sentence the local commands and the watcher use, for just the one asked about.
    assert executor.execute(Action("get_batteries", {"device": "mac"})).say == "Your MacBook Pro's on 72%, and not charging."
    assert executor.execute(Action("get_batteries", {"device": "tab"})).say == "Your Tab S11 Ultra was on 88%, 3 days ago."


def test_get_batteries_finds_nothing_called_that():
    executor = ActionExecutor(batteries=FakeBatteries([_battery("MacBook Pro", 72)]))
    result = executor.execute(Action("get_batteries", {"device": "kettle"}))
    assert (result.ok, result.say) == (False, "I can't find a battery called that.")


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("down"), ValueError("not JSON"), KeyError("unit"), TypeError("not a list")])
def test_get_batteries_with_home_assistant_down_says_so(failure):
    # A wrong-shaped answer raises KeyError or TypeError, not only ValueError.
    result = ActionExecutor(batteries=FakeBatteries(fails=failure)).execute(Action("get_batteries"))
    assert (result.ok, result.say) == (False, "I can't reach Home Assistant right now.")


def test_a_bug_reading_batteries_isnt_taken_for_home_assistant_being_down():
    with pytest.raises(RuntimeError):
        ActionExecutor(batteries=FakeBatteries(fails=RuntimeError("bug"))).execute(Action("get_batteries"))


def test_batteries_are_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"] for schema in executor.tool_schemas()}

    assert "get_batteries" in offered(ActionExecutor(batteries=FakeBatteries()))
    assert "get_batteries" not in offered(ActionExecutor())
    assert ActionExecutor().execute(Action("get_batteries")).say == "Battery levels need Home Assistant."


# Where the phone is, and ringing it - see devices.py

class FakeAppHome:
    """Stands in for Home Assistant: the app's devices, and the notifications it was asked to send."""

    def __init__(self, devices, notify_fails=None):
        self.devices = devices
        self.notify_fails = notify_fails
        self.sent = []

    def app_devices(self):
        return self.devices

    def notify(self, service, message, title=None, data=None):
        if self.notify_fails is not None:
            raise self.notify_fails
        self.sent.append((service, message, data))


def _app_device(name="s25-ultra", maker="samsung", model="SM-S938B", notify="mobile_app_s25_ultra",
                tracker="not_home", age=600.0):
    return {"device": f"dev_{name}", "name": name, "maker": maker, "model": model, "notify": notify,
            "tracker": {"state": tracker, "reported": time.time() - age},
            "place": {"state": "High Street, Leeds", "thoroughfare": "High Street", "locality": "Leeds"}}


def test_find_device_says_where_without_ringing():
    home = FakeAppHome([_app_device(tracker="home")])
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": False}))
    assert (result.ok, result.say, result.private) == (True, "Your phone's at home.", True)
    assert home.sent == []


def test_ring_my_phone_rings_wherever_the_user_is():
    # Away from home it's still rung - it may be in the car, or a friend's.
    home = FakeAppHome([_app_device()])
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert result.say == "Your phone's near High Street, Leeds, as of 10 minutes ago. Ringing your phone."
    assert result.private
    assert [(service, message) for service, message, _ in home.sent] == [("mobile_app_s25_ultra", "TTS")]


def test_ringing_is_only_said_once_home_assistant_took_it():
    failure = requests.HTTPError("500", response=SimpleNamespace(status_code=500))
    home = FakeAppHome([_app_device(tracker="home")], notify_fails=failure)
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert (result.ok, result.say) == (False, "Your phone's at home. I couldn't ring your phone.")
    assert result.private


def test_a_device_without_the_apps_notifications_cant_be_rung():
    mac = _app_device("macbook-pro", "Apple", "Mac16,8", notify=None, tracker="home")
    result = ActionExecutor(home=FakeAppHome([mac])).execute(Action("find_device", {"device": "mac", "ring": True}))
    assert result.say == ("Your Mac's at home. Your Mac can't be rung - it hasn't got the Home Assistant app's "
                          "notifications.")


def test_find_device_finds_nothing_called_that():
    result = ActionExecutor(home=FakeAppHome([_app_device()])).execute(Action("find_device", {"device": "kettle"}))
    assert (result.ok, result.say, result.private) == (False, "I haven't got a device called that.", True)


@pytest.mark.parametrize("failure", [requests.ConnectionError("down"), ValueError("not JSON"), KeyError("device")])
def test_find_device_with_home_assistant_down_says_so(failure):
    class Down(FakeAppHome):
        def app_devices(self):
            raise failure
    result = ActionExecutor(home=Down([])).execute(Action("find_device", {"device": "phone"}))
    assert (result.ok, result.say) == (False, "I can't reach Home Assistant right now.")


def test_find_device_is_offered_with_home_assistant():
    schema = {s["name"]: s for s in ActionExecutor(home=object()).tool_schemas()}["find_device"]
    assert schema["description"] == (
        "Say where one of the user's devices is - their phone, tablet or Mac - and ring it if asked ('find my "
        "phone', 'make my tablet ring'). What's found is said for you, so just reply 'Okay.'")
    assert "find_device" not in {s["name"] for s in ActionExecutor().tool_schemas()}
    # What it finds is said, never read back to Claude.
    assert "find_device" not in ActionExecutor().read_tools()


def test_a_private_reply_is_kept_private():
    assert ActionResult.from_reply({"success": True, "message": "Your phone's at home.", "private": True}).private
    assert not ActionResult.from_reply({"success": True, "message": "Done."}).private


# What Claude reads of the house

def test_home_status_is_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"]: schema for schema in executor.tool_schemas()}

    schema = offered(ActionExecutor(home=object()))["home_status"]
    assert schema["description"].startswith("Read the state of things in Home Assistant")
    assert schema["input_schema"]["required"] == ["about"]
    assert "home_status" not in offered(ActionExecutor())
    assert ActionExecutor().execute(Action("home_status", {"about": "sunset"})).say == "Home Assistant isn't set up yet."
    # It's the one tool whose answer goes back to Claude rather than being said.
    assert ActionExecutor().read_tools() == ["home_status"]


# Lists - Assist first, then Nova's own, through a real HomeAssistant and Lists with a fake
# in place of Home Assistant's API, so the guard and the intents asked for are the real ones.

class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeSession:
    """Stands in for Home Assistant's REST API, answering by path, or raising, and noting what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("8123", 1)[1]
        self.requests.append((path, kwargs.get("json")))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer(kwargs.get("json")) if callable(answer) else answer)


class FakeSocket:
    """Stands in for the WebSocket, answering Assist's sentence debugging with one match, or none."""

    def __init__(self, found):
        self.found = found

    def call(self, messages):
        return [{"results": [self.found]}]


def _debugged(intent, *targets):
    """Assist's sentence debugging's match, e.g. _debugged("HassListAddItem", "todo.shopping_list")."""
    return {"match": True, "intent": {"name": intent}, "targets": {target: {"matched": True} for target in targets},
            "source": "builtin"}


@pytest.fixture
def make_lists_executor(tmp_path, monkeypatch):
    from voice_assistant import network
    from voice_assistant.choices import ChoiceStore
    from voice_assistant.ha_registry import Entry, Snapshot
    from voice_assistant.home_assistant import HomeAssistant
    from voice_assistant.lists import Lists

    monkeypatch.setattr(network, "online", lambda: True)

    def factory(found, answers=None, items=("milk",), lists=None):
        # Each list's things, by its entity id - the shopping list's alone, unless a test says.
        lists = lists or {"todo.shopping_list": ("Shopping list", items)}
        snapshot = Snapshot(entries={entity: Entry(entity, "local_todo", None, None, entity, None, (), True)
                                     for entity in lists}, devices={}, areas={}, read_at=0.0)
        home = HomeAssistant("http://homeassistant.local:8123/", "token",
                             registry=type("Registry", (), {"snapshot": lambda self: snapshot})())
        home._session = FakeSession({
            "/api/states": [{"entity_id": entity, "state": str(len(things)), "attributes": {"friendly_name": name}}
                            for entity, (name, things) in lists.items()],
            "/api/conversation/process": {"response": {"response_type": "action_done",
                                                       "speech": {"plain": {"speech": "Added milk"}}}},
            "/api/services/todo/add_item": [],
            "/api/services/todo/update_item": [],
            "/api/services/todo/get_items?return_response": lambda asked: {"service_response": {asked["entity_id"]: {
                "items": [{"summary": item, "uid": f"uid-{item}", "status": "needs_action"}
                          for item in lists[asked["entity_id"]][1]]}}},
            **(answers or {})})
        home.socket = FakeSocket(found)
        return ActionExecutor(home=home, lists=Lists(home, ChoiceStore(str(tmp_path / "lists.json"), {"main": None}))), \
            home._session.requests
    return factory


def _sent(requests_made):
    """The paths sent to, bar the reads before Assist is asked."""
    return [path for path, _ in requests_made if path != "/api/states"]


def test_assist_adds_it_first_then_nova_does(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                     "command": "add milk to my shopping list"}))
    # Said, as adding to a list doesn't show itself the way a light does.
    assert (result.ok, result.say, result.confirm) == (True, "Added milk", "speak")
    assert _sent(sent) == ["/api/conversation/process"]

    # "Add milk to notes": Assist would add it with no list, which isn't sent.
    executor, sent = make_lists_executor(_debugged("HassListAddItem"))
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "shopping",
                                                     "command": "add milk to shopping"}))
    assert (result.ok, result.say) == (True, "Added milk to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]
    assert sent[-1][1] == {"entity_id": "todo.shopping_list", "item": "milk"}

    # The offline fallback gives no request for Assist, so Nova adds it straight away.
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "eggs milk and bread", "list": "shopping"}))
    assert (result.ok, result.say) == (True, "Added eggs milk and bread to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]


def test_a_list_sentence_the_guard_refuses_is_still_added_by_nova(make_lists_executor):
    # Its words say it would lock something, but Nova's lists only call todo services.
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"))
    result = executor.execute(Action("add_to_list", {"item": "a door lock", "list": "my shopping list",
                                                     "command": "add a door lock to my shopping list"}))
    assert (result.ok, result.say) == (True, "Added a door lock to your shopping list.")
    assert _sent(sent) == ["/api/services/todo/add_item"]


def test_a_sentence_assist_would_read_as_removing_is_never_sent(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListRemoveItem", "todo.shopping_list"))
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "take milk off my shopping list"}))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    assert "/api/conversation/process" not in _sent(sent)
    assert sent[-1] == ("/api/services/todo/update_item",
                        {"entity_id": "todo.shopping_list", "item": "uid-milk", "status": "completed"})


def test_ticking_off_goes_to_assist_only_as_completing(make_lists_executor):
    completed = {"response": {"response_type": "action_done", "speech": {"plain": {"speech": "Completed milk"}}}}
    executor, sent = make_lists_executor(_debugged("HassListCompleteItem", "todo.shopping_list"),
                                         {"/api/conversation/process": completed})
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "complete milk on my shopping list"}))
    assert (result.ok, result.say) == (True, "Completed milk")
    assert _sent(sent) == ["/api/conversation/process"]
    # "Tick off milk on my shopping list" is HassTurnOn to Assist, with nothing to act on.
    executor, sent = make_lists_executor(_debugged("HassTurnOn"))
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "tick off milk on my shopping list"}))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    assert "/api/conversation/process" not in _sent(sent)


def test_a_list_is_read_by_nova_never_assist(make_lists_executor):
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"), items=("milk", "eggs"))
    result = executor.execute(Action("read_list", {"list": "my shopping list"}))
    assert (result.ok, result.say) == (True, "Your shopping list has milk and eggs.")
    assert "/api/conversation/process" not in _sent(sent)


@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.Timeout("no answer")])
def test_what_names_no_list_goes_to_claude_with_home_assistant_down(make_lists_executor, failure):
    # "I've got a headache" is ticking something off to the local rule - but a sentence
    # about the world mustn't end at "I can't reach Home Assistant".
    executor, sent = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action("tick_off_list", {"item": "a headache", "list": None,
                                                       "command": "i've got a headache"}))
    assert (result.ok, result.understood) == (False, False)
    assert "/api/conversation/process" not in _sent(sent)
    # Nor does it with Home Assistant up and nothing on any list that fits.
    executor, _ = make_lists_executor(None)
    result = executor.execute(Action("tick_off_list", {"item": "a headache", "list": None,
                                                       "command": "i've got a headache"}))
    assert (result.ok, result.understood) == (False, False)
    # A list named is said to be out of reach.
    executor, _ = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action("tick_off_list", {"item": "milk", "list": "my shopping list",
                                                       "command": "tick off milk on my shopping list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't reach Home Assistant right now.")


def test_claude_adds_several_things_one_by_one(make_lists_executor):
    executor, sent = make_lists_executor(None)
    result = executor.execute(Action("manage_list", {"action": "add", "items": ["eggs", "milk", "bread"],
                                                     "list": "shopping"}, from_model=True))
    assert (result.ok, result.say) == (True, "Added eggs, milk and bread to your shopping list.")
    assert [sent_json["item"] for path, sent_json in sent if path == "/api/services/todo/add_item"] == [
        "eggs", "milk", "bread"]
    assert "/api/conversation/process" not in _sent(sent)


def test_claude_reads_and_ticks_off_one_thing(make_lists_executor):
    executor, _ = make_lists_executor(None)
    result = executor.execute(Action("manage_list", {"action": "read"}, from_model=True))
    assert (result.ok, result.say) == (True, "Your shopping list has milk.")
    result = executor.execute(Action("manage_list", {"action": "tick_off", "items": ["milk"]}, from_model=True))
    assert (result.ok, result.say) == (True, "Ticked off milk on your shopping list.")
    for wrong in ({"action": "tick_off"}, {"action": "delete", "items": ["milk"]}, {"action": "add", "items": "milk"}):
        assert executor.execute(Action("manage_list", wrong, from_model=True)).ok is False


def test_lists_are_only_offered_with_home_assistant():
    def offered(executor):
        return {schema["name"]: schema for schema in executor.tool_schemas()}

    schema = offered(ActionExecutor(lists=object()))["manage_list"]
    assert schema["description"].startswith("Add things to, read, or tick things off one of the user's to-do lists")
    assert schema["input_schema"]["required"] == ["action"]
    assert not {"add_to_list", "read_list", "tick_off_list"} & set(offered(ActionExecutor(lists=object())))
    assert "manage_list" not in offered(ActionExecutor())
    for tool_name in ("manage_list", "add_to_list", "read_list", "tick_off_list"):
        assert ActionExecutor().execute(Action(tool_name, {"action": "read"})).say == "Lists need Home Assistant."
    # Claude can't run the local ones, which pass free text to Assist.
    result = ActionExecutor(lists=object()).execute(Action("add_to_list", {"item": "milk"}, from_model=True))
    assert result.ok is False


def _ticked(sent):
    return [sent_json for path, sent_json in sent if path == "/api/services/todo/update_item"]


@pytest.mark.parametrize("said, on_list", [("flu", "Flu jab"), ("car", "Wash the car"), ("time", "Time sheet")])
def test_ive_got_ticks_off_only_an_exact_fit(make_lists_executor, said, on_list):
    # "I've got the flu" is a sentence about the world, never "Flu jab" ticked off - it's Claude's.
    executor, sent = make_lists_executor(None, items=(on_list,))
    result = executor.execute(Action("tick_off_list", {"item": said, "list": None, "exact": True}))
    assert (result.ok, result.understood) == (False, False)
    assert _ticked(sent) == [] and "/api/conversation/process" not in _sent(sent)
    # The thing itself is ticked off.
    executor, sent = make_lists_executor(None, items=(on_list, said))
    result = executor.execute(Action("tick_off_list", {"item": said, "list": None, "exact": True}))
    assert (result.ok, result.say) == (True, f"Ticked off {said} on your shopping list.")


def test_a_list_named_without_the_word_list_and_nothing_fitting_goes_to_claude(make_lists_executor):
    executor, sent = make_lists_executor(None, lists={"todo.work": ("Work", ("Send the report",))})
    result = executor.execute(Action("tick_off_list", {"item": "friday", "list": "work",
                                                       "command": "take friday off work"}))
    assert (result.ok, result.understood) == (False, False) and _ticked(sent) == []
    # Said, when the words say it's a list.
    result = executor.execute(Action("tick_off_list", {"item": "friday", "list": "my work list",
                                                       "command": "take friday off my work list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't see friday on your work list.")


@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.Timeout("no answer")])
@pytest.mark.parametrize("tool, params", [
    ("add_to_list", {"item": "drake", "list": "spotify", "command": "put drake on spotify"}),
    ("read_list", {"list": "netflix"}),
    ("tick_off_list", {"item": "friday", "list": "work", "command": "take friday off work"}),
])
def test_words_naming_no_list_go_to_claude_with_home_assistant_down(make_lists_executor, failure, tool, params):
    executor, sent = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action(tool, params))
    assert (result.ok, result.understood) == (False, False)
    # Words saying it's a list are told it can't be reached.
    executor, _ = make_lists_executor(None, {"/api/states": failure})
    result = executor.execute(Action(tool, {**params, "list": "my shopping list"}))
    assert (result.ok, result.understood, result.say) == (False, True, "I can't reach Home Assistant right now.")


def test_offline_a_list_that_isnt_there_says_claude_cant_be_reached(make_lists_executor):
    # The fallback's "add a reminder to call mum" isn't a list, and the offline message
    # (the model's reply) is said rather than "I haven't got a list called that.".
    executor, _ = make_lists_executor(None)
    results = [executor.execute(Action("add_to_list", {"item": "a reminder", "list": "call mum"}))]
    assert confirm(results, fallback="Sorry, I can't reach the internet right now.")[1] == [
        "Sorry, I can't reach the internet right now."]


def test_an_assist_error_after_sending_is_said_not_done_again(make_lists_executor):
    failed = {"response": {"response_type": "error", "data": {"code": "failed_to_handle"},
                           "speech": {"plain": {"speech": "An unexpected error occurred"}}}}
    executor, sent = make_lists_executor(_debugged("HassListAddItem", "todo.shopping_list"),
                                         {"/api/conversation/process": failed})
    result = executor.execute(Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                     "command": "add milk to my shopping list"}))
    assert (result.ok, result.say) == (False, "An unexpected error occurred")
    assert _sent(sent) == ["/api/conversation/process"]
