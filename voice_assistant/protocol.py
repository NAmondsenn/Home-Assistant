"""
Protocol Module.

How a Pi and Nova's backend talk - see docs/decisions/0004. One connection, kept
open, carrying messages which are each a line of JSON, followed by the audio it says
comes with it, if any: Wyoming's message shape, without needing its library.

After the handshake, what a satellite sends the brain:

    ping
    wake  {"talked_over": ...}
    audio  (payload: 16-bit samples at 16kHz)
    audio_end  {"reason": "limit"|"cancelled"}
    played  {"id": ..., "stopped": ..., "position": ...}
    progress  {"id": ..., "position": ...}
    away  {"rang": [ids], "dismissed": [ids]}

and what the brain sends a satellite:

    pong
    listen
    stop_listening
    play_sound  {"id": ..., "name": ..., "volume": ..., "kind": ..., "start": ..., "length": ...}
    play_stream  {"id": ..., "rate": ..., "channels": ..., "volume": ..., "kind": ..., "paused": ...}
    stream_audio  {"id": ...}  (payload)
    stream_end  {"id": ...}
    set_playback  {"id": ..., "paused"?: ..., "volume"?: ...}
    stop  {"kinds": [...]|null} or {"id": ...}  (whole kinds of sound, or one playback)
    silence  {"kind": ...}
    allow  {"kind": ...}
    state  {"volumes": {"general": ..., "alarm": ..., "music": ...}, "muted": ...}
    alarms  {"entries": [{"id": ..., "due": ..., "kind": ..., "sound": ...}]}
    phrase  {"rate": ...}  (payload)
    restart

The handshake comes first, and proves the satellite's password to the brain and the
brain's to the satellite, without the password itself ever being sent:

    hello  {name, hostname, sample_rate, unclean, nonce}    (satellite)
    challenge  {nonce, proof}    (brain; or refused, and the connection is closed)
    proof  {proof}    (satellite, once the brain's proof is right)
    ready, or refused    (brain)
"""

import hmac
import json
import secrets
from typing import BinaryIO, Callable, Dict, Optional, Tuple

# The longest a line of JSON may be, so neither side can be kept reading for ever.
MAX_LINE = 4096

# The most audio one message may carry - 10 seconds of it.
MAX_AUDIO = 10 * 16000 * 2


class ProtocolError(Exception):
    """Something arrived which isn't a message."""


def send(stream: BinaryIO, kind: str, payload: bytes = b"", /, **fields):
    """
    Sends one message.

    Args:
        stream: The connection, as a file - socket.makefile("rwb").
        kind: What it is, e.g. "audio".
        payload: Audio to go with it.
        fields: Anything else it carries, e.g. text="Pause". The arguments above are
            positional only, so a field may share a name with one - as play_stream's
            "kind" does.
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


class Refused(Exception):
    """The other side couldn't prove the password, or the brain doesn't know the satellite."""


def proof(password: str, role: str, first: str, second: str) -> str:
    """
    Proves the password without sending it.

    Args:
        password: The satellite's password.
        role: Who is proving it, "brain" or "satellite". Part of what is signed, so
            one side's proof can't be replayed back to it as the other's.
        first: The nonce of whoever is being proved to.
        second: The nonce of whoever is proving.

    Returns:
        The HMAC-SHA256 of the three, in hex.
    """
    message = f"{role}|{first}|{second}".encode()
    return hmac.new(password.encode(), message, "sha256").hexdigest()


def _expect(stream: BinaryIO, kind: str, *fields: str) -> Dict:
    """Receives the next message, which has to be this kind with these fields as strings."""
    message, _ = receive(stream)
    if message["type"] == "refused":
        raise Refused("The brain refused the satellite")
    if message["type"] != kind or not all(isinstance(message.get(f), str) for f in fields):
        raise ProtocolError(f"Expected {kind}, not {message['type']}")
    return message


def greet(stream: BinaryIO, name: str, hostname: str, password: str, sample_rate: int, unclean: bool):
    """
    The satellite's side of the handshake.

    Args:
        stream: The connection, as a file.
        name: This satellite's name, e.g. "bedroom".
        hostname: The machine it runs on.
        password: Its password.
        sample_rate: What its speaker plays at.
        unclean: Whether it restarted without being asked to.

    Raises:
        Refused: The brain refused it, or couldn't prove it knows the password - in
            which case nothing of the satellite's has been given away.
        ProtocolError, ConnectionError: As receive does.
    """
    nonce = secrets.token_hex(16)
    send(stream, "hello", name=name, hostname=hostname, sample_rate=sample_rate,
         unclean=unclean, nonce=nonce)
    challenge = _expect(stream, "challenge", "nonce", "proof")
    expected = proof(password, "brain", nonce, challenge["nonce"])
    if not hmac.compare_digest(expected, challenge["proof"]):
        raise Refused("The brain couldn't prove it knows the password")
    send(stream, "proof", proof=proof(password, "satellite", challenge["nonce"], nonce))
    _expect(stream, "ready")


def admit(stream: BinaryIO, password_for: Callable[[str], Optional[str]]) -> Dict:
    """
    The brain's side of the handshake.

    Args:
        stream: The connection, as a file.
        password_for: Looks a satellite's password up by name - None if unknown.

    Returns:
        The hello's name, hostname, sample_rate and unclean.

    Raises:
        Refused: The satellite is unknown or got the password wrong. It has been told so.
        ProtocolError, ConnectionError: As receive does.
    """
    hello, _ = receive(stream)
    nonce = hello.get("nonce")
    name = hello.get("name")
    hostname = hello.get("hostname")
    sample_rate = hello.get("sample_rate")
    unclean = hello.get("unclean")
    if (hello["type"] != "hello" or not isinstance(name, str) or not isinstance(hostname, str)
            or isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
            or not isinstance(unclean, bool) or not isinstance(nonce, str) or len(nonce) != 32):
        raise ProtocolError("Expected a hello")
    password = password_for(name)
    if password is None:
        send(stream, "refused")
        raise Refused(f"No satellite called {name}")
    brain_nonce = secrets.token_hex(16)
    send(stream, "challenge", nonce=brain_nonce, proof=proof(password, "brain", nonce, brain_nonce))
    reply, _ = receive(stream)
    if (reply["type"] != "proof" or not isinstance(reply.get("proof"), str)
            or not hmac.compare_digest(proof(password, "satellite", brain_nonce, nonce), reply["proof"])):
        send(stream, "refused")
        raise Refused(f"{name} got the password wrong")
    send(stream, "ready")
    return {"name": name, "hostname": hostname, "sample_rate": sample_rate, "unclean": unclean}
