"""
Nova's Backend
Runs on PVE2, and hears for the Pis - for now only speech-to-text, stage 1 of
docs/decisions/0004. A Pi sends what its microphone hears while the user is still
talking, and gets the words back once they stop. Nemotron works on the audio as it
arrives, so there's little left to do by then - see voice_assistant/nemotron.py.

It's only reachable over the tailnet: it listens on this machine alone, and Tailscale
passes the tailnet's connections on to it -

    tailscale serve --bg --tcp 10400 tcp://localhost:10400

- and each Pi has to give the password in NOVA_BACKEND_TOKEN, in
~/.config/nova/.env on both machines, before anything else. Run with:

    venv/bin/python backend.py
"""

import hmac
import logging
import os
import socketserver
import time

import numpy as np

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.logging_setup import TRANSCRIPT, log_transcripts, setup_logging
from voice_assistant.protocol import ProtocolError, receive, send

logger = logging.getLogger(__name__)

# The shortest password accepted, so it can't be guessed by trying them all.
MIN_TOKEN = 16


class Connection(socketserver.StreamRequestHandler):
    """One Pi's connection, for as long as it keeps it open."""

    def handle(self):
        server = self.server
        try:
            message, _ = receive(self.rfile)
            if message["type"] != "hello" or not hmac.compare_digest(
                    str(message.get("token", "")).encode(), server.token.encode()):
                logger.warning(f"Refused {self.client_address[0]}, which didn't give the password")
                send(self.wfile, "refused")
                return
            send(self.wfile, "ready")
            logger.info(f"{self.client_address[0]} connected")
            self._serve(server.nemotron)
        except ConnectionError:
            logger.info(f"{self.client_address[0]} went")
        except (ProtocolError, OSError) as e:
            logger.warning(f"Dropped {self.client_address[0]}: {e}")

    def _serve(self, nemotron):
        """Answers the Pi's messages, one recording at a time, until it goes."""
        stream = None
        while True:
            message, payload = receive(self.rfile)
            kind = message["type"]
            if kind == "ping":
                send(self.wfile, "pong")
            elif kind == "listen":
                stream = nemotron.start()
            elif kind == "audio" and stream is not None and len(payload) % 2 == 0:
                nemotron.add(stream, np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768)
            elif kind == "finish" and stream is not None:
                started = time.monotonic()
                text = nemotron.finish(stream)
                stream = None
                logger.info(f"Heard, {time.monotonic() - started:.2f}s after the recording stopped: '{text}'",
                            extra=TRANSCRIPT)
                send(self.wfile, "text", text=text)
            elif kind == "cancel":
                stream = None
            else:
                raise ProtocolError(f"Didn't expect '{kind}' then")


class Server(socketserver.ThreadingTCPServer):
    """Listens for Pis, each on a thread of its own."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, token: str, nemotron):
        """
        Args:
            port: Where to listen, on this machine alone.
            token: The password each Pi has to give first.
            nemotron: What hears - see voice_assistant/nemotron.py.

        Raises:
            ValueError: If the password is too short to be safe.
        """
        if len(token) < MIN_TOKEN:
            raise ValueError(f"The password, NOVA_BACKEND_TOKEN in {paths.SECRETS_FILE}, has to be at least "
                             f"{MIN_TOKEN} characters long")
        self.token = token
        self.nemotron = nemotron
        super().__init__(("127.0.0.1", port), Connection)


def main():
    paths.load_secrets()
    setup_logging(os.path.join(paths.LOGS_DIR, "backend.log"))
    config = Config()
    log_transcripts(config.section("logging").get("log_transcripts", True))
    settings = config.section("backend")

    # Imported here, so the rest of Nova never needs sherpa-onnx.
    from voice_assistant.nemotron import Nemotron

    logger.info("Loading Nemotron...")
    nemotron = Nemotron(os.path.join(paths.PROJECT_DIRECTORY, settings.get("model")))
    server = Server(settings.get("port", 10400), os.getenv("NOVA_BACKEND_TOKEN", ""), nemotron)
    logger.info(f"Listening on port {server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
