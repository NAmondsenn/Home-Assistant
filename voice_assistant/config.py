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
from typing import Any, Dict, List, Optional, Tuple

import yaml

from voice_assistant import paths

logger = logging.getLogger(__name__)

# Settings which only make sense within a range, or as one of a few words - checked
# beyond being the right kind.
BETWEEN_0_AND_1 = {("thresholds", "vad_threshold"), ("thresholds", "wake_word_confidence"),
                   ("thresholds", "silence_threshold"), ("llm", "temperature")}
CHOICES = {("music", "player"): ("navidrome", "spotify")}
# Battery marks are percentages, and how many each kind of device has - the settings page
# allows no other number. The battery watcher can't check every 0 seconds or call a
# reading old after -5.
MARKS = {("batteries", "mobile_marks"): 1, ("batteries", "other_marks"): 2}
POSITIVE = {("batteries", "check_every"), ("batteries", "stale_after")}

# Sections an install's file may still have from when the Pi heard for itself, before
# the brain did - see docs/decisions/0006. Nothing reads them now, but a Pi's own
# settings often have them, and refusing them stopped the satellite and the brain both
# starting, so they're let through with a warning instead. Any other section config.yaml
# doesn't have is still a mistake.
RETIRED = ("speech_to_text", "moonshine", "whisper")

# Each setting which has left config.yaml, and where it's set now. An install's file
# often still has one - the brain's own had weather.location once the weather was Home
# Assistant's - and refusing it would stop Nova starting over a setting nothing reads,
# so it's let through, and a section holding only these, with a warning saying so.
REMOVED: Dict[Tuple[str, str], str] = {
    ("weather", "location"): "the weather is Home Assistant's own, for its home location",
}


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


def marks_problem(marks: Any, count: int, who: str) -> Optional[str]:
    """
    Says what is wrong with a list of battery marks, if anything - the one rule for
    config.yaml's and the settings page's alike, so the page can always save what
    config.yaml starts it with.

    Args:
        marks: The marks, e.g. [15, 5].
        count: How many there must be - one for a mobile device, two for anything else.
        who: What they belong to, to start the sentence with, e.g. "MacBook Pro" or
            "config.yaml: 'batteries.mobile_marks'".

    Returns:
        A sentence saying what is wrong, or None if they are fine: the right number of
        whole numbers from 1 to 99 (0% would never be reached, and 100 or more would
        warn about a full battery), all different.
    """
    if not isinstance(marks, list):
        return f"{who} needs {'one mark' if count == 1 else 'two marks'}, in a list, not {marks!r}."
    if len(marks) != count:
        return f"{who} needs {'one mark' if count == 1 else 'two marks'}, not {len(marks)}."
    if any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 99 for m in marks):
        return "A mark must be a whole number from 1 to 99."
    if len(set(marks)) != len(marks):
        return "The two marks must be different."
    return None


def removed(settings: Dict) -> List[Tuple[str, str]]:
    """
    The settings in REMOVED an install's file still has.

    Args:
        settings: The install's settings.

    Returns:
        Each as (section, name), e.g. [("weather", "location")].
    """
    return [(section, name) for section, values in settings.items() if isinstance(values, dict)
            for name in values if (section, name) in REMOVED]


def emptied(settings: Dict, defaults: Dict) -> List[str]:
    """
    The sections an install's file still has, empty, once their removed settings are
    deleted - "weather:" on its own, after the warning said "weather.location" could go.

    Args:
        settings: The install's settings.
        defaults: config.yaml's.

    Returns:
        Each section's name, e.g. ["weather"].
    """
    homes = {section for section, _ in REMOVED}
    return [section for section, values in settings.items()
            if section in homes and section not in defaults and values in (None, {})]


