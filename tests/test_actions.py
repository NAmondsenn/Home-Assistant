"""
Action Executor Test
Tests how actions are dispatched, using the real VolumeControl (saving into a
temporary folder) and a fake Spotify controller, so nothing plays or goes online.
"""

import time

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
