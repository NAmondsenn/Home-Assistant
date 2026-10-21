"""
Protocol Test
Checks messages between a Pi and the backend arrive as they were sent, and that
anything which isn't a message is refused rather than read on for ever.
"""

import io

import pytest

from voice_assistant import protocol


def test_a_message_and_its_audio_arrive_as_sent():
    stream = io.BytesIO()
    protocol.send(stream, "audio", b"\x01\x02\x03\x04")
    protocol.send(stream, "text", text="Pause")
    stream.seek(0)

    assert protocol.receive(stream) == ({"type": "audio", "bytes": 4}, b"\x01\x02\x03\x04")
    assert protocol.receive(stream) == ({"type": "text", "text": "Pause"}, b"")


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