def check(settings: Dict, defaults: Dict, path: str) -> List[str]:
    """
    Checks an install's settings against config.yaml's.

    Args:
        settings: The install's settings.
        defaults: config.yaml's.
        path: The install's file, for the message.

    Returns:
        The sections it has which are no longer used - see RETIRED - and so aren't
        checked, and are to be left out. The settings in REMOVED aren't checked either,
        nor the sections emptied of them.

    Raises:
        ConfigError: Naming the first setting which is wrong, and how.
    """
    retired = [section for section in settings if section in RETIRED and section not in defaults]
    empty = emptied(settings, defaults)
    for section, values in settings.items():
        if section in retired or section in empty:
            continue
        if section not in defaults:
            if isinstance(values, dict) and values and all((section, name) in REMOVED for name in values):
                continue
            raise ConfigError(f"{path}: there's no '{section}' section.{_guess(section, defaults)}")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: '{section}' should be settings under it, indented, like config.yaml's")
        for name, value in values.items():
            if (section, name) in REMOVED:
                continue
            known = defaults[section]
            if name not in known:
                raise ConfigError(f"{path}: '{section}.{name}' isn't a setting.{_guess(name, known)}")
            default = known[name]
            if default is not None and value is not None and _kind(value) != _kind(default):
                raise ConfigError(f"{path}: '{section}.{name}' should be {_kind(default)}, not {value!r}")
            if (section, name) in BETWEEN_0_AND_1 and value is not None and not 0 <= value <= 1:
                raise ConfigError(f"{path}: '{section}.{name}' should be between 0 and 1, not {value}")
            if (section, name) in MARKS:
                setting = f"{path}: '{section}.{name}'"
                problem = marks_problem(value, MARKS[(section, name)], setting)
                if problem:
                    # Only some of the sentences begin with who they're about.
                    raise ConfigError(problem if problem.startswith(setting) else f"{setting}: {problem}")
            if (section, name) in POSITIVE and not (isinstance(value, (int, float)) and value > 0):
                raise ConfigError(f"{path}: '{section}.{name}' should be a number above 0, not {value!r}")
            choices = CHOICES.get((section, name))
            if choices and str(value).lower() not in choices:
                raise ConfigError(f"{path}: '{section}.{name}' should be one of {', '.join(choices)}, not {value!r}")
    return retired


class Config:
    """The settings, read once at startup. section() gives one part of them."""

    def __init__(self, path: str = None, defaults: str = None, quiet: bool = False):
        """
        Args:
            path: This install's settings file - ~/.config/nova/config.yaml unless
                given. It needn't exist: the defaults are used as they are.
            defaults: The file with every setting's default - the repository's
                config.yaml unless given.
            quiet: Not to log where the settings came from - for the settings page,
                which reads them every time it looks.

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

        # Only the settings it names are changed - "music: {player: ...}" leaves the
        # rest of config.yaml's music section as it is.
        if os.path.exists(self.path):
            settings = _read(self.path)
            retired = check(settings, self.data, self.path)
            gone = removed(settings)
            for name, values in settings.items():
                # A section holding only removed settings isn't config.yaml's any more.
                if name not in retired and name in self.data:
                    self.data[name] = {**self.data[name],
                                       **{key: value for key, value in values.items() if (name, key) not in gone}}
            if not quiet:
                logger.info(f"Settings loaded from {self.path}")
                if retired:
                    logger.warning(f"{self.path} still has the {', '.join(retired)} section"
                                   f"{'s' if len(retired) > 1 else ''}, which Nova no longer uses - "
                                   f"{'they' if len(retired) > 1 else 'it'} can be deleted")
                for section, name in gone:
                    logger.warning(f"{self.path}: '{section}.{name}' is no longer used - "
                                   f"{REMOVED[(section, name)]} - so it can be deleted")
                for section in emptied(settings, self.data):
                    where = "; ".join(where for (home, _), where in REMOVED.items() if home == section)
                    logger.warning(f"{self.path}: '{section}' is no longer used - {where} - so it can be deleted")

    def section(self, name):
        """
        Returns a specific section of the config such as 'audio' or 'llm' as a dict.
        Returns an empty dict if the section doesn't exist, so callers can fall back
        to their own defaults via .get() rather than crashing on optional sections.
        """
        return self.data.get(name, {})
