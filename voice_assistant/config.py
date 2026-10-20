import os
import yaml
import logging

logger = logging.getLogger(__name__)

# Settings for this one machine, kept beside config.yaml but never committed (see
# .gitignore) - things like the town the weather is for, which have no business in
# a public repository, and which an update shouldn't overwrite.
LOCAL_FILENAME = "config.local.yaml"

class Config:
    """
    Loads settings from config.yaml, then anything in config.local.yaml on top. This throws an error if
    config.yaml is missing or invalid. Config exposes sections via .section(name) for easy access to
    specific parts of the configuration.
    """

    def __init__(self, path=None):
        # Defaults to config.yaml in the project root (the parent of the
        # voice_assistant package), so the project works anywhere when cloned / run.
        if path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(project_root, "config.yaml")
        self.path = os.path.expanduser(path)
        
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Config file not found at {self.path}")
        
        with open(self.path) as f:
            self.data = yaml.safe_load(f)
        
        if not self.data:
            raise ValueError(f"Config file at {self.path} is empty or invalid")
        
        logger.info(f"Config loaded from {self.path}")

        # Only the settings it names are changed - "weather: {location: ...}" leaves
        # the rest of config.yaml's weather section as it is.
        local_path = os.path.join(os.path.dirname(self.path), LOCAL_FILENAME)
        if os.path.exists(local_path):
            with open(local_path) as f:
                local = yaml.safe_load(f) or {}
            for name, settings in local.items():
                if isinstance(settings, dict) and isinstance(self.data.get(name), dict):
                    self.data[name] = {**self.data[name], **settings}
                else:
                    self.data[name] = settings
            logger.info(f"Local settings loaded from {local_path}")

    def section(self, name):
        """
        Returns a specific section of the config such as 'audio' or 'llm' as a dict.
        Returns an empty dict if the section doesn't exist, so callers can fall back
        to their own defaults via .get() rather than crashing on optional sections.
        """
        return self.data.get(name, {})