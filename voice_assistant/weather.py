"""
Weather Module.

Answers "what's the weather?" from Home Assistant's own weather - Met's forecast, for
its home location - through weather.get_forecasts. See docs/decisions/0008.

It came from Open-Meteo, for a town or city in weather.location, which was one more
setting and one more service on the internet. Home Assistant already has the forecast,
so Nova says how old it is when Home Assistant last heard from Met over three hours ago.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import format_duration, spoken_clock_time
from voice_assistant.home_assistant import HomeAssistant

# Logger setup
logger = logging.getLogger(__name__)

# What Home Assistant's weather conditions mean, in words to say. Anything else -
# "exceptional" - is left unsaid.
CONDITIONS = {
    "partlycloudy": "partly cloudy", "rainy": "raining", "pouring": "pouring", "clear-night": "clear",
    "sunny": "sunny", "cloudy": "cloudy", "fog": "foggy", "snowy": "snowing", "snowy-rainy": "sleety",
    "lightning": "thundery", "lightning-rainy": "thundery", "hail": "hailing",
    "windy": "windy", "windy-variant": "windy",
}

# The conditions which are rain, whatever an hour's millimetres say.
RAINY = ("rainy", "pouring", "snowy-rainy", "lightning-rainy")

# The least rain in an hour, in millimetres, which is rain worth saying - Met gives a
# trace of 0.0 to most hours.
RAIN_MM = 0.1

# How old Home Assistant's forecast can be, in seconds, before Nova says how old it
# is - Home Assistant may not have heard from Met since, with its internet down.
OLD_FORECAST = 3 * 3600


class Weather:
    """The forecast for home, as Home Assistant has it."""

    def __init__(self, home: HomeAssistant, choices: ChoiceStore):
        """
        Args:
            home: The Home Assistant to ask.
            choices: Which of its weather entities to use, under "entity", for a home
                with several - None until one is chosen.
        """
        self.home = home
        self.choices = choices

    def forecast(self, day: str = "today", now: Optional[datetime] = None) -> Dict:
        """
        The weather as a sentence to say.

        Args:
            day: "today" (including how it is right now) or "tomorrow".
            now: The time to say it for, or None for now.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        now = (now or datetime.now()).astimezone()
        try:
            entities = self.home.weather_entities()
            if not entities:
                return {"success": False, "message": "Home Assistant hasn't got a weather forecast."}
            entity = self._entity(entities)
            if entity["state"] in ("unavailable", "unknown"):
                return {"success": False, "message": "Home Assistant's forecast isn't available right now."}
            wanted = now.date() + timedelta(days=1 if day == "tomorrow" else 0)
            daily = _on(self.home.forecasts(entity["id"], "daily"), wanted)
            entry = daily[0] if daily else {}
            chance = entry.get("precipitation_probability")
            if isinstance(chance, bool) or not isinstance(chance, (int, float)):
                chance = None
            # Met gives no chance of rain for a day, so the hours say when it's likely.
            hours = [] if chance is not None else [
                hour for hour in _on(self.home.forecasts(entity["id"], "hourly"), wanted)
                if _when(hour) + timedelta(hours=1) > now]
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Could not get the weather from Home Assistant: {e}")
            # Said as every other Home Assistant failure is - CANT_REACH while it's down.
            return {"success": False, "message": HomeAssistant._failure_message(e)}

        rain = _rain(chance, hours)
        if day == "tomorrow":
            message = _day_said("Tomorrow", [CONDITIONS.get(entry.get("condition"))] + _temperatures(entry), rain)
        else:
            now_said = _now_said(entity)
            message = " ".join(part for part in (now_said, _day_said("Today", _temperatures(entry), rain)) if part)
        if not message:
            message = f"Home Assistant's forecast has nothing for {day}."

        reported = entity.get("reported")
        if reported is not None and now.timestamp() - reported > OLD_FORECAST:
            message += f" That's Home Assistant's forecast from {format_duration(now.timestamp() - reported)} ago."
        return {"success": True, "message": message}

    def entities(self) -> List[Dict]:
        """
        Every weather entity Home Assistant has, for the settings page.

        Returns:
            Each as {"id", "name", "chosen"}, with "chosen" true for the one the forecast
            uses - the one chosen, else the first by entity id.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If its states can't be read.
        """
        found = self.home.weather_entities()
        used = self._entity(found)["id"] if found else None
        return [{"id": each["id"], "name": each["name"], "chosen": each["id"] == used} for each in found]

    def choose(self, entity_id: str, seen: Optional[List[Dict]] = None):
        """
        Chooses the weather entity the forecast uses, from the next request.

        Args:
            entity_id: The weather entity, which Home Assistant has to be showing.
            seen: What entities gave just before, if the caller read it already - so it can
                read without holding a lock the save needs.

        Raises:
            ValueError: If Home Assistant isn't showing that weather.
            OSError: If the choice can't be saved.
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        if seen is None:
            seen = self.entities()
        if not any(each["id"] == entity_id for each in seen):
            raise ValueError("Home Assistant isn't showing that weather just now.")
        self.choices.change(lambda state: state.update(entity=entity_id))

    def _entity(self, entities: List[Dict]) -> Dict:
        """The one weather entity, or with several, the one chosen - else the first by entity id."""
        chosen = self.choices.get("entity")
        for entity in entities:
            if entity["id"] == chosen:
                return entity
        return min(entities, key=lambda entity: entity["id"])


def _when(entry: Dict) -> Optional[datetime]:
    """When a forecast entry is for, in local time, or None if it doesn't say."""
    try:
        return datetime.fromisoformat(str(entry["datetime"])).astimezone()
    except (KeyError, ValueError):
        return None


