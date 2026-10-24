# Lists, devices and the weather - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova reads the weather from Met through Home Assistant; adds to, reads and ticks off Home Assistant's to-do lists; says where the phone is and rings it; sends things to the Mac or tablet; and turns the PC on and off - mostly without Claude, and with the internet down.

**Architecture:** Each piece has a small module of its own rules - a rewritten `weather.py`, and new `lists.py`, `devices.py` and `pc.py` - over new reads and calls in `HomeAssistant`, every call behind `SERVICES`. The choices decision 15 adds are kept in the data folder with their defaults, through `choices.py`'s `ChoiceStore`, lifted out of 0007's `BatteryChoices`. The new local rules come before plan 1's Assist shapes. Shutting the PC down asks first, through a new pending yes in `Conversation`.

**Tech stack:** Python 3.13 on the brain, Home Assistant's REST API (states, templates, services with `return_response`) and plan 1's registry `Snapshot`, ntfy, pytest. No new dependency.

**Spec:** `docs/decisions/0008-home-assistant-first.md` - decisions 5, 8, 9, 12, 13, 14 (its allowlist table) and 15 - and `docs/decisions/0009-the-settings-page.md` decisions 1 and 2, for the helper and the removed setting. Read 0008 before starting any task. It's built after `2026-10-24-assist-and-safety.md`, whose `HomeRegistry`, `Snapshot`, `command(request, wider, only)` and `PC_KINDS` it uses.

## Global constraints

- **`SERVICES` gains exactly 0008's table, and nothing else:**
  - `todo`: `add_item`, `update_item` and `get_items`, given to-do lists only;
  - `weather`: `get_forecasts`, given weather entities only;
  - `notify`: only services named `mobile_app_*`, given no target;
  - `button`: `press`, only on a HASS.Agent button of a kind in `PC_KINDS`, or a Wake-on-LAN button, each checked against the registry snapshot.

  `get_items` and `get_forecasts` are asked for their answer. `todo.remove_item`, `todo.remove_completed_items` and every Proxmox button stay refused.
- **Choices are kept in the data folder** (`paths.DATA_DIR`), each through `ChoiceStore`, with its default, and work straight away:
  - `lists.json`, `{"main": None}` - None is the list called todo;
  - `weather.json`, `{"entity": None}` - only used with more than one weather entity;
  - `pcs.json`, `{"wake": {}}` - a PC's device id to its Wake-on-LAN button, only used with more than one of either.

  Nothing in `config.yaml` names an entity. 0009's page adds the controls later.
