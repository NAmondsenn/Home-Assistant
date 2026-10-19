"""
Action Executor Test
Tests how actions are dispatched, using the real VolumeControl (saving into a
temporary folder) and a fake Spotify controller, so nothing plays or goes online.
"""

import pytest

from voice_assistant import volume as volume_module
from voice_assistant.actions import ActionExecutor, confirm
from voice_assistant.volume import VolumeControl


class FakeSpotify:
    """Stands in for SpotifyController, remembering the music volume it's given."""

    def __init__(self, playing, level=50):
        self.playing = playing
        self.level = level

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

    def factory(spotify=None):
        return ActionExecutor(spotify=spotify, volume=VolumeControl())

    return factory


def test_bare_volume_change_with_music_on_changes_the_music(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    result = executor.execute({"type": "volume", "category": None, "direction": "down"})
    assert result["success"] and spotify.level == 40
    # The assistant's own voice is left where it was.
    assert executor.volume.percent("general") == 70


def test_bare_volume_change_with_nothing_playing_changes_the_assistant(make_executor):
    spotify = FakeSpotify(playing=False, level=50)
    executor = make_executor(spotify)
    executor.execute({"type": "volume", "category": None, "direction": "up"})
    assert executor.volume.percent("general") == 80 and spotify.level == 50


def test_saying_which_volume_still_wins(make_executor):
    spotify = FakeSpotify(playing=True, level=50)
    executor = make_executor(spotify)
    executor.execute({"type": "volume", "category": "general", "level": 30})
    assert executor.volume.percent("general") == 30 and spotify.level == 50


def test_volume_without_spotify_falls_back_to_the_assistant(make_executor):
    executor = make_executor()
    executor.execute({"type": "volume", "category": None, "level": 55})
    assert executor.volume.percent("general") == 55


def test_volume_changes_are_confirmed_with_a_beep(make_executor):
    executor = make_executor(FakeSpotify(playing=False))
    result = executor.execute({"type": "volume", "category": "alarm", "direction": "down"})
    assert result["sound"] == "volume_beep.wav" and result["sound_category"] == "alarm"


def test_unknown_action_type_is_handled(make_executor):
    result = make_executor().execute({"type": "netflix", "command": "play"})
    assert result["success"] is False


# Confirming what was done

def test_an_action_which_confirms_itself_has_nothing_said_about_it():
    # Even when there's something else to say: a reminder offer after a skipped
    # song used to come out as "That song was shit anyway. Also, your reminder..."
    skipped = {"success": True, "silent": True, "message": "That song was shit anyway"}
    assert confirm([skipped], fallback="Okay.") == ([], [])


@pytest.mark.parametrize("result, chime_enabled, sounds", [
    ({"success": True, "sound": "volume_beep.wav", "sound_category": "alarm"}, True,
     [("volume_beep.wav", "alarm")]),
    ({"success": True, "chime": True, "message": "Paused"}, True, [("success_chime.wav", "general")]),
])
def test_an_action_with_a_sound_is_confirmed_by_it(result, chime_enabled, sounds):
    assert confirm([result], chime_enabled) == (sounds, [])


def test_a_chimed_action_is_said_out_loud_when_the_chime_is_off():
    assert confirm([{"success": True, "chime": True, "message": "Paused"}], chime_enabled=False) == ([], ["Paused"])


def test_a_failed_action_says_why_or_falls_back_to_the_models_reply():
    assert confirm([{"success": False, "message": "Spotify is not available right now."}],
                   fallback="Okay.") == ([], ["Spotify is not available right now."])
    assert confirm([{"success": False, "message": None}], fallback="Okay.") == ([], ["Okay."])