def _on(entries: List[Dict], day: date) -> List[Dict]:
    """The forecast entries which fall on a local day, in order - leaving out any without a time."""
    return sorted((entry for entry in entries if _when(entry) and _when(entry).date() == day), key=_when)


def _temperatures(entry: Dict) -> List[str]:
    """A day's high and low, as far as Met gives them, e.g. ["a high of 14", "a low of 8"]."""
    said = []
    if entry.get("temperature") is not None:
        said.append(f"a high of {round(entry['temperature'])}")
    if entry.get("templow") is not None:
        said.append(f"a low of {round(entry['templow'])}")
    return [" and ".join(said)] if said else []


def _rain(chance: Optional[float], hours: List[Dict]) -> Optional[str]:
    """
    What to say about rain, e.g. "a 40 percent chance of rain".

    Args:
        chance: The day's chance of rain, in percent, or None if Met gave none.
        hours: The day's hourly entries still to come, used without a chance.

    Returns:
        The words, or None with neither to go by.
    """
    if chance is not None:
        chance = round(chance)
        if not chance:
            return "no rain expected"
        # "An 80", "an 11" and "an 18" are said with a vowel sound.
        article = "an" if str(chance).startswith("8") or chance in (11, 18) else "a"
        return f"{article} {chance} percent chance of rain"
    if not hours:
        return None
    rainy = [_when(hour) for hour in hours
             if (hour.get("precipitation") or 0) >= RAIN_MM or hour.get("condition") in RAINY]
    if not rainy:
        return "no rain expected"
    first, last = (spoken_clock_time(when.hour, 0).replace(":00", "") for when in (rainy[0], rainy[-1]))
    return f"rain likely around {first}" if first == last else f"rain likely from about {first} until {last}"


def _day_said(called: str, parts: List[Optional[str]], rain: Optional[str]) -> str:
    """
    A day's forecast as a sentence, e.g. "Tomorrow, cloudy, a high of 14 and a low of 8,
    and no rain expected." - with whatever Met left out left out.
    """
    parts = [part for part in parts if part]
    if not parts:
        return f"{called}, {rain}." if rain else ""
    said = ", ".join(parts)
    if rain:
        said += f", and {rain}" if rain.startswith("no ") else f", with {rain}"
    return f"{called}, {said}."


def _now_said(entity: Dict) -> str:
    """How it is at the moment, e.g. "It's 12 degrees and partly cloudy at the moment."."""
    temperature = entity["attributes"].get("temperature")
    conditions = CONDITIONS.get(entity["state"])
    said = [f"{round(temperature)} degrees"] if isinstance(temperature, (int, float)) else []
    said += [conditions] if conditions else []
    return f"It's {' and '.join(said)} at the moment." if said else ""
