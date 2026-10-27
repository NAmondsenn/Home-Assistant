"""
Paths Module.

Where everything the assistant reads and writes is kept, decided in one place.

The repository is the code, and the defaults for every setting. What belongs to this
one install lives outside it, so a git pull can never conflict with it, a fresh clone
or a reinstall loses none of it, and none of it can ever be committed by mistake:

    ~/.config/nova/config.yaml   this install's settings, on top of the defaults
    ~/.config/nova/.env          keys and passwords
    ~/.local/share/nova/         timers, alarms, volumes, the Spotify and Google sign-ins, logs

NOVA_CONFIG and NOVA_DATA move those two folders - for a second copy, or the tests.
The models stay in the repository's models/ folder, which Git ignores: they're
downloaded by setup, not chosen, and large.

Everything used to live in the repository folder, and an install's own settings were
edited into the tracked config.yaml - so updating meant merging, and a reinstall
meant copying everything across by hand.
"""

import os

from dotenv import load_dotenv

PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every setting, with its default and a comment saying what it's for.
DEFAULTS_FILE = os.path.join(PROJECT_DIRECTORY, "config.yaml")
MODELS_DIR = os.path.join(PROJECT_DIRECTORY, "models")
SOUNDS_DIR = os.path.join(PROJECT_DIRECTORY, "sounds")

CONFIG_DIR = os.path.expanduser(os.environ.get("NOVA_CONFIG") or "~/.config/nova")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.yaml")
SECRETS_FILE = os.path.join(CONFIG_DIR, ".env")
PAGE_PASSWORD_FILE = os.path.join(CONFIG_DIR, "page_password")

DATA_DIR = os.path.expanduser(os.environ.get("NOVA_DATA") or "~/.local/share/nova")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SPOTIFY_TOKEN_FILE = os.path.join(DATA_DIR, "spotify_token.json")
GOOGLE_TOKEN_FILE = os.path.join(DATA_DIR, "google_token.json")
PAGE_LOGINS_FILE = os.path.join(DATA_DIR, "page_logins.json")


def make_folders():
    """Makes the install's own folders, readable by this user only."""
    for folder in (CONFIG_DIR, DATA_DIR, LOGS_DIR):
        os.makedirs(folder, mode=0o700, exist_ok=True)


def load_secrets():
    """Reads the keys and passwords in ~/.config/nova/.env into the environment."""
    load_dotenv(SECRETS_FILE)
