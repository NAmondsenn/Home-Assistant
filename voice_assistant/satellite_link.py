"""
Satellite Link Module.

The satellite's connection to its brain - see docs/decisions/0006. It connects,
proves the password, and keeps itself connected: reconnecting when the connection
goes, pinging so a silent brain is noticed, and logging why it can't connect once
rather than on every try.
"""

import logging
import socket
import threading
from time import monotonic
from typing import Callable, Dict, Optional

from voice_assistant.protocol import ProtocolError, Refused, greet, receive, send

logger = logging.getLogger(__name__)

# How long any one connect, send or wait for more of a message may take. A send never
# blocks for longer, and the reading thread wakes this often to ping and check.
SOCKET_TIMEOUT = 2.0

# The wait before trying again, which doubles with each failure up to the longest.
RETRY_FIRST = 2.0
RETRY_LONGEST = 30.0

# How often to ping, and how long hearing nothing at all means the connection has gone.
PING_EVERY = 5.0
SILENT_FOR = 15.0


class _Stream:
    """
    A socket as the file protocol.send and receive expect.

    socket.makefile won't do: a read which times out leaves it unusable and may drop
    the half of a message it had, whereas here the half stays buffered and reading
    carries on once the link has had a look at the clock.
    """

    def __init__(self, sock: socket.socket):
        self._socket = sock
        self._buffer = bytearray()
        # Called whenever waiting for the brain times out. Raises if the wait is to end.
        self.when_idle: Callable[[], None] = self._give_up
        # Called as bytes arrive, so a big message coming slowly isn't mistaken for silence.
        self.when_heard: Callable[[], None] = lambda: None

    @staticmethod
    def _give_up():
        raise TimeoutError("The brain didn't answer in time")

    def write(self, data: bytes):
        self._socket.sendall(data)

    def flush(self):
        pass

    def _fill(self):
        while True:
            try:
                data = self._socket.recv(65536)
            except socket.timeout:
                self.when_idle()
                continue
            if not data:
                raise ConnectionError("The connection was closed")
            self._buffer += data
            self.when_heard()
            return

    def readline(self, limit: int = -1) -> bytes:
        searched = 0
        while True:
            end = self._buffer.find(b"\n", searched)
            if end >= 0:
                end += 1
            elif 0 <= limit <= len(self._buffer):
                end = limit
            if end >= 0:
                if 0 <= limit < end:
                    end = limit
                line = bytes(self._buffer[:end])
                del self._buffer[:end]
                return line
            searched = len(self._buffer)
            try:
                self._fill()
            except ConnectionError:
                # A cut-off line is for receive to call too short, as makefile's is.
                line = bytes(self._buffer)
                self._buffer.clear()
                return line

    def read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                self._fill()
            except ConnectionError:
                break
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


