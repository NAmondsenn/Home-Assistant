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

import argparse
import hmac
import logging
import os
import socketserver
import sys
import time
from typing import List, Optional

import numpy as np

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.logging_setup import TRANSCRIPT, log_transcripts, setup_logging
from voice_assistant.protocol import ProtocolError, receive, send
from voice_assistant.satellite_passwords import MIN_PASSWORD, SatellitePasswords

logger = logging.getLogger(__name__)

# The shortest password accepted, so it can't be guessed by trying them all.
MIN_TOKEN = MIN_PASSWORD


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


def manage_satellites(argv: List[str]):
    """
    Adds or removes a satellite's password, then returns.

    Args:
        argv: add-satellite NAME [--password-stdin], or remove-satellite NAME.

    Raises:
        SystemExit: If the arguments are wrong, or the name is already there, or the
            password given is too short.
    """
    parser = argparse.ArgumentParser(prog="backend.py")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-satellite", help="let a satellite in, and print its new password")
    add.add_argument("name")
    # Reading it from stdin keeps an existing password out of the process list.
    add.add_argument("--password-stdin", action="store_true", help="take the password from stdin")
    commands.add_parser("remove-satellite", help="stop admitting a satellite").add_argument("name")
    args = parser.parse_args(argv)

    passwords = SatellitePasswords()
    if args.command == "remove-satellite":
        if not passwords.remove(args.name):
            sys.exit(f"There is no satellite called {args.name}")
        print(f"Removed {args.name}")
        return
    given = sys.stdin.readline().strip() if args.password_stdin else None
    try:
        password = passwords.add(args.name, given)
    except ValueError as e:
        sys.exit(str(e))
    if given is None:
        print(f"Added {args.name}. Its password, which is not shown again, is:\n{password}")
    else:
        print(f"Added {args.name}")


def main(argv: Optional[List[str]] = None):
    """
    Runs the backend, or - given add-satellite or remove-satellite - manages the
    satellites' passwords and returns.

    Args:
        argv: The command line after the program's name. None runs the server.
    """
    if argv:
        manage_satellites(argv)
        return
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
    main(sys.argv[1:])
