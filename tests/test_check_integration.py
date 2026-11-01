"""
Check Integration Test
Checks scripts/manual/check_integration.py still runs against the assistant as it is now:
it builds the ActionExecutor with the parameters it has, offers Claude the tools it can run,
and reads what Claude asked for, and what each action came to, in the form they come in.
Claude, Spotify and Piper are stood in for, so nothing here needs an account or a speaker -
but the ActionExecutor is the real one, which is what the script once crashed on.
"""

import importlib.util
import os

from voice_assistant import paths
from voice_assistant.actions import Action

spec = importlib.util.spec_from_file_location(
    "check_integration",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "manual", "check_integration.py"))
check_integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_integration)


class Music:
    """Stands in for Spotify: plays what it's asked, or fails with nothing to say."""

    name = "Spotify"
    needs_internet = False

    def __init__(self, works=True):
        self.works = works

    def play(self, query=None, search_type=None):
        return {"success": True, "message": f"Playing {query}."} if self.works else {"success": False}


class Claude:
    """Stands in for LLMHandler, asking for play_music whenever it's told to play something."""

    made = []

    def __init__(self, config=None, api_key=None, tools=None, **kwargs):
        self.tools = tools
        Claude.made.append(self)

    def process_query(self, text):
        if "play" in text.lower():
            return {"response": "Playing some pop.", "actions": [Action("play_music", {"query": "pop"})],
                    "success": True}
        return {"response": "Fine, thanks.", "actions": [], "success": True}


def run(monkeypatch, capsys, music):
    """Runs the script against the stand-ins, and gives back what it printed."""
    Claude.made.clear()
    monkeypatch.setattr(paths, "load_secrets", lambda: None)
    monkeypatch.setattr(check_integration, "Config", lambda: object())
    monkeypatch.setattr(check_integration, "SpotifyController", lambda: music)
    monkeypatch.setattr(check_integration, "LLMHandler", Claude)

    def no_piper(config):
        raise OSError("no Piper here")

    monkeypatch.setattr(check_integration, "TextToSpeech", no_piper)
    check_integration.test_integration()
    return capsys.readouterr().out


def test_a_spotify_action_is_run_and_the_queries_pass(monkeypatch, capsys):
    out = run(monkeypatch, capsys, Music())

    assert "Detected action: Action(tool='play_music'" in out
    assert "Action result: ActionResult(ok=True, say='Playing pop.'" in out
    assert "[PASS] Hello, how are you?" in out
    assert "[PASS] Play some pop music" in out


def test_claude_is_offered_the_tools_the_music_gives(monkeypatch, capsys):
    run(monkeypatch, capsys, Music())

    # With none it could never ask for an action, and the script would pass without running one.
    assert "play_music" in [tool["name"] for tool in Claude.made[0].tools]


def test_an_action_which_fails_with_nothing_to_say_fails_the_query(monkeypatch, capsys):
    out = run(monkeypatch, capsys, Music(works=False))

    assert "[FAIL] Play some pop music" in out
    assert "[PASS] Hello, how are you?" in out
