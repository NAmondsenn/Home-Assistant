"""
Paths Module.

Where everything the assistant reads and writes is kept, decided in one place.

The repository is the code, and the defaults for every setting. What belongs to this
one install lives outside it, so a git pull can never conflict with it, a fresh clone
or a reinstall loses none of it, and none of it can ever be committed by mistake:

    ~/.config/nova/config.yaml   this install's settings, on top of the defaults
    ~/.config/nova/.env          keys and passwords
    ~/.local/share/nova/         timers, alarms, volumes, the Spotify sign-in, logs

NOVA_CONFIG and NOVA_DATA move those two folders - for a second copy, or the tests.
The models stay in the repository's models/ folder, which Git ignores: they're
downloaded by setup, not chosen, and large.

Everything used to live in the repository folder, and an install's own settings were
edited into the tracked config.yaml - so updating meant merging, and a reinstall
meant copying everything across by hand. migrate() moves what an older install left
there to its new place, once.
"""

import logging
import os
import shutil
from typing import List

from dotenv import load_dotenv

# Logger setup
logger = logging.getLogger(__name__)

PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every setting, with its default and a comment saying what it's for.
DEFAULTS_FILE = os.path.join(PROJECT_DIRECTORY, "config.yaml")
MODELS_DIR = os.path.join(PROJECT_DIRECTORY, "models")
SOUNDS_DIR = os.path.join(PROJECT_DIRECTORY, "sounds")

CONFIG_DIR = os.path.expanduser(os.environ.get("NOVA_CONFIG") or "~/.config/nova")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.yaml")
SECRETS_FILE = os.path.join(CONFIG_DIR, ".env")

DATA_DIR = os.path.expanduser(os.environ.get("NOVA_DATA") or "~/.local/share/nova")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SPOTIFY_TOKEN_FILE = os.path.join(DATA_DIR, "spotify_token.json")

# What an install from before this module kept in the repository folder, and where
# each now belongs.
OLD_PLACES = [
    ("config.local.yaml", CONFIG_FILE),
    (".env", SECRETS_FILE),
    ("data/timers.json", os.path.join(DATA_DIR, "timers.json")),
    ("data/alarms.json", os.path.join(DATA_DIR, "alarms.json")),
    ("data/volumes.json", os.path.join(DATA_DIR, "volumes.json")),
    (".spotify_cache", SPOTIFY_TOKEN_FILE),
    ("logs", LOGS_DIR),
]


def make_folders():
    """Makes the install's own folders, readable by this user only."""
    for folder in (CONFIG_DIR, DATA_DIR, LOGS_DIR):
        os.makedirs(folder, mode=0o700, exist_ok=True)


def migrate(repository: str = PROJECT_DIRECTORY) -> List[str]:
    """
    Moves anything an older install kept in the repository folder to where it now
    belongs - each only if there's nothing there already, so it's safe to run every
    time the assistant starts, and never overwrites anything.

    Args:
        repository: The repository folder - only given by the tests.

    Returns:
        What was moved, e.g. ["data/timers.json"].
    """
    make_folders()
    moved = []
    for old_name, new_path in OLD_PLACES:
        old_path = os.path.join(repository, old_name)
        if not os.path.exists(old_path):
            continue
        if os.path.isdir(old_path) and os.path.isdir(new_path):
            # A folder - the logs - goes across file by file, leaving anything
            # already in the new one alone.
            names = [name for name in os.listdir(old_path) if not os.path.exists(os.path.join(new_path, name))]
            for name in names:
                shutil.move(os.path.join(old_path, name), os.path.join(new_path, name))
            if not os.listdir(old_path):
                os.rmdir(old_path)
            if not names:
                continue
        elif not os.path.exists(new_path):
            shutil.move(old_path, new_path)
        else:
            logger.warning(f"{old_path} is still in the repository folder, but {new_path} already exists, "
                           f"so it's left where it is - it's no longer used")
            continue
        os.chmod(new_path, 0o700 if os.path.isdir(new_path) else 0o600)
        moved.append(old_name)
    # The folder the saved timers and volumes were in, once it's empty.
    old_data = os.path.join(repository, "data")
    if os.path.isdir(old_data) and not os.listdir(old_data):
        os.rmdir(old_data)
    return moved


def load_secrets():
    """Reads the keys and passwords in ~/.config/nova/.env into the environment."""
    load_dotenv(SECRETS_FILE)