class SatelliteLink:
    """
    Keeps the satellite connected to its brain.

    on_message is called on the link's own reading thread, so a handler which is slow
    delays reading. pong is dealt with here and never reaches it.
    """

    def __init__(self, address: str, name: str, hostname: str, password: str, sample_rate: int,
                 unclean: bool, on_message: Callable[[Dict, bytes], None],
                 on_connected: Callable[[bool], None]):
        """
        Args:
            address: The brain's "name:port" on the tailnet - backend.address in config.yaml.
            name: This satellite's name, e.g. "bedroom".
            hostname: The machine it runs on.
            password: Its password.
            sample_rate: What its speaker plays at.
            unclean: Whether the last run stopped without being asked to. Only the first
                hello says so - the brain has heard it by the time of any reconnect.
            on_message: Called with each message from the brain and its audio, if any.
            on_connected: Called with True or False each time the connection changes.
        """
        host, _, port = address.rpartition(":")
        self.address = (host, int(port))
        self.name = name
        self.hostname = hostname
        self.password = password
        self.sample_rate = sample_rate
        self._unclean = unclean
        self._on_message = on_message
        self._on_connected = on_connected
        self._connected = False
        # What on_connected was last told, as _connected can change under it.
        self._announced = False
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Guards the socket and what's written to it, so messages can't be interleaved.
        self._lock = threading.Lock()
        self._socket: Optional[socket.socket] = None
        self._stream: Optional[_Stream] = None
        self._last_heard = 0.0
        self._last_ping = 0.0

    @property
    def connected(self) -> bool:
        """Whether the brain has accepted the satellite and not been lost since."""
        return self._connected

    def start(self):
        """Starts connecting, and keeps at it until stop."""
        self._thread = threading.Thread(target=self._run, name="satellite-link", daemon=True)
        self._thread.start()

    def stop(self):
        """Disconnects, and stops trying."""
        self._stopping.set()
        self._close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def send(self, kind: str, payload: bytes = b"", /, **fields) -> bool:
        """
        Sends the brain a message. Safe from any thread, and never blocks for longer
        than SOCKET_TIMEOUT.

        Args:
            kind: What it is, e.g. "wake".
            payload: Audio to go with it.
            fields: Anything else it carries - even a "kind", as the arguments above
                are positional only.

        Returns:
            False if it wasn't sent, because the satellite isn't connected or the
            connection failed just now.
        """
        with self._lock:
            if not self._connected or self._stream is None:
                return False
            try:
                send(self._stream, kind, payload, **fields)
                return True
            except OSError as e:
                logger.warning(f"Couldn't send '{kind}' to the brain ({e})")
                self._close_locked()
                return False

    def _close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        """Closes the socket, which ends the reading thread's wait if it is in one."""
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
        self._connected = False

    def _run(self):
        delay = RETRY_FIRST
        last_failure = None
        while not self._stopping.is_set():
            try:
                self._connect()
            except (OSError, ProtocolError, Refused) as e:
                if self._stopping.is_set():
                    break
                # Say why once, however long the brain stays away.
                failure = f"{type(e).__name__}: {e}"
                if failure != last_failure:
                    logger.warning(f"Can't connect to the brain at {self.address[0]} ({e}), "
                                   f"and will keep trying")
                    last_failure = failure
                self._stopping.wait(delay)
                delay = min(delay * 2, RETRY_LONGEST)
                continue
            last_failure = None
            delay = RETRY_FIRST
            logger.info(f"Connected to the brain at {self.address[0]}")
            # Together, so connected is never True without on_connected having been told.
            with self._lock:
                self._connected = True
            self._set_connected(True)
            try:
                self._serve()
            except (OSError, ProtocolError) as e:
                if not self._stopping.is_set():
                    logger.warning(f"Lost the brain at {self.address[0]} ({e})")
            finally:
                self._close()
                self._set_connected(False)
            self._stopping.wait(delay)

    def _connect(self):
        """Connects and proves the password, leaving the socket and stream ready."""
        sock = socket.create_connection(self.address, timeout=SOCKET_TIMEOUT)
        stream = _Stream(sock)
        try:
            greet(stream, self.name, self.hostname, self.password, self.sample_rate, self._unclean)
        except BaseException:
            sock.close()
            raise
        self._unclean = False
        with self._lock:
            if self._stopping.is_set():
                sock.close()
                raise ConnectionError("Stopped")
            self._socket = sock
            self._stream = stream
        self._last_heard = self._last_ping = monotonic()
        stream.when_idle = self._check_in
        stream.when_heard = self._heard

    def _set_connected(self, connected: bool):
        if connected != self._announced:
            self._announced = connected
            try:
                self._on_connected(connected)
            except Exception:
                logger.exception("on_connected failed")

    def _heard(self):
        self._last_heard = monotonic()

    def _check_in(self):
        """Run whenever the brain has been quiet for a moment: pings, or gives up on it."""
        now = monotonic()
        if now - self._last_heard >= SILENT_FOR:
            raise ConnectionError(f"Nothing heard from the brain for {SILENT_FOR:.0f} seconds")
        if now - self._last_ping >= PING_EVERY:
            self._last_ping = now
            self.send("ping")

    def _serve(self):
        """Reads what the brain sends until the connection ends."""
        while not self._stopping.is_set():
            # Every pass, as well as when the brain is quiet: while it streams music in
            # small pieces, reads never time out, but it still wants its pings.
            self._check_in()
            message, payload = receive(self._stream)
            if message["type"] == "pong":
                continue
            try:
                self._on_message(message, payload)
            except Exception:
                logger.exception(f"Handling '{message['type']}' from the brain failed")
