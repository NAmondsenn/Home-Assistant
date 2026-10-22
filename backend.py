"""
Nova's Backend
Runs Nova's brain on PVE2 - see voice_assistant/brain.py and docs/decisions/0006. Each
satellite - each Pi - connects to it, and it hears, answers and rings for them all.

It's only reachable over the tailnet: it listens on this machine alone, and Tailscale
passes the tailnet's connections on to it -

    tailscale serve --bg --tcp 10400 tcp://localhost:10400

- and each satellite has to prove its own password before anything else. The brain
keeps them, one for each satellite; on the satellite it's NOVA_BACKEND_TOKEN, in
~/.config/nova/.env. Run with:

    venv/bin/python backend.py

and let a satellite in, or stop letting it in, with:

    venv/bin/python backend.py add-satellite NAME
    venv/bin/python backend.py remove-satellite NAME
"""

import argparse
import os
import signal
import sys
from typing import List, Optional

from voice_assistant import paths
from voice_assistant.brain import Brain
from voice_assistant.config import Config
from voice_assistant.logging_setup import log_transcripts, setup_logging
from voice_assistant.satellite_passwords import SatellitePasswords


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
    Runs the brain, or - given add-satellite or remove-satellite - manages the
    satellites' passwords and returns.

    Args:
        argv: The command line after the program's name. None runs the brain.
    """
    if argv:
        manage_satellites(argv)
        return
    # Keys and passwords from ~/.config/nova/.env, then the settings - the repository's
    # config.yaml with this install's own on top. See paths.py.
    paths.load_secrets()
    setup_logging(os.path.join(paths.LOGS_DIR, "backend.log"))
    config = Config()
    # Whether what's said, and the replies, are kept in the logs - see logging_setup.py.
    log_transcripts(config.section("logging").get("log_transcripts", True))
    brain = Brain(config)
    # Ctrl+C and systemd's stop both end it properly - see Brain.shutdown.
    signal.signal(signal.SIGINT, brain.shutdown)
    signal.signal(signal.SIGTERM, brain.shutdown)
    brain.run()


if __name__ == "__main__":
    main(sys.argv[1:])
