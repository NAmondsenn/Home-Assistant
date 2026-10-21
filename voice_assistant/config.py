"""
Config Module.

The assistant's settings: the repository's config.yaml - every setting, with its
default and a comment saying what it's for - with this install's own settings file
on top (~/.config/nova/config.yaml, see paths.py), which only names what's
different here, setting by setting.

The install's file is checked against config.yaml as it's read. A setting config.yaml
doesn't have - "silence_treshold" - or one of the wrong kind - "silence_duration:
quick" - stops the assistant starting, with a message naming the file, the setting
and, for a misspelling, what was probably meant. Both used to be ignored without a
word, leaving the default quietly in force.
"""

import difflib
import logging
import os
from typing import Any, Dict

import yaml

from voice_assistant import paths

logger = logging.getLogger(__name__)

# Settings which only make sense within a range, or as one of a few words - checked
# beyond being the right kind.
BETWEEN_0_AND_1 = {("thresholds", "vad_threshold"), ("thresholds", "wake_word_confidence"),
                   ("thresholds", "silence_threshold"), ("llm", "temperature")}
CHOICES = {("music", "player"): ("navidrome", "spotify")}


class ConfigError(ValueError):
    """A settings file which can't be used as it is."""


def _read(path: str) -> Dict:
    """A YAML file's settings, or a ConfigError saying why it can't be read."""
    try:
        with open(path) as file:
            data = yaml.safe_load(file)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} isn't valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should be sections of settings, like the config.yaml it's on top of")
    return data


def _kind(value: Any) -> str:
    """The kind of value a setting holds, as said in an error."""
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "words, in quotes"
    if isinstance(value, list):
        return "a list"
    return "settings"


def _guess(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def check(settings: Dict, defaults: Dict, path: str):
    """
    Checks an install's settings against config.yaml's.

    Args:
        settings: The install's settings.
        defaults: config.yaml's.
        path: The install's file, for the message.

    Raises:
        ConfigError: Naming the first setting which is wrong, and how.
    """
    for section, values in settings.items():
        if section not in defaults:
            raise ConfigError(f"{path}: there's no '{section}' section.{_guess(section, defaults)}")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: '{section}' should be settings under it, indented, like config.yaml's")
        for name, value in values.items():
            known = defaults[section]
            if name not in known:
                raise ConfigError(f"{path}: '{section}.{name}' isn't a setting.{_guess(name, known)}")
            default = known[name]
            if default is not None and value is not None and _kind(value) != _kind(default):
                raise ConfigError(f"{path}: '{section}.{name}' should be {_kind(default)}, not {value!r}")
            if (section, name) in BETWEEN_0_AND_1 and value is not None and not 0 <= value <= 1:
                raise ConfigError(f"{path}: '{section}.{name}' should be between 0 and 1, not {value}")
            choices = CHOICES.get((section, name))
            if choices and str(value).lower() not in choices:
                raise ConfigError(f"{path}: '{section}.{name}' should be one of {', '.join(choices)}, not {value!r}")


class Config:
    """The settings, read once at startup. section() gives one part of them."""

    def __init__(self, path: str = None, defaults: str = None):
        """
        Args:
            path: This install's settings file - ~/.config/nova/config.yaml unless
                given. It needn't exist: the defaults are used as they are.
            defaults: The file with every setting's default - the repository's
                config.yaml unless given.

        Raises:
            ConfigError: If either file can't be used, saying why.
        """
        self.defaults_path = defaults or paths.DEFAULTS_FILE
        self.path = os.path.expanduser(path or paths.CONFIG_FILE)

        if not os.path.exists(self.defaults_path):
            raise FileNotFoundError(f"Config file not found at {self.defaults_path}")
        self.data = _read(self.defaults_path)
        if not self.data:
            raise ValueError(f"Config file at {self.defaults_path} is empty or invalid")

        # Only the settings it names are changed - "weather: {location: ...}" leaves
        # the rest of config.yaml's weather section as it is.
        if os.path.exists(self.path):
            settings = _read(self.path)
            check(settings, self.data, self.path)
            for name, values in settings.items():
                self.data[name] = {**self.data[name], **values}
            logger.info(f"Settings loaded from {self.path}")

    def section(self, name):
        """
        Returns a specific section of the config such as 'audio' or 'llm' as a dict.
        Returns an empty dict if the section doesn't exist, so callers can fall back
        to their own defaults via .get() rather than crashing on optional sections.
        """
        return self.data.get(name, {})
