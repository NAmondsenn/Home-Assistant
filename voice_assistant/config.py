import os
import yaml
import logging

logger = logging.getLogger(__name__)

class Config:
    """
    Loads settings from config.yaml. This throws an error if the file is missing or invalid.
    Config exposes sections via .section(name) for easy access to specific parts of the configuration.
    """
    def __init__(self, path="~/config.yaml"):
        self.path = os.path.expanduser(path)
        
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Config file not found at {self.path}")
        
        with open(self.path) as f:
            self.data = yaml.safe_load(f)
        
        if not self.data:
            raise ValueError(f"Config file at {self.path} is empty or invalid")
        
        logger.info(f"Config loaded from {self.path}")

    def section(self, name):
        """
        Returns a specific section of the config such as 'audio' or 'llm' as a dict.
        Returns an empty dict if the section doesn't exist, so callers can fall back
        to their own defaults via .get() rather than crashing on optional sections.
        """
        return self.data.get(name, {})