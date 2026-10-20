"""
Protocol Module.

How a Pi and Nova's backend talk - see docs/decisions/0004. One connection, kept
open, carrying messages which are each a line of JSON, followed by the audio it says
comes with it, if any: Wyoming's message shape, without needing its library.

What a Pi sends, and what the backend answers:

    hello  {"token": ...}    ready, or refused - and the connection is closed
    ping                     pong
    listen                   (a recording is starting)
    audio  {"bytes": n}      (n bytes of it follow: 16-bit samples at 16kHz)
    finish                   text {"text": ...}, once the recording is over
    cancel                   (the recording came to nothing)
"""

import json
from typing import BinaryIO, Dict, Tuple

# The longest a line of JSON may be, so neither side can be kept reading for ever.
MAX_LINE = 4096

# The most audio one message may carry - 10 seconds of it.
MAX_AUDIO = 10 * 16000 * 2


class ProtocolError(Exception):
    """Something arrived which isn't a message."""


def send(stream: BinaryIO, kind: str, payload: bytes = b"", **fields):
    """
    Sends one message.

    Args:
        stream: The connection, as a file - socket.makefile("rwb").
        kind: What it is, e.g. "audio".
        payload: Audio to go with it.
        fields: Anything else it carries, e.g. text="Pause".
    """
    message = {"type": kind, **fields}
    if payload:
        message["bytes"] = len(payload)
    stream.write(json.dumps(message).encode() + b"\n" + payload)
    stream.flush()


def receive(stream: BinaryIO) -> Tuple[Dict, bytes]:
    """
    Waits for the next message.

    Args:
        stream: The connection, as a file.

    Returns:
        The message, and the audio which came with it - empty if none did.

    Raises:
        ConnectionError: The other side has gone.
        ProtocolError: What arrived wasn't a message.
    """
    line = stream.readline(MAX_LINE + 1)
    if not line:
        raise ConnectionError("The connection was closed")
    if not line.endswith(b"\n"):
        raise ProtocolError("A message was too long, or cut short")
    try:
        message = json.loads(line)
    except ValueError as e:
        raise ProtocolError(f"Not a message: {line[:80]!r}") from e
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError(f"Not a message: {line[:80]!r}")
    size = message.get("bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_AUDIO:
        raise ProtocolError(f"Can't take {size!r} bytes of audio in one message")
    payload = stream.read(size) if size else b""
    if len(payload) != size:
        raise ConnectionError("The connection was closed part way through a message")
    return message, payload