- **Lists:** the main list is the one called todo until another is chosen. Up to 10 things are read out, then how many more. Things are ticked off, never deleted. Several things at once go to Claude, and offline they're added as one item.
- **Devices** are the Home Assistant app's, found as Nova runs, and named and told apart by 0007's rules (`batteries.find`, `kind_of`, `is_phone`, `spoken_name`). "Ring my phone" always rings, wherever the user is. The phone, or no device named, keeps ntfy; the Mac and tablet go through their `notify.mobile_app_*`.
- **"Added", "Ringing" and "Sent"** are only said once Home Assistant has taken the call.
- **What "where's my phone?" says, asked locally, never reaches Claude** - not even in its history.
- **The PC** is found by its HASS.Agent device (plan 1's `hass_agent_buttons`), never by its name. It's on while its entities aren't unavailable. Shutting down and restarting ask "Shut down the PC?" first, and only a yes does it; sleep, screen off and lock don't ask. It's pressed with Nova's own `button.press`, never through Assist.
- **The weather** comes from Met, through Home Assistant. Open-Meteo goes, and so does `weather.location`, from `config.yaml` and the page. A forecast more than 3 hours old is said with its age. With Home Assistant down there's no weather, and Nova says it can't reach Home Assistant.
- **A removed setting is let through** with a warning saying where it's set now (0009 decision 2), in the same task that removes it - the brain's own `~/.config/nova/config.yaml` still sets `weather.location`.
- **The local commands stay strict (0003).** The new rules come before plan 1's Assist shapes, and each comes with tests for what it mustn't catch.
- **The repo is public.** No email address, hardware address, hostname, IP address, street address or token in code, tests, docs or commits. The Wake-on-LAN button's real entity id carries the PC's hardware address, so tests use made-up ids such as `button.wake_desk_pc`.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes root@home-assistant 'cd /tmp/nova-check/work && /tmp/nova-check-pytest/bin/ruff check .'`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Safety:** never push or deploy; touch no machine except through those two commands.

## Review focus

1. **Several things in one breath** - "eggs, milk and bread", or "eggs, milk, bread" with only commas - are never added as one item while Claude can be asked. (Task 4, `test_several_things_go_to_claude`.)
2. **A list or a thing that fits two** - "milk" with "milk" and "oat milk" on the list, or "uni" with lists "uni" and "uni-work" - asks which, rather than guessing. (Task 3, `test_two_lists_that_fit_are_asked_about` and `test_two_things_that_fit_are_asked_about`.)
3. **Home Assistant refusing a call** - a 500, or a timeout - is never followed by "Added", "Ringing" or "Sent". (Tasks 3, 5 and 6, `test_added_is_only_said_once_home_assistant_took_it`, `test_ringing_is_only_said_once_home_assistant_took_it` and `test_sent_is_only_said_once_home_assistant_took_it`.)
4. **A "yes" that isn't an answer to "Shut down the PC?"** - one after a new wake word, or Claude calling the press itself - presses nothing. (Task 7, `test_a_yes_in_a_new_conversation_presses_nothing` and `test_claude_cant_press_the_pcs_button_itself`.)
5. **Met leaving something out** - no `templow`, no hourly forecast, no chance of rain - gives a shorter sentence, never an error. (Task 2, `test_a_forecast_with_less_in_it_still_says_what_it_has`.)

## Live checks before Task 1

For the controller, read-only, with the user's OK, through Assist's sentence debugging and Home Assistant's REST API as plan 1's checks were. Put right any test fake a check proves wrong before its task.

1. **The list sentences Assist knows on 2026.10**, and their intent names: adding ("add milk to my shopping list", "put milk on my shopping list", "add milk to the todo list"), reading ("what's on my shopping list"), and ticking off ("tick off milk on my shopping list", "complete milk on my shopping list", "check off milk"). Also whether "take milk off my shopping list" or "remove milk from my shopping list" is read as removing an item - Task 4 never sends such a sentence. Expected: `HassListAddItem`, and maybe `HassListCompleteItem`.
2. **`todo.get_items` through `POST /api/services/todo/get_items?return_response`**: the answer is `{"service_response": {entity_id: {"items": [{"summary", "uid", "status"}]}}}`. And the four lists' friendly names and other names.
3. **Met's `weather.get_forecasts`**, daily and hourly: which fields each entry has (`datetime`, `condition`, `temperature`, `templow`, `precipitation`, and whether there's `precipitation_probability`), and how far ahead the hourly one goes. Also that `/api/states` gives `weather.forecast_home` a `last_reported`, and how often it changes.
4. **The Home Assistant app's devices:** that each one's notify service is `mobile_app_` and its device name, slugified (for today's three), that each has a `device_tracker`, and which attributes its `_geocoded_location` sensor has - the names only. No address is written down.
5. **The PC:** what HASS.Agent's entities read while the PC is off (expected `unavailable`), and that the Wake-on-LAN button's registry platform is `wake_on_lan`. Its entity id isn't written down.

---

### Task 1: One helper for choices kept in the data folder

**Files:**
- Create: `voice_assistant/choices.py`, with a module docstring saying what it's for (0009 decision 1).
- Modify: `voice_assistant/batteries.py` (`BatteryChoices`).
- Test: `tests/test_choices.py` (new); `tests/test_batteries.py` and `tests/test_brain.py`, which pass unchanged.

**Interfaces:**
- Produces: `ChoiceStore(path: str, defaults: Dict[str, Any])`, the one helper 0009 asks for.
  - It reads the file with `storage.load_json(path, {})`. One that can't be read is logged as a warning - "Couldn't read {path}, starting with the defaults: {e}" - and the defaults are used. A key the defaults haven't got is dropped, and a missing one gets a copy of its default.
  - `path: str` and `lock: threading.RLock`.
  - `state -> Dict`, a property: the live state, only read under `lock`, and never changed in place.
  - `get(key: str) -> Any`: a deep copy, taken under the lock.
  - `change(change: Callable[[Dict], object])`: under the lock, it applies the change to a deep copy, saves the copy with `storage.save_json` - called through the module, so a test can make it fail - and only then keeps it. It raises `OSError` or `TypeError`, with nothing changed.
  - `keep(change: Callable[[Dict], object], what: str)`: `change`, or, if the save fails with `OSError`, the change made to the state in memory alone, and logged - "Couldn't save the {what}: {e}".
- `BatteryChoices` keeps every method and signature, and its `_path` and `_defaults`, so its tests pass unchanged. It holds `ChoiceStore(path, {"devices": {}, "warned": {}, "held": {}, "also_send_when_said": True})`; its `_lock` is the store's lock, and its own `_change` and `_keep` go.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_new_store_starts_with_the_defaults`;
  - `test_a_file_that_cant_be_read_starts_with_the_defaults_and_says_so` (caplog);
  - `test_only_the_defaults_keys_are_kept`;
  - `test_a_change_is_saved_and_read_back_by_a_new_store`;
  - `test_a_change_that_cant_be_saved_changes_nothing` - `storage.save_json` made to raise `OSError`;
  - `test_keep_holds_a_change_in_memory_when_the_save_fails`;
  - `test_two_threads_changes_are_both_kept` - 50 changes each.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_choices.py`. Expected: they fail, with `ModuleNotFoundError: No module named 'voice_assistant.choices'`.
- [ ] **Step 3: Implement** `ChoiceStore`, then move `BatteryChoices` onto it.
- [ ] **Step 4: Run** `tests/test_choices.py tests/test_batteries.py tests/test_brain.py tests/test_settings_page.py` - expected: all pass, with the battery tests unchanged. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Keep choices about Home Assistant's things with one shared helper".

### Task 2: The weather from Met, through Home Assistant

**Files:**
- Modify: `voice_assistant/weather.py` (rewritten); `voice_assistant/home_assistant.py` (`SERVICES`, `_call_service`, and new `weather_entities` and `forecasts`); `voice_assistant/config.py` (`REMOVED`, `removed`, `check`, `Config.__init__`); `config.yaml` (the `weather` section goes); `voice_assistant/settings_page.py` (`EDITABLE`); `voice_assistant/brain.py` (`self.weather`); `voice_assistant/actions.py` (`UNAVAILABLE["weather"]`); `README.md` (the weather, and its settings table); `docs/troubleshooting.md` ("The weather doesn't work").
- Test: `tests/test_weather.py` (rewritten - the Open-Meteo tests go), `tests/test_config.py`, `tests/test_home_assistant.py`, `tests/test_settings_page.py`, `tests/test_brain.py`.

**Interfaces:**
- Consumes: `ChoiceStore` from Task 1.
- Produces:
  - `_call_service(domain, service, data, returns: bool = False) -> Optional[Dict]`. With `returns`, it posts to `/api/services/{domain}/{service}?return_response` and gives back the answer's `service_response`. Only `RETURNS = {("weather", "get_forecasts"), ("todo", "get_items")}` may be asked for one; anything else with `returns` is `NotAllowed`. `/api/services/` is still in the module once.
  - `SERVICES["weather"] = ("get_forecasts",)`.
  - `HomeAssistant.weather_entities() -> List[Dict]` - every `weather.*` state in `/api/states`, as `{"id", "name", "state", "attributes", "reported"}`, with `reported` the epoch seconds of `last_reported`, else `last_updated`.
  - `HomeAssistant.forecasts(entity_id: str, kind: str) -> List[Dict]` - the entity's `forecast` list from `weather.get_forecasts`, for `kind` "daily" or "hourly".
  - `Weather(home: HomeAssistant, choices: ChoiceStore)`, and `forecast(day: str = "today", now: Optional[datetime] = None) -> Dict`:
    - **the entity** is the only one there is. With several, it's `choices.get("entity")` if that's still there, else the first by entity id;
    - **now** is the entity's state as words - `CONDITIONS` maps Home Assistant's names: "partlycloudy" is "partly cloudy", "rainy" is "raining", "pouring" "pouring", "clear-night" "clear", "sunny" "sunny", "cloudy" "cloudy", "fog" "foggy", "snowy" "snowing", "snowy-rainy" "sleety", "lightning" and "lightning-rainy" "thundery", "hail" "hailing", "windy" and "windy-variant" "windy", and anything else nothing - and its `temperature` attribute;
    - **the day** is the daily entry whose `datetime` falls on that local day: `temperature` is the high and `templow` the low;
    - **rain** is the daily entry's `precipitation_probability` if Met gives one, said as today: "with a 40 percent chance of rain", or "and no rain expected". Without one, it's from that day's hourly entries still to come: hours with `precipitation` of 0.1 mm or more, or a rainy condition, give "with rain likely from about 3 PM until 6 PM" (the first such hour to the last) or "with rain likely around 3 PM" for one. None is "and no rain expected";
    - **the sentences** are today's: "It's 12 degrees and partly cloudy at the moment. Today, a high of 14 and a low of 8, with a 40 percent chance of rain." and "Tomorrow, cloudy, a high of 14 and a low of 8, and no rain expected." A part Met leaves out is left out of the sentence;
    - **an old forecast**, `reported` more than `OLD_FORECAST = 3 * 3600` seconds ago, adds " That's Home Assistant's forecast from 5 hours ago." (`format_duration`);
    - no weather entity: "Home Assistant hasn't got a weather forecast."; the entity unavailable: "Home Assistant's forecast isn't available right now."; Home Assistant down: `CANT_REACH`.
  - `Brain.weather` is `Weather(self.home, ChoiceStore(<data folder>/weather.json, {"entity": None}))`, or None without Home Assistant. `UNAVAILABLE["weather"] = "The weather needs Home Assistant."`.
  - In `config.py`:
    - `REMOVED: Dict[Tuple[str, str], str]` - each setting that has left `config.yaml`, and where it's set now. This task adds `("weather", "location"): "the weather is Home Assistant's own, for its home location"`;
    - `check()` lets a removed setting through, and a section whose settings are all removed ones;
    - `removed(settings: Dict) -> List[Tuple[str, str]]` - the removed settings an install's file has;
    - `Config.__init__` leaves them out, and unless `quiet`, logs a warning for each: "{path}: '{section}.{name}' is no longer used - {where} - so it can be deleted".
  - `EDITABLE` loses `weather.location`.

- [ ] **Step 1: Write the failing tests** with a `FakeHome` answering `weather_entities()` and `forecasts()`, in the shapes live check 3 found:
  - `test_todays_weather_is_said_as_before` - the first sentence above, from a daily `precipitation_probability` of 40;
  - `test_without_a_chance_of_rain_the_hours_say_when` - hourly `precipitation` at 3, 4, 5 and 6 PM gives "with rain likely from about 3 PM until 6 PM";
  - `test_tomorrows_weather_is_said_as_before`;
  - `test_no_rain_says_so`;
  - `test_an_old_forecast_says_how_old` - 5 hours;
  - `test_a_forecast_with_less_in_it_still_says_what_it_has` - no `templow`, no hourly entries, and no `precipitation_probability`;
  - `test_with_several_weather_entities_the_chosen_one_is_used` - and the first by id until one is chosen, or once the chosen one has gone;
  - `test_without_a_weather_entity_or_with_it_unavailable_it_says_so`, and `test_with_home_assistant_down_it_says_so`;
  - `test_the_forecast_is_asked_for_its_answer_through_the_allowlist` (test_home_assistant.py) - `?return_response`, the `service_response` given back, and `returns` refused for `light.turn_on`;
  - `test_a_setting_nova_no_longer_has_is_let_through_with_where_its_set_now` (test_config.py) - an install with `weather:\n  location: Leeds\n` starts; one warning names `'weather.location'`, says "Home Assistant" and "deleted"; and `section("weather") == {}`;
  - `test_the_page_can_save_with_a_removed_setting_still_in_the_file` (test_settings_page.py) - and the page no longer offers `weather.location`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_weather.py tests/test_config.py tests/test_home_assistant.py tests/test_settings_page.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say. The brain makes `self.home` before `self.weather`. Delete `config.yaml`'s weather section and the README's `weather.location` row, and say in the README that the weather is Home Assistant's, for its home location.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Read the weather from Met through Home Assistant, and let removed settings through with a warning".

### Task 3: Lists - found, matched and worked

**Files:**
- Create: `voice_assistant/lists.py`, with a module docstring giving 0008 decision 12's rules in brief.
- Modify: `voice_assistant/home_assistant.py` (`SERVICES`, and new `todo_lists`, `list_items`, `add_item` and `tick_off`); `voice_assistant/brain.py` (`self.lists`).
- Test: `tests/test_lists.py` (new), `tests/test_home_assistant.py`, `tests/test_brain.py`.

**Interfaces:**
- Consumes: `ChoiceStore` from Task 1, `_call_service(..., returns=True)` from Task 2, and plan 1's `Snapshot.aliases`.
- Produces:
  - `SERVICES["todo"] = ("add_item", "update_item", "get_items")`.
  - `HomeAssistant.todo_lists() -> List[Dict]` - every `todo.*` state, as `{"id", "name", "count", "aliases"}`. `count` is its state as a number of things still to do, or None; `aliases` come from the snapshot.
  - `HomeAssistant.list_items(list_id: str) -> List[Dict]` - `todo.get_items` with `{"entity_id": list_id, "status": ["needs_action"]}`, each `{"summary", "uid"}`.
  - `HomeAssistant.add_item(list_id: str, item: str)` - `todo.add_item` with `{"entity_id": list_id, "item": item}`.
  - `HomeAssistant.tick_off(list_id: str, uid: str)` - `todo.update_item` with `{"entity_id": list_id, "item": uid, "status": "completed"}`.
  - In `lists.py`:
    - `list_words(name: str) -> Tuple[str, ...]` - lower case; spaces, hyphens and underscores alike; "to do", "to-do" and "todo" all the one word "todo"; the words "list" and "lists", and a leading "my" or "the", dropped; and a last "s" taken off each word, so "notes" is ("note",);
    - `match_list(said: str, lists: List[Dict]) -> List[Dict]` - the lists whose name or one of whose other names has the same `list_words`. If none, the list whose name's words begin with the words said - ("note",) begins ("notepad",) - but only if just one list's do (0008: "if only one list's name does");
    - `match_item(said: str, items: List[Dict]) -> List[Dict]` - the things with the same words, ignoring case, punctuation, plurals and a leading "the", "some" or "my"; if none, those with every word said;
    - `say_items(name: str, items: List[Dict]) -> str` - "Your shopping list has milk, eggs and bread.", the first `MOST_ITEMS = 10` and then ", and 4 more", or "Your shopping list is empty.". A list is said as its name in lower case, with " list" after it unless it already ends in "list": "your notepad list", "your todo list";
    - `Lists(home: HomeAssistant, choices: ChoiceStore)`, with:
      - `main() -> Optional[Dict]` - the chosen main list if it's still there, else the one whose `list_words` are ("todo",), else the only list there is, else None;
      - `find(said: Optional[str]) -> Tuple[Optional[Dict], Optional[str]]` - with no list said, or only "list" or "my list", the main one. Otherwise `match_list`: one is the list; two or more give "Which list, uni or uni-work?"; none gives "I haven't got a list called that." The second is a sentence to say instead;
      - `add(items: List[str], said: Optional[str]) -> Dict` - `add_item` for each, then "Added milk to your shopping list." or "Added eggs, milk and bread to your shopping list.". A failure stops there: "Added eggs to your shopping list, but I couldn't add milk or bread.", or with none added, "I couldn't add that to your shopping list.";
      - `read(said: Optional[str]) -> Dict` - `say_items`;
      - `tick_off(item: str, said: Optional[str]) -> Dict` - with no list said, every list is looked through. "Ticked off milk on your shopping list."; two fit, "Which one, milk or oat milk?"; none, "I can't see milk on your shopping list." (or "on your lists");
      - every one says `CANT_REACH` with Home Assistant down. When no list fits and the words said didn't have "list" in them, the result also has `"not_understood": True`, so "add the dentist to my calendar" goes to Claude (Task 4). So does `tick_off` with no list said and nothing on any list fitting, so "I've got work tomorrow" goes to Claude too.
  - `Brain.lists: Optional[Lists]` - `Lists(self.home, ChoiceStore(<data folder>/lists.json, {"main": None}))`, or None without Home Assistant.

- [ ] **Step 1: Write the failing tests:**
  - `test_each_list_is_found_by_the_users_own_words` - with today's four lists, "Shopping list", "todo", "notepad" and "uni-work": "shopping" and "my shopping list" are Shopping list; "todo", "to do" and "to-do list" are todo; "notes", "my notes" and "notepad" are notepad; "uni work" and "uni-work" are uni-work;
  - `test_a_lists_other_name_finds_it` - the alias "groceries";
  - `test_two_lists_that_fit_are_asked_about` - lists "Shopping" and "Shopping list" both fit "shopping", which asks "Which list, Shopping or Shopping list?". "uni", with lists "uni" and "uni-work", is "uni"; and "note", with lists "notepad" and "notebook", fits neither, as the start of a name only counts for one list;
  - `test_a_list_that_isnt_there_says_so`;
  - `test_with_no_list_named_its_the_main_one` - todo, then the chosen one, then todo again once the chosen one has gone;
  - `test_added_is_only_said_once_home_assistant_took_it` - a 500 gives "I couldn't add that to your shopping list.", and a failure on the second of three gives the sentence above;
  - `test_a_list_is_read_up_to_ten_things` - twelve things give the first ten "and 2 more"; an empty list says so;
  - `test_a_thing_is_ticked_off_not_deleted` - `todo.update_item` with `"status": "completed"`, and `todo.remove_item` is `NotAllowed` (test_home_assistant.py);
  - `test_two_things_that_fit_are_asked_about` - "milk", with "oat milk" and "milk shake" on the list, asks which; with "milk" on it too, ticks that one off;
  - `test_words_naming_no_list_go_to_claude` - "my calendar" gives `not_understood`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_lists.py tests/test_home_assistant.py -k "list or thing or todo or added"`. Expected: they fail, with `ModuleNotFoundError: No module named 'voice_assistant.lists'`.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Find Home Assistant's to-do lists by the user's own words, and add to, read and tick off them".

### Task 4: Lists by voice

**Files:**
- Modify: `voice_assistant/intents.py` (three rules, and keeping the words as said); `voice_assistant/actions.py` (`add_to_list`, `read_list`, `tick_off_list` and `manage_list`; `UNAVAILABLE["lists"]`; `ActionExecutor(lists=...)`); `voice_assistant/llm.py` (`_parse_action`); `voice_assistant/session.py` (passing `lists=brain.lists`); `README.md` (lists).
- Test: `tests/test_intents.py`, `tests/test_actions.py`, `tests/test_llm.py`.

**Interfaces:**
- Consumes: `Lists` from Task 3, and plan 1's `command(request, wider=True, only=...)`.
- Produces:
  - Rules after the calendar rules, and before plan 1's Assist shapes. LISTWORDS is `(?:my |the )?[a-z0-9' -]+?`:
    - adding: `(?:add|put) (?P<item>.+?) (?:to|on|onto) (?P<list>LISTWORDS)` gives `Action("add_to_list", {"item": ..., "list": ..., "command": <the request>})`;
    - reading: `what(?:'s| is) on (?P<list>LISTWORDS)` and `read (?:me )?(?P<list>LISTWORDS list)` give `Action("read_list", {"list": ...})`;
    - ticking off: `tick (?:off )?(?P<item>.+?)(?: off)?(?: (?:on|from) (?P<list>LISTWORDS))?`, `(?:take|cross) (?P<item>.+?) off (?P<list>LISTWORDS)` and `i(?:'ve| have) got (?:the |some )?(?P<item>.+)` give `Action("tick_off_list", {"item": ..., "list": ... or None, "command": <the request>})`.
  - Each handler gives None, so later rules or Claude have it, when the list words name one of Nova's own things or a device - timer, alarm, reminder, calendar, schedule, diary, agenda, phone, tablet, mac, laptop or ipad - or the thing is only "it", "that" or "this". The adding one gives None too when the thing has "and" in it, or the words as said had a comma in them, as with "eggs, milk, bread": `LocalIntents.match` keeps the text as said for that. Claude splits them.
  - The tools, each `needs="lists"`:
    - `add_to_list(item, list, command)`, `for_model=False`: Assist first, with `self.home.command(command, wider=True, only=("HassListAddItem",))` (the intent live check 1 found). Done is Assist's own reply. `not_understood` - or no `command`, from the offline fallback - is `self.lists.add([item], list)`;
    - `read_list(list)`, `for_model=False`: `self.lists.read(list)`. It's local, never Assist;
    - `tick_off_list(item, list, command)`, `for_model=False`: Assist first, the same way, with `only=("HassListCompleteItem",)`, if live check 1 found that intent; otherwise straight to `self.lists.tick_off(item, list)`;
    - `manage_list(action, items, list)`, for Claude: `action` is "add", "read" or "tick_off", `items` a list of strings, and `list` optional. It's described: "Add things to, read, or tick things off one of the user's to-do lists in Home Assistant. Split several things into separate items, but keep one thing whole ('salt and vinegar crisps'). Leave list out for their main list. What happened is said for you, so just reply 'Okay.'"
  - Offline, `_parse_action` takes "add X to (my) Y (list)" as `Action("add_to_list", {"item": X, "list": Y})`, with X whole. So "eggs, milk and bread" is one thing, rather than being lost.
  - `UNAVAILABLE["lists"] = "Lists need Home Assistant."`.

- [ ] **Step 1: Write the failing tests:**
  - `test_one_thing_is_added_without_claude` - "Put milk on my shopping list." and "Add milk to notes." give `add_to_list`, with their item, list and command;
  - `test_several_things_go_to_claude` - "Add eggs, milk and bread to my shopping list.", "Add eggs, milk, bread to my list." and "Add salt and vinegar crisps to my list." are None;
  - `test_what_the_list_rules_mustnt_catch_is_left_alone` - "Add ten minutes to the timer.", "Add the dentist to my calendar.", "Put the kettle on.", "Put it on my phone." (still `send_to_phone`) and "What's on tomorrow?" (still the calendar);
  - `test_a_list_is_read_and_things_ticked_off_without_claude` - "What's on my shopping list?", "Read me my todo list.", "Tick off milk.", "Take milk off my shopping list." and "I've got the milk.";
  - `test_assist_adds_it_first_then_nova_does` - Assist done says Assist's reply; `not_understood` adds it with `todo.add_item`;
  - `test_a_sentence_assist_would_read_as_removing_is_never_sent` - the debugging says `HassListRemoveItem` for "take milk off my shopping list": it's ticked off by Nova, and Assist isn't asked to act;
  - `test_claude_adds_several_things_one_by_one` - `manage_list("add", ["eggs", "milk", "bread"], "shopping")` adds three and says "Added eggs, milk and bread to your shopping list.";
  - `test_offline_several_things_are_added_as_one` (test_llm.py) - the fallback gives `add_to_list` with "eggs milk and bread";
  - `test_lists_are_only_offered_with_home_assistant`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_intents.py tests/test_actions.py tests/test_llm.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Add to, read and tick off lists by voice, through Assist first".

### Task 5: Where's my phone, and ring it

**Files:**
- Create: `voice_assistant/devices.py`.
- Modify: `voice_assistant/home_assistant.py` (`APP_DEVICES_TEMPLATE`, `app_devices`, `notify`, and `SERVICES` and `_call_service` for `notify`); `voice_assistant/batteries.py` (`_is_mac` becomes `is_mac`, for its callers too); `voice_assistant/actions.py` (`ActionResult.private`, a `find_device` tool); `voice_assistant/conversation.py` (`handle`); `voice_assistant/intents.py` (three rules); `README.md`.
- Test: `tests/test_devices.py` (new), `tests/test_home_assistant.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_intents.py`.

**Interfaces:**
- Consumes: 0007's `find`, `kind_of`, `is_phone`, `is_mac` and `spoken_name` from `batteries.py`.
- Produces:
  - `HomeAssistant.app_devices() -> List[Dict]` - one template read of every device with an entity in `integration_entities('mobile_app')`. Each has `"device"`; `"name"` (the user's name for it, else its own); `"maker"`; `"model"`; `"notify"`, `"mobile_app_"` and the device's own name, slugified, kept only if `/api/services` has it; `"tracker"`, its `device_tracker`'s `{"state", "reported"}`, or None; and `"place"`, its `_geocoded_location` sensor's `{"state", "thoroughfare", "locality"}`, or None.
  - `HomeAssistant.notify(service: str, message: str, title: Optional[str] = None, data: Optional[Dict] = None)` - through `_call_service`. `SERVICES["notify"]` takes any service named `mobile_app_[a-z0-9_]+` and nothing else, and it's given no target.
  - In `devices.py`:
    - `@dataclass AppDevice`, with `id`, `name` (spoken), `kind`, `phone`, `computer`, `apple: bool`, `notify: Optional[str]`, `tracker: Optional[str]`, `reported: Optional[float]` and `place: Optional[str]`, and `AppDevice.from_home(found: Dict) -> AppDevice`. `batteries.find` finds one by what was said, as it finds a battery, so "my phone", "my tablet" and "my Mac" are the same device for both;
    - `where(device: AppDevice, now: float) -> str`: "Your phone's at home."; in another zone, "Your phone's at Work."; away with a place, "Your phone's near High Street, Leeds, as of 10 minutes ago." - the place is "thoroughfare, locality", else the first two parts of the sensor's state; away with none, "Your phone's away from home, as of 10 minutes ago."; with no tracker, or one that's unavailable, "I can't tell where your phone is.". A phone, tablet or Mac is said as that word, anything else by its name;
    - `ring_call(device: AppDevice) -> Dict` - what's sent to its notify service. Android: `{"message": "TTS", "data": {"tts_text": "Nova's looking for your phone.", "media_stream": "alarm_stream_max", "priority": "high", "ttl": 0}}`, said aloud on the alarm stream, so it's heard on silent. An iPhone or iPad: `{"message": "Nova's looking for your iPhone.", "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}`. A Mac: `{"message": "Nova's looking for your Mac.", "data": {"push": {"sound": "default"}}}`.
  - The tool `find_device(device, ring)`, `needs="home"`, described: "Say where one of the user's devices is - their phone, tablet or Mac - and ring it if asked ('find my phone', 'make my tablet ring'). What's found is said for you, so just reply 'Okay.'". It says where; with `ring` it also rings, wherever the device is: "Ringing your phone." once Home Assistant takes it, else "I couldn't ring your phone.". A device with no notify service: "Your Mac can't be rung - it hasn't got the Home Assistant app's notifications." None found: "I haven't got a device called that.". Its result has `"private": True`.
  - `ActionResult.private: bool`, read from a reply's `"private"`. When a turn was handled locally and a result is private, `Conversation.handle` gives Claude's history "I said where your phone is." in place of the words, in both `llm.remember` and `llm.record_reply`. Claude's own `find_device` call is kept as usual.
  - Local rules: `where(?:'s| is) (?:my |the )(?P<device>phone|tablet|mac|laptop)` gives `Action("find_device", {"device": ..., "ring": False})`; `(?:find|ring) (?:my |the )(?P<device>...)` gives it with `"ring": True`.

- [ ] **Step 1: Write the failing tests:**
  - `test_where_a_device_is_is_said_from_its_tracker` - home, a zone, away with a place and its age, away with no place, and no tracker;
  - `test_each_kind_of_device_is_rung_its_own_way` - the three `ring_call`s above;
  - `test_ringing_is_only_said_once_home_assistant_took_it` - and a 500 says it couldn't;
  - `test_ring_my_phone_rings_wherever_the_user_is` - away, it's still rung;
  - `test_a_devices_notify_service_is_found_by_its_name` - `mobile_app_s25_ultra` for the device "s25-ultra", and none when `/api/services` hasn't got it;
  - `test_only_the_apps_notify_services_are_called` - `notify.notify`, `notify.send_message`, and anything with a target, are `NotAllowed`;
  - `test_where_my_phone_is_never_reaches_claude_when_asked_locally` (test_conversation.py) - the history has "I said where your phone is.", never the place; Claude's own `find_device` keeps its words;
  - `test_the_phone_is_found_and_rung_without_claude` - "Where's my phone?", "Find my phone." and "Ring my tablet." (test_intents.py); "What's my phone on?" is still the battery.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_devices.py tests/test_home_assistant.py tests/test_actions.py tests/test_conversation.py tests/test_intents.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README: "where's my phone?" and "ring my phone", and the user's one step - on the phone, let the Home Assistant app ring through Do Not Disturb.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Say where the phone is and ring it, through the Home Assistant app".

### Task 6: Send that to my Mac, or the tablet

**Files:**
- Modify: `voice_assistant/actions.py` (`send_to_phone`; `tool` and `_available`); `voice_assistant/intents.py` (a rule); `README.md`.
- Test: `tests/test_actions.py`, `tests/test_intents.py`.

**Interfaces:**
- Consumes: `app_devices`, `notify` and `AppDevice` from Task 5, and `batteries.find`.
- Produces:
  - `tool(needs=...)` takes a tuple too, offered while any of them is set up; `UNAVAILABLE` is the first's.
  - `send_to_phone(text=None, title=None, device=None)`, `needs=("ntfy", "home")`. With no device, or one `find` says is the phone, it's ntfy, as now. Any other device goes through its notify service with `home.notify(service, text, title=title)`: "Sent." and the chime once Home Assistant takes it, else "I couldn't send that to your MacBook Pro.". A device without the service: "Your MacBook Pro hasn't got the Home Assistant app's notifications."; none found: "I haven't got a device called that."; without Home Assistant: "Sending to your Mac needs Home Assistant.". Its description gains: "Give device only when the user names one other than their phone, e.g. 'mac' or 'tablet'."
  - The local rule `(?:send|text|put) (?:that|it|this) (?:to|on) (?:my |the )(?P<device>mac|macbook|laptop|tablet|ipad)` gives `Action("send_to_phone", {"device": ...})`. The phone's own phrasings stay as they are.

- [ ] **Step 1: Write the failing tests:**
  - `test_send_that_to_my_mac_goes_through_its_notify_service` - with what was last said, under what was asked;
  - `test_the_phone_still_goes_through_ntfy`;
  - `test_sent_is_only_said_once_home_assistant_took_it`;
  - `test_a_device_with_no_notify_service_says_so`;
  - `test_sending_to_the_mac_and_tablet_is_done_without_claude` - "Send that to my Mac." and "Put it on the tablet.";
  - `test_send_to_phone_is_offered_with_ntfy_or_home_assistant`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_actions.py tests/test_intents.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Send things to the Mac or the tablet through Home Assistant".

### Task 7: The PC on and off

**Files:**
- Create: `voice_assistant/pc.py`.
- Modify: `voice_assistant/ha_registry.py` (`Snapshot.device_name`, `Snapshot.wake_buttons`); `voice_assistant/home_assistant.py` (`SERVICES`, `_call_service`'s check for buttons, and a new `press`); `voice_assistant/actions.py` (`ActionResult.ask_first`, and `pc_power` and `press_pc_button`); `voice_assistant/conversation.py` (`start`, `handle`); `voice_assistant/intents.py` (rules); `voice_assistant/brain.py` (`self.pc`); `voice_assistant/session.py`; `README.md`.
- Test: `tests/test_pc.py` (new), `tests/test_home_assistant.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_intents.py`.

**Interfaces:**
- Consumes: plan 1's `Snapshot.hass_agent_buttons`, `on_device` and `PC_KINDS`; `ChoiceStore` from Task 1; `Clock.answer_to_offer`.
- Produces:
  - `Snapshot.device_name(device_id) -> Optional[str]` and `Snapshot.wake_buttons() -> List[str]` - the buttons whose platform is `wake_on_lan`.
  - `SERVICES["button"] = ("press",)`. `_call_service` presses only an entity the snapshot shows is a HASS.Agent button of a kind in `PC_KINDS`, or a `wake_on_lan` button - never a `proxmoxve` one, and nothing at all without a snapshot. `HomeAssistant.press(entity_id: str)`.
  - In `pc.py`:
    - `@dataclass PC`, with `device: str`, `name: str` (spoken, from the device's name), `on: bool`, `buttons: Dict[str, str]` (kind to entity id) and `wake: Optional[str]`;
    - `find_pcs(states: List[Dict], snapshot: Snapshot, wake: Dict[str, str]) -> List[PC]` - one for each HASS.Agent device. It's `on` when any of its entities' states is other than `unavailable` (live check 5). Its Wake-on-LAN button is the only one, when there's one PC and one button; otherwise it's the one chosen for that device in `wake`, if that button's still there; otherwise None;
    - `PCPower(home: HomeAssistant, choices: ChoiceStore)`, with `act(action: str, name: Optional[str] = None) -> Dict` for action "on", "off", "restart", "sleep", "screen_off", "lock" or "status", and `press(action: str, name: Optional[str] = None) -> Dict`, which does "off" or "restart" once it's been agreed.
  - What `act` says:
    - "off" or "restart", with the PC on, asks first: `{"success": True, "message": "Shut down the PC?", "ask_first": Action("press_pc_button", {"action": "off", "name": name})}`, or "Restart the PC?". `press` then says "Shutting down the PC." or "Restarting the PC.". With the PC off, it's "Your PC's already off.";
    - "on" presses its Wake-on-LAN button and says "Turning your PC on.". It's "Your PC's already on." while it's on; "There's no Wake-on-LAN button for your PC in Home Assistant." without one; and "I don't know which Wake-on-LAN button wakes your PC yet." with several and none chosen;
    - "sleep" is "Putting your PC to sleep.", "screen_off" "Turning your PC's screen off." and "lock" "Locking your PC.", while it's on; otherwise "Your PC's off.";
    - "status" is "Your PC's on." or "Your PC's off.";
    - with no snapshot: "I can't check your PC's buttons just now."; no PC: "I can't find your PC in Home Assistant."; several, and none named that fits: "Which PC, Desk PC or Gaming PC?".
  - `ActionResult.ask_first: Optional[Action]`, read from a reply's `"ask_first"`. In `Conversation`:
    - a result that asks first keeps its question, which is said and listened for, as any question is;
    - the next thing said in the same conversation is read with `Clock.answer_to_offer`, before anything else. A yes runs the action, with `from_model=False`, and says what it says. A no says "Okay, I'll leave it.". Anything else drops it, and is taken as a fresh request;
    - `start()`, for a new wake word, drops it.
  - The tools: `pc_power(action)`, for Claude and the local commands, `needs="pc"`, described: "Turn the user's PC on, shut it down, restart it, put it or its screen to sleep, lock it, or say whether it's on. Shutting down and restarting ask the user first. What happens is said for you, so just reply 'Okay.'"; and `press_pc_button(action, name)`, `for_model=False`, so Claude can't skip the yes.
  - `UNAVAILABLE["pc"] = "Your PC needs Home Assistant."`, and `Brain.pc` is `PCPower(self.home, ChoiceStore(<data folder>/pcs.json, {"wake": {}}))`, or None without Home Assistant.
  - Local rules, before plan 1's Assist shapes, each giving `Action("pc_power", {"action": ...})`. PC is `(?:my |the )?(?:pc|computer)`, as `PC_LOCK` has it:
    - "on": `(?:turn|switch) on PC`, `(?:turn|switch) PC on` and `wake(?: up)? PC`;
    - "off": `(?:turn|switch) off PC`, `(?:turn|switch) PC off`, `shut down PC` and `shut PC down`;
    - "restart": `(?:restart|reboot) PC`;
    - "sleep": `put PC to sleep`;
    - "screen_off": `turn off PC(?:'s)? (?:screen|monitor)`;
    - "lock": `lock PC`;
    - "status": `is PC on`.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_pc_is_found_by_its_device_not_its_name` - buttons `button.desk_*` on a HASS.Agent device; on while a sensor reads a number, off while every entity is `unavailable`;
  - `test_shutting_down_asks_first_and_only_a_yes_does_it` (test_conversation.py) - "Shut down the PC?", then "yes" presses the shutdown button and says "Shutting down the PC."; "no" presses nothing;
  - `test_a_yes_in_a_new_conversation_presses_nothing`;
  - `test_claude_cant_press_the_pcs_button_itself` - `Action("press_pc_button", ..., from_model=True)` is refused;
  - `test_sleep_screen_off_and_lock_dont_ask`;
  - `test_turning_on_presses_wake_on_lan` - and "Your PC's already on." while it's on;
  - `test_with_several_wake_buttons_the_chosen_one_is_pressed` - and none until one's chosen;
  - `test_only_the_pcs_buttons_are_ever_pressed` (test_home_assistant.py) - a `proxmoxve` button, HASS.Agent's `launchurl` (kind "other"), a button on no HASS.Agent device, and any button without a snapshot are `NotAllowed`;
  - `test_the_pc_is_worked_without_claude` - each phrasing above gives its action; "Lock my PC." is `pc_power`, not Assist; "Is my PC on?" comes before the Assist shapes' "is X on";
  - `test_what_the_pc_rules_mustnt_catch_is_left_alone` - "Turn on the PC lights." (still the lights), "Turn off my phone." and "Restart the music." aren't `pc_power`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_pc.py tests/test_home_assistant.py tests/test_actions.py tests/test_conversation.py tests/test_intents.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README: the PC by voice, and what the user does - nothing, if HASS.Agent's device and Wake-on-LAN's button are in Home Assistant.
- [ ] **Step 4: Run** the changed files, then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff. Expected: all pass, and ruff is clean.
- [ ] **Step 5: Commit** - "Turn the PC on and off by voice, asking before shutting it down".
