"""
Backend Test
Checks backend.py, which runs Nova's brain - see test_brain.py for the brain itself -
and looks after the satellites' passwords, and that a Pi still running Nova by itself
hears with its own speech-to-text when there's no backend to use. Needs numpy
installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import os
import signal
import socket

import pytest

np = pytest.importorskip("numpy")

import backend
from voice_assistant.speech_to_text import BackendSpeechToText

TOKEN = "correct-horse-battery-staple"


# Running the brain

def test_with_no_command_the_brain_runs_until_stopped(monkeypatch, make_config):
    happened, made = [], []
    config = make_config(logging={"log_transcripts": False})

    class Brain:
        def __init__(self, config):
            happened.append(("made", config))
            made.append(self)

        restarting = False

        def shutdown(self, *args):
            pass

        def run(self):
            happened.append("ran")

    handlers = {}
    monkeypatch.setattr(backend, "Brain", Brain)
    monkeypatch.setattr(backend, "Config", lambda: config)
    monkeypatch.setattr(backend.paths, "load_secrets", lambda: happened.append("secrets"))
    monkeypatch.setattr(backend, "setup_logging", lambda path: happened.append("logging"))
    monkeypatch.setattr(backend, "log_transcripts", lambda kept: happened.append(("transcripts", kept)))
    monkeypatch.setattr(backend.signal, "signal", lambda kind, handler: handlers.__setitem__(kind, handler))
    backend.main([])

    assert happened == ["secrets", "logging", ("transcripts", False), ("made", config), "ran"]
    # Ctrl+C and systemd's stop both end it properly.
    assert handlers == {signal.SIGINT: made[0].shutdown, signal.SIGTERM: made[0].shutdown}


def test_a_restart_from_the_settings_page_exits_with_the_status_systemd_restarts_on(monkeypatch, make_config):
    class Brain:
        restarting = True

        def __init__(self, config):
            pass

        def shutdown(self, *args):
            pass

        def run(self):
            pass

    monkeypatch.setattr(backend, "Brain", Brain)
    monkeypatch.setattr(backend, "Config", lambda: make_config(logging={"log_transcripts": False}))
    monkeypatch.setattr(backend.paths, "load_secrets", lambda: None)
    monkeypatch.setattr(backend, "setup_logging", lambda path: None)
    monkeypatch.setattr(backend, "log_transcripts", lambda kept: None)
    monkeypatch.setattr(backend.signal, "signal", lambda kind, handler: None)
    with pytest.raises(SystemExit) as stopped:
        backend.main([])
    assert stopped.value.code == backend.RESTART_EXIT_STATUS
    assert "RestartForceExitStatus=75" in open(os.path.join(os.path.dirname(backend.__file__), "nova-backend.service")).read()


# A Pi running Nova by itself

class FakeLocal:
    """Stands in for the Pi's own speech-to-text."""

    def __init__(self):
        self.transcribed = None

    def transcribe(self, audio):
        self.transcribed = len(audio)
        return {"text": "Heard on the Pi."}

    def listen(self):
        return "listening on the Pi"


@pytest.fixture
def local():
    """Loads the Pi's own speech-to-text when asked, keeping each one loaded."""
    loaded = []

    def load():
        loaded.append(FakeLocal())
        return loaded[-1]

    return loaded, load


def _nothing_listening():
    """An address nothing is listening on."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{spare.getsockname()[1]}"


def test_with_the_backend_down_the_pis_own_is_loaded_straight_away(local):
    loaded, load = local
    stt = BackendSpeechToText(_nothing_listening(), TOKEN, load)
    try:
        assert len(loaded) == 1 and not stt.reachable
        assert stt.listen() == "listening on the Pi"
    finally:
        stt.close()


# The satellites' passwords

@pytest.fixture
def passwords(tmp_path, monkeypatch):
    """The satellites' passwords, kept in the test's own folder."""
    from voice_assistant.satellite_passwords import SatellitePasswords

    kept = SatellitePasswords(str(tmp_path / "satellites.json"))
    monkeypatch.setattr(backend, "SatellitePasswords", lambda: kept)
    return kept


def test_add_satellite_prints_the_password_once(passwords, capsys):
    backend.main(["add-satellite", "bedroom"])

    assert passwords.get("bedroom") in capsys.readouterr().out
    with pytest.raises(SystemExit):
        backend.main(["add-satellite", "bedroom"])
    assert passwords.get("bedroom") not in capsys.readouterr().out


def test_add_satellite_can_take_an_existing_password_from_stdin(passwords, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("an-existing-password-here\n"))
    backend.main(["add-satellite", "bedroom", "--password-stdin"])

    assert passwords.get("bedroom") == "an-existing-password-here"
    assert "an-existing-password-here" not in capsys.readouterr().out


def test_remove_satellite_stops_admitting_it(passwords):
    passwords.add("bedroom")
    backend.main(["remove-satellite", "bedroom"])

    assert passwords.names() == []
