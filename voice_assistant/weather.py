"""
Weather Module.

Answers "what's the weather?" from Open-Meteo, which is free and needs no API key.

The question used to be impossible to answer: web search only switched on for words
like "search" or "look up", so the model said it couldn't check - for one of the
questions people ask a voice assistant most. A forecast service is cheaper, quicker
and more predictable than searching the web for it.
"""

import logging
from typing import Dict, Optional

import requests

from voice_assistant import network

# Logger setup
logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Seconds to wait for Open-Meteo before giving up.
REQUEST_TIMEOUT = 4

# What Open-Meteo's weather codes (the WMO's) mean, in words to say.
CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "foggy",
    51: "drizzly", 53: "drizzly", 55: "drizzly", 56: "drizzly", 57: "drizzly",
    61: "raining lightly", 63: "raining", 65: "raining heavily", 66: "raining", 67: "raining heavily",
    71: "snowing lightly", 73: "snowing", 75: "snowing heavily", 77: "snowing",
    80: "showery", 81: "showery", 82: "pouring", 85: "snowing", 86: "snowing heavily",
    95: "thundery", 96: "thundery", 99: "thundery",
}


class Weather:
    """The forecast for one place - home, from weather.location in config.yaml."""

    def __init__(self, config=None):
        """
        Args:
            config: Configs loaded from config.yaml, for the location.
        """
        weather_config = config.section("weather") if config else {}
        self.location = (weather_config.get("location") or "").strip()
        # Looked up the first time it's needed, then kept - it doesn't move.
        self._place: Optional[Dict] = None

    def forecast(self, day: str = "today") -> Dict:
        """
        The weather as a sentence to say.

        Args:
            day: "today" (including how it is right now) or "tomorrow".

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if not self.location:
            return {"success": False,
                    "message": "I don't know where you are yet. Set weather location in config.yaml."}
        if not network.online():
            return {"success": False, "message": "I can't get the weather without the internet."}

        try:
            place = self._find_place()
            if place is None:
                return {"success": False, "message": f"I couldn't find {self.location} to get the weather for."}

            response = requests.get(FORECAST_URL, timeout=REQUEST_TIMEOUT, params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "forecast_days": 2,
            })
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning(f"Could not get the weather: {e}")
            return {"success": False, "message": "Sorry, I couldn't get the weather right now."}

        index = 1 if day == "tomorrow" else 0
        daily = data["daily"]
        high = round(daily["temperature_2m_max"][index])
        low = round(daily["temperature_2m_min"][index])
        rain = daily["precipitation_probability_max"][index]
        # "An 80", "an 11" and "an 18" are said with a vowel sound.
        article = "an" if str(rain).startswith("8") or rain in (11, 18) else "a"
        rain_part = f", with {article} {rain} percent chance of rain" if rain else ", and no rain expected"

        if day == "tomorrow":
            conditions = CONDITIONS.get(daily["weather_code"][1], "")
            looks = f"{conditions}, " if conditions else ""
            message = f"Tomorrow, {looks}a high of {high} and a low of {low}{rain_part}."
        else:
            current = data["current"]
            conditions = CONDITIONS.get(current["weather_code"])
            now = f"It's {round(current['temperature_2m'])} degrees" + (f" and {conditions}" if conditions else "")
            message = f"{now} at the moment. Today, a high of {high} and a low of {low}{rain_part}."
        return {"success": True, "message": message}

    def _find_place(self) -> Optional[Dict]:
        """
        Where weather.location is, from Open-Meteo's place search.

        Returns:
            Dict with 'latitude' and 'longitude', or None if nowhere matched.
        """
        if self._place is None:
            response = requests.get(GEOCODING_URL, timeout=REQUEST_TIMEOUT,
                                    params={"name": self.location, "count": 1, "language": "en", "format": "json"})
            response.raise_for_status()
            results = response.json().get("results") or []
            if not results:
                return None
            self._place = {"latitude": results[0]["latitude"], "longitude": results[0]["longitude"]}
            logger.info(f"Weather is for {results[0].get('name')}, {results[0].get('country')}")
        return self._place
