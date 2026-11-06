"""
Protocol Test
Checks messages between a Pi and the backend arrive as they were sent, and that
anything which isn't a message is refused rather than read on for ever.
"""

import io
import json
import socket
import threading

import pytest

from voice_assistant import protocol


def test_a_message_and_its_audio_arrive_as_sent():
    stream = io.BytesIO()
    protocol.send(stream, "audio", b"\x01\x02\x03\x04")
    protocol.send(stream, "text", text="Pause")
    stream.seek(0)

    assert protocol.receive(stream) == ({"type": "audio", "bytes": 4}, b"\x01\x02\x03\x04")
    assert protocol.receive(stream) == ({"type": "text", "text": "Pause"}, b"")


def test_a_message_can_carry_a_field_named_kind():
    # As play_stream, play_sound, silence and allow all do.
    stream = io.BytesIO()
    protocol.send(stream, "silence", kind="announcement")
    stream.seek(0)

    assert protocol.receive(stream) == ({"type": "silence", "kind": "announcement"}, b"")


@pytest.mark.parametrize("sent", [
    b'{"type": "ping"',
    b"not a message\n",
    b'["ping"]\n',
    b'{"kind": "ping"}\n',
    b'{"type": "audio", "bytes": 999999999}\n',
    b'{"type": "audio", "bytes": -1}\n',
    b'{"type": "audio", "bytes": true}\n',
])
def test_what_isnt_a_message_is_refused(sent):
    with pytest.raises(protocol.ProtocolError):
        protocol.receive(io.BytesIO(sent))


def test_a_line_too_long_to_be_a_message_isnt_read_to_the_end():
    stream = io.BytesIO(b"x" * 100000 + b"\n")
    with pytest.raises(protocol.ProtocolError):
        protocol.receive(stream)
    assert stream.tell() <= protocol.MAX_LINE + 1


def test_the_other_side_going_is_told_apart_from_a_bad_message():
    with pytest.raises(ConnectionError):
        protocol.receive(io.BytesIO(b""))
    # Gone part way through the audio it said was coming.
    with pytest.raises(ConnectionError):
        protocol.receive(io.BytesIO(b'{"type": "audio", "bytes": 4}\n\x01\x02'))


def _handshake(satellite_password, brain_passwords, name="bedroom", **speaker):
    """Runs both sides over a socket pair, returning what each gave or raised, and what the brain sent."""
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    satellite_stream = near.makefile("rwb")
    brain_stream = far.makefile("rwb")
    outcome = {}
    sent = []

    class Recording:
        """Notes each message the brain sends."""

        def write(self, data):
            sent.append(json.loads(data.split(b"\n")[0])["type"])
            return brain_stream.write(data)

        def __getattr__(self, attribute):
            return getattr(brain_stream, attribute)

    def run(side, function):
        try:
            outcome[side] = function()
        except Exception as e:
            outcome[side] = e
        finally:
            # Hangs up as a caller would, since the makefile keeps the socket open otherwise.
            (near if side == "satellite" else far).shutdown(socket.SHUT_RDWR)

    threads = [
        threading.Thread(target=run, args=("satellite", lambda: protocol.greet(
            satellite_stream, name, "home-assistant", satellite_password, 48000, False, **speaker))),
        threading.Thread(target=run, args=("brain", lambda: protocol.admit(Recording(), brain_passwords.get))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return outcome, sent


PASSWORD = "correct-horse-battery-staple"


def test_the_handshake_admits_a_satellite_which_knows_its_password():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD})

    assert outcome["brain"] == {"name": "bedroom", "hostname": "home-assistant",
                                "sample_rate": 48000, "unclean": False, "speaker": None}
    assert outcome["satellite"] is None
    assert sent == ["challenge", "ready"]


def _admitted(**fields):
    """
    What admit gives back for a hello of the bedroom's with these fields as well, sent
    by hand - for what no satellite of today's would send. The password is right.
    """
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    brain_stream, satellite_stream = far.makefile("rwb"), near.makefile("rwb")
    outcome = {}

    def brain():
        try:
            outcome["brain"] = protocol.admit(brain_stream, {"bedroom": PASSWORD}.get)
        except Exception as e:
            outcome["brain"] = e

    thread = threading.Thread(target=brain)
    thread.start()
    nonce = "0" * 32
    protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                  unclean=False, nonce=nonce, **fields)
    challenge, _ = protocol.receive(satellite_stream)
    protocol.send(satellite_stream, "proof",
                  proof=protocol.proof(PASSWORD, "satellite", challenge["nonce"], nonce))
    assert protocol.receive(satellite_stream)[0]["type"] == "ready"
    thread.join(5)
    near.close()
    far.close()
    return outcome["brain"]


@pytest.mark.parametrize("nonce", ["z" * 32, "0" * 31 + "g", " " * 32, "0x" + "0" * 30, "\u0660" * 32, "0" * 31, "0" * 33],
                         ids=["letters", "one letter", "spaces", "0x", "arabic-indic digits", "short", "long"])
def test_a_hello_whose_nonce_isnt_32_hex_digits_is_refused(nonce):
    near, far = socket.socketpair()
    far.settimeout(1)
    try:
        protocol.send(near.makefile("rwb"), "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=False, nonce=nonce)
        with pytest.raises(protocol.ProtocolError):
            protocol.admit(far.makefile("rwb"), {"bedroom": PASSWORD}.get)
    finally:
        near.close()
        far.close()


def test_a_satellite_says_its_spotify_speaker():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD}, speaker="Bedroom Pi")

    assert outcome["brain"]["speaker"] == "Bedroom Pi"
    assert outcome["satellite"] is None
    assert sent == ["challenge", "ready"]


def test_a_hello_without_a_speaker_still_connects():
    # As an older satellite's has none - and one which doesn't make sense is only
    # left out, never the reason a satellite can't connect.
    assert _admitted()["speaker"] is None
    for speaker in ("x" * 200, 7, "", None, True, ["Bedroom Pi"], "Bedroom\nPi", "Bedroom\x00Pi"):
        assert _admitted(speaker=speaker) == {"name": "bedroom", "hostname": "h", "sample_rate": 48000,
                                              "unclean": False, "speaker": None}
    # Up to 64 characters, of any language.
    assert _admitted(speaker="x" * 64)["speaker"] == "x" * 64
    assert _admitted(speaker="Cuisine de Zoë")["speaker"] == "Cuisine de Zoë"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"], ids=["line separator", "paragraph separator"])
def test_a_speaker_with_a_line_or_paragraph_separator_is_left_out(separator):
    # Neither is a control character, but each ends a line as a newline does.
    assert _admitted(speaker=f"Bedroom{separator}Pi")["speaker"] is None
    # While the spaces which don't end a line still do for a name: "Bedroom Pi", and a non-breaking one.
    assert _admitted(speaker="Bedroom\u00a0Pi")["speaker"] == "Bedroom\u00a0Pi"


def test_a_speaker_left_out_is_logged(caplog):
    # So falling back to the brain's own speaker name can be told from the journal,
    # not only heard as "I can't find the Voice-Assistant speaker" - shown as ascii()
    # and cut short, so a hello can't write lines of its own into the journal.
    with caplog.at_level("WARNING", logger="voice_assistant.protocol"):
        _admitted(speaker="Bedroom\nPi" + "x" * 200)
    (record,) = caplog.records
    assert "Bedroom\\nPi" in record.getMessage() and "\n" not in record.getMessage()
    assert "x" * 100 not in record.getMessage()

    caplog.clear()
    with caplog.at_level("WARNING", logger="voice_assistant.protocol"):
        _admitted()
        _admitted(speaker="Bedroom Pi")
    # Nothing for a hello without one, as an older satellite's, or a good one.
    assert caplog.records == []


def test_an_old_brain_ignores_the_speaker():
    # A brain from before speakers were said lets in a satellite which says one, as
    # admit has always let in a hello with fields it doesn't know - which this checks
    # still holds, so either end can be updated first.
    admitted = _admitted(speaker="Bedroom Pi", colour="blue")
    assert admitted["name"] == "bedroom" and "colour" not in admitted


def test_a_wrong_password_is_refused():
    outcome, sent = _handshake("some-other-password-entirely", {"bedroom": PASSWORD})

    # The satellite finds the brain's proof wrong, and hangs up before giving one of its own.
    assert isinstance(outcome["satellite"], protocol.Refused)
    assert not isinstance(outcome["brain"], dict)
    assert sent == ["challenge"]


def test_the_brain_refuses_a_proof_which_is_wrong():
    near, far = socket.socketpair()
    near.settimeout(5)
    far.settimeout(5)
    brain_stream, satellite_stream = far.makefile("rwb"), near.makefile("rwb")
    outcome = []

    def brain():
        # Kept, as nothing raised in a thread is seen by the test.
        try:
            outcome.append(protocol.admit(brain_stream, {"bedroom": PASSWORD}.get))
        except Exception as e:
            outcome.append(e)

    thread = threading.Thread(target=brain)
    try:
        protocol.send(satellite_stream, "hello", name="bedroom", hostname="h", sample_rate=48000,
                      unclean=True, nonce="0" * 32)
        thread.start()
        challenge, _ = protocol.receive(satellite_stream)
        # Replies with the proof the brain itself gave, which is for the wrong role.
        protocol.send(satellite_stream, "proof", proof=challenge["proof"])

        assert protocol.receive(satellite_stream)[0]["type"] == "refused"
        thread.join(5)
        assert not thread.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], protocol.Refused)
    finally:
        for closing in (brain_stream, satellite_stream, near, far):
            closing.close()


def test_a_brain_which_cant_prove_the_password_is_refused():
    outcome, sent = _handshake(PASSWORD, {"bedroom": "some-other-password-entirely"})

    assert isinstance(outcome["satellite"], protocol.Refused)
    # The satellite hung up without proving anything, so the brain never got a proof.
    assert not isinstance(outcome["brain"], dict)
    assert sent == ["challenge"]


def test_an_unknown_satellite_is_refused():
    outcome, sent = _handshake(PASSWORD, {"bedroom": PASSWORD}, name="kitchen")

    assert isinstance(outcome["brain"], protocol.Refused)
    assert isinstance(outcome["satellite"], protocol.Refused)
    assert sent == ["refused"]


def test_a_proof_depends_on_who_gives_it():
    assert protocol.proof(PASSWORD, "brain", "a", "b") != protocol.proof(PASSWORD, "satellite", "a", "b")
    assert protocol.proof(PASSWORD, "brain", "a", "b") != protocol.proof(PASSWORD, "brain", "b", "a")
