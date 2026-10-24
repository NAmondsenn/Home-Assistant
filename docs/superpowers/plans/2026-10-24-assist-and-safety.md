# Assist and safety - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assist works far more than the lights - switches, media players, scripts, scenes and "is X on?" - behind a guard which refuses locks, doors, Proxmox and the PC's shutdown under any name. "Dim the lights" works without Claude, Claude is told the lights' other names, and Claude can read Home Assistant with `home_status`.

**Architecture:** A new `voice_assistant/ha_registry.py` talks to Home Assistant's WebSocket API with `websocket-client`. `HomeSocket` makes one short exchange. `HomeRegistry`, a thread on the brain, reads the entity, device and area registries, and what's exposed to Assist, every five minutes into a `Snapshot`. `HomeAssistant.command` uses it to refuse a guarded thing by any of its names, then asks Assist's own sentence debugging what a sentence would act on before sending it. Without both it fails shut, to today's light sentences. `intents.py` gains Assist's sentence shapes and dimming. Claude gains `run_home_script`, and `home_status`, whose answer goes back to Claude through a new read round in `llm.py`.

**Tech stack:** Python 3.13 on the brain, websocket-client (new, the brain only), Home Assistant's WebSocket API (registries, exposure, `conversation/agent/homeassistant/debug`) and REST API, pytest.

**Spec:** `docs/decisions/0008-home-assistant-first.md` - decisions 2, 3, 4, 6, 7 and 14, and the user's answer 1. Read it before starting any task. `2026-10-24-lists-devices-and-weather.md` and `2026-10-24-google-calendar.md` come after this plan and build on it.

## Global constraints

- **One new library, on the brain only:** `websocket-client`, pinned exactly in `requirements-backend.txt`, the brain's lock, which the weekly audit already reads. It's imported inside `HomeSocket.call`, never at the top of a module, so a brain without it fails shut and the satellite never needs it. `tests/conftest.py` stands in for it where it's missing, as it does for `anthropic`.
- **The allowlist doesn't grow in this plan.** `SERVICES` keeps `light`, `script` and `scene` as they are (decision 14), and every service still goes through `_call_service`.
- **What the guard refuses through Assist**, by what Home Assistant says each thing is, under its name, its entity id or any other name:
  - locks, valves and alarm panels, and covers of device class door, garage, gate or window (batch 1);
  - every entity of the Proxmox integration (registry platform `proxmoxve`);
  - HASS.Agent's buttons of any kind but sleep, monitor sleep and lock - so shutdown, restart, launching a web address and custom commands;
  - a script or scene that works any of those, or whose contents can't be read.
- **Fails shut.** If the registry can't be read, or Assist's sentence debugging doesn't answer, decision 2's wider rule is off: only today's light sentences go to Assist, until both work again.
- **Nova's own commands come first:** timers, alarms, reminders, the music, Nova's volume, notifications, the time, the weather and the calendar. Words that only point back - "it", "this", "that" - stay with Claude. Claude gets no tool of its own for switches, fans or media players.
- **Dimming:** a step of 30 points of brightness, never below 5%, and never off. A light that's off stays off when dimmed; brightening it switches it on at 30%.
- **The registry** is read every 300 seconds, and 30 seconds after a failed read.
- **The repo is public.** No email address, hardware address, hostname, IP address or token in code, tests, docs or commits. Test entity ids are made up.
- **Claude's tool descriptions stay short** - they're in every request's cached part.
- **The local commands stay strict (0003).** Each new shape comes with tests for what it mustn't catch.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. The baseline is 1246 passing. Ruff: `ssh -o BatchMode=yes root@home-assistant 'cd /tmp/nova-check/work && /tmp/nova-check-pytest/bin/ruff check .'`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Safety:** never push or deploy; touch no machine except through those two commands.

## Review focus

1. **One guarded thing among several targets** - "turn off everything", with one lock among the lights - refuses the whole sentence, and nothing goes to Assist. (Task 4, `test_one_guarded_target_among_many_refuses_the_lot`.)
2. **Nova's own things, said like a device** - "turn off the microphone" (Nova's own mute switch in Home Assistant), "turn off my alarm", "turn on shuffle" - never go to Assist. (Task 6, `test_what_the_assist_rule_mustnt_catch_is_left_alone`.)
3. **The registry or the sentence debugging coming and going:** the wider rule goes off and comes back by itself, each change logged once. (Task 4, `test_the_wider_rule_comes_back_once_both_answer`.)
4. **A script whose contents can't be read** - a 404, a token that isn't an administrator's, a YAML script with no id - is refused, never run. (Task 5, `test_a_script_that_cant_be_checked_is_refused`.)
5. **Dimming at the ends** - a light at 5%, one at 100%, one that only switches on and off, one unavailable - never switches a light off or does anything odd. (Task 2, `test_dimming_never_switches_a_light_off`.)

## Live checks before Task 1

For the controller, read-only, with the user's OK. `.superpowers/tools/ha_assist_debug.py` already shows how to sign in and ask the sentence debugging. The tests' fakes are shaped like the answers expected here, so put right any fake a check proves wrong before its task starts.

1. **websocket-client:** the newest release on PyPI that's at least two weeks old, and its wheel has no `Requires-Dist`. That's the version Task 1 pins.
2. **Assist's sentence debugging** (`conversation/agent/homeassistant/debug`, with `sentences`, `language` and `device_id`) on 2026.10.2: a matched sentence gives `result.results[0]` with `intent.name` and `targets` as `{entity_id: {"matched": true}}`, and an unmatched one gives `null`. Also what a sentence matched by an automation's own sentence trigger gives - its `source`, and whether `targets` is empty.
3. **That the debugging matches what `/api/conversation/process` would**, for a sentence only fuzzy matching understands. If it doesn't, Task 4's "matched nothing, so not sent" loses those sentences. That's safe, but worth knowing.
4. **The entity registry:** `config/entity_registry/list` has `entity_id`, `platform`, `device_id`, `area_id`, `unique_id` and `original_name`; the aliases come from `config/entity_registry/get_entries` (or the list has them too).
5. **Exposure:** `homeassistant/expose_entity/list` answers `{"exposed_entities": {entity_id: {"conversation": bool, ...}}}`, and goodnight isn't exposed.
6. **Devices and areas:** `config/device_registry/list` has `id`, `name`, `name_by_user`, `manufacturer`, `model` and `area_id`; `config/area_registry/list` has `area_id`, `name` and `aliases`.
7. **HASS.Agent's six buttons:** platform `mqtt`, one device, and which field carries each command's kind - expected to be `original_name`: "Shutdown", "Restart", "Sleep", "MonitorSleep", "Lock" and "LaunchUrl".
8. **Proxmox's platform** in the registry is `proxmoxve`.
9. **A script's configuration:** `GET /api/config/script/config/<key>` for goodnight - whether the key is the registry's `unique_id` or the object id, and its shape (`sequence`; each step's `action` or `service`, `target` and `data`).
10. **The wider shapes are Assist's:** through the debugging, "pause X", "next track on X", "set X volume to 40", "is X on", "run X", "activate X" and "close X" each match an intent, for something that's exposed.

**Results (24 October, read-only, from the brain):**
1. `websocket-client==1.9.2` (31 August 2026); its `requires_dist` is extras only, so no dependencies.
2. As expected: `result.results[i]` is `{"intent": {"name"}, "slots", "details", "targets": {entity_id: {"matched": true}}, "match", "sentence_template", "unmatched_slots", "source": "builtin"}`, and an unmatched sentence is `null`. No automation here has a sentence trigger, so that case wasn't seen.
3. Loose wordings match through the debugging too - "could you please turn the lamp off for me" is `HassTurnOff` on `light.lamp`.
4. `config/entity_registry/list` has no aliases (1,113 entries). `config/entity_registry/get_entries` gives each `aliases`, and a list can hold `None` - drop those.
5. As expected. 17 things are exposed (8 switches, 4 to-do lists, 3 media players, 2 lights), and no script - so "run goodnight" and "activate goodnight" match `HassTurnOn` with no targets.
6. As expected; areas also have `aliases`. Areas today: Bedroom, Home.
7. HASS.Agent's device has `manufacturer: "HASS.Agent Team"`. Its buttons' `original_name`s are "Shutdown", "Restart", "Sleep", "Monitor Sleep", "Lock" and "Launch HA" - so `button_kind` gives shutdown, restart, sleep, monitorsleep, lock and other. Nova's own MQTT device has a "Restart" and a "Stop" button too.
8. `proxmoxve`, 332 entities.
9. The key is the `unique_id`, which is the object id ("goodnight"). The answer is `{"alias", "icon", "mode", "sequence"}`; steps use `action` and `target.entity_id` (a list), and nest under `if`/`then`. Goodnight presses `button.windows_pc_sleep`, a safe kind, so it must stay runnable.
10. Each wider shape matches its intent with its target: `HassMediaPause`, `HassMediaNext`, `HassSetVolume`, `HassGetState` and `HassTurnOff`.

**The controller's rulings from these:**
- A HASS.Agent device is one whose `manufacturer` starts "HASS.Agent" - set by HASS.Agent, not the user - rather than "an `mqtt` device with a shutdown button" (Task 1).
- A match with no targets is never sent on the wider rule, whatever its intent (Task 4). It's how a script that isn't exposed matches, and how "add milk to notes" matches when no list is called notes - which plan 2 then adds to notepad itself.

---

### Task 1: Read the registry over the WebSocket

**Files:**
- Create: `voice_assistant/ha_registry.py`, with a module docstring saying what it reads and why (0008's answer 1).
- Modify: `requirements-backend.txt` (the pinned line, and its header); `tests/conftest.py` (a stand-in for `websocket`); `voice_assistant/home_assistant.py` (`HomeAssistant.__init__`, `from_config`); `voice_assistant/brain.py` (`__init__`, `run`, `_clean_up`); `voice_assistant/session.py:172`.
- Test: `tests/test_ha_registry.py` (new), `tests/test_brain.py`.

**Interfaces:**
- Produces:
  - `class HomeSocketError(Exception)`.
  - `HomeSocket(url: str, token: str, timeout: float = 3.0)`, and `HomeSocket.call(messages: List[Dict]) -> List[Any]`. One connection to `ws(s)://.../api/websocket`: it signs in, sends each message with ids 1, 2 and so on, and gives back each answer's `result`, in order. It always closes. It raises `HomeSocketError` for no network (checked with `network.online()`, without trying), a refused sign-in, an answer with `success: false`, a timeout, or the library missing or failing.
  - `@dataclass(frozen=True) Entry`: `entity_id: str`, `platform: str`, `device_id: Optional[str]`, `area_id: Optional[str]` (its own, else its device's), `unique_id: str`, `original_name: Optional[str]`, `aliases: Tuple[str, ...]` and `exposed: bool` (to Assist).
  - `@dataclass(frozen=True) Snapshot`: `entries: Dict[str, Entry]`, `devices: Dict[str, Dict]` (id to `name` - the user's name for it if they gave one - `maker`, `model` and `area_id`), `areas: Dict[str, str]` (id to name) and `read_at: float`. Its methods:
    - `platform(entity_id) -> Optional[str]`, `aliases(entity_id) -> Tuple[str, ...]`, `exposed(entity_id) -> bool` (False if it isn't known) and `area_name(entity_id) -> Optional[str]`;
    - `on_device(device_id) -> List[str]`, the device's entity ids;
    - `hass_agent_buttons() -> Dict[str, Tuple[str, str]]`, each HASS.Agent button's entity id to its device id and kind. A HASS.Agent device is one whose `manufacturer` starts "HASS.Agent" ("HASS.Agent Team" today) - never one found by the `windows_pc_` prefix, which the user can rename. Nova's own MQTT device, with its "Restart" button, isn't one.
  - `button_kind(entry: Entry) -> str`: "shutdown", "restart", "sleep", "monitorsleep", "lock" or "other". It's read from `original_name`, lower-cased with everything but letters taken out ("MonitorSleep" is "monitorsleep", "Shut down" is "shutdown", "reboot" counts as "restart"), else from the last `_` part of `unique_id`, else it's "other". Also `PC_KINDS = ("shutdown", "restart", "sleep", "monitorsleep", "lock")` and `PC_SAFE = ("sleep", "monitorsleep", "lock")`.
  - `HomeRegistry(socket: HomeSocket, every: float = 300.0, retry: float = 30.0)`:
    - `read() -> Snapshot` - one `call` with `config/entity_registry/list`, `homeassistant/expose_entity/list`, `config/device_registry/list` and `config/area_registry/list`, then a second with `config/entity_registry/get_entries` for every entity id, for the aliases. A `None` in an entry's `aliases` is dropped. It raises `HomeSocketError`, or `ValueError`, `KeyError` or `TypeError` for an answer of the wrong shape;
    - `refresh() -> bool` - one pass: read and keep the snapshot, or drop it on any failure. A failure is logged once until a read works again, and that's logged too;
    - `snapshot() -> Optional[Snapshot]` - None until a read works, and while the latest one is failing;
    - `start()` and `stop()` - a daemon thread named `ha-registry`, calling `refresh()` straight away, then every `every` seconds, or `retry` seconds after a failure.
  - `HomeAssistant.__init__(..., registry: Optional[HomeRegistry] = None)` and `HomeAssistant.from_config(config, token, registry=None)`; `HomeAssistant.socket: HomeSocket`, its own, for Task 4; and `HomeAssistant._snapshot() -> Optional[Snapshot]`.
  - `Brain.registry: Optional[HomeRegistry]` - made from `self.home.socket` when there's Home Assistant, given to `self.home` and to every session's `HomeAssistant`, started in `run()` and stopped in `_clean_up`.

- [ ] **Step 1: Write the failing tests.** Use a fake connection in place of `websocket.create_connection` that answers `auth_required`, then each message by its `type`; and a `FakeSocket` with `call(messages)` for the registry.
  - `test_a_call_signs_in_then_asks_each_thing_in_turn` - it sends `{"type": "auth", "access_token": "token"}`, then messages with ids 1 and 2; it returns the two results in order, and closes;
  - `test_a_refused_sign_in_or_a_failed_answer_raises` - `auth_invalid`, and `{"success": false}`, each raise `HomeSocketError`, and close;
  - `test_the_address_is_home_assistants_own_websocket` - `http://homeassistant.local:8123/` gives `ws://homeassistant.local:8123/api/websocket`, and `https://` gives `wss://`;
  - `test_with_no_network_nothing_is_tried`;
  - `test_the_registry_is_read_into_a_snapshot` - `light.bedroom_light` has aliases `("main light",)`, is exposed, and is in "Bedroom" through its device's area; `lock.front_door` has its platform;
  - `test_hass_agents_buttons_are_found_by_their_device_and_kind` - a device with `manufacturer` "HASS.Agent Team" whose buttons' `original_name`s are "Shutdown", "Restart", "Sleep", "Monitor Sleep", "Lock" and "Launch HA" gives kinds shutdown, restart, sleep, monitorsleep, lock and other, though their entity ids are `button.desk_*`. Nova's own device (manufacturer "Nova") with a "Restart" button isn't one;
  - `test_a_none_alias_is_dropped` - `get_entries` giving `aliases: [None, "main light"]` gives `("main light",)`;
  - `test_no_snapshot_until_a_read_works_and_none_while_reads_fail` - with caplog, two failures in a row log once, and the read that works again logs that it does;
  - `test_the_brain_reads_the_registry_when_home_assistant_is_set_up` - and it's None without Home Assistant.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_ha_registry.py`. Expected: they fail, with `ModuleNotFoundError: No module named 'voice_assistant.ha_registry'`.
- [ ] **Step 3: Implement.** Pin `websocket-client==1.9.2` (live check 1) in `requirements-backend.txt`, in its alphabetical place, and add Home Assistant's WebSocket API to the header's list of what the brain needs. The conftest stand-in has `create_connection = None`, `WebSocketException` and `WebSocketTimeoutException`; tests set `create_connection` with monkeypatch.
- [ ] **Step 4: Run them again** - expected: they pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Read Home Assistant's registry over its WebSocket API".

### Task 2: The lights by their other names, and dimming without Claude

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`lights`, `_light`, `_light_said`, and new `lights_called` and `dim`); `voice_assistant/intents.py` (the light rule, and a dimming rule); `voice_assistant/actions.py` (a `dim_lights` tool).
- Test: `tests/test_home_assistant.py`, `tests/test_intents.py`, `tests/test_actions.py`.

**Interfaces:**
- Consumes: `HomeAssistant._snapshot()` and `Snapshot.aliases` from Task 1.
- Produces:
  - Each light from `lights()` gains `"aliases": List[str]`, from the snapshot, or `[]` without one. `describe_lights` says them: "Bedroom Light, also main light (light.bedroom_light, Bedroom) on at 40%". Two or more are joined with "or".
  - `HomeAssistant.lights_called(words: str, lights: List[Dict]) -> Optional[List[Dict]]` - the lights some words mean, after a leading "all", "the" or "my" is taken off:
    1. "light", "lights", "lamp" or "lamps" alone is the lights in Nova's room. The caller asks "Which room?" when Nova's room isn't known;
    2. else a light whose name or other name equals the words, ignoring case and punctuation - "bedroom light" is Bedroom Light, "main light" the light that's also called that, "lamp" is Lamp;
    3. else an area whose name equals the words once a last word of light, lights, lamp or lamps is taken off - "bedroom lights" are the Bedroom's;
    4. else None.
  - `HomeAssistant.dim(direction: str, words: str) -> Dict`, with direction "down" or "up". It reads the lights once, with the template it already uses, finds them with `lights_called`, and calls `light.turn_on` with each one's new `brightness_pct` - one call for each new brightness, with every light getting it. `DIM_STEP = 30` and `DIM_FLOOR = 5`:
    - down: `max(DIM_FLOOR, now - DIM_STEP)`, for a light that's on;
    - up: `min(100, now + DIM_STEP)`, and a light that's off goes on at `DIM_STEP`;
    - a light that's off when dimmed, one that's unavailable, and one that only switches on and off are left as they are;
    - with nothing to change, it says "They're as dim as they go.", "They're as bright as they go." or, if they only switch, "Those lights only switch on and off.";
    - a success is chimed. Words it can't place give `{"success": False, "message": "I don't know a light called that.", "not_understood": True}`, so Claude is asked, as now.
  - The tool `dim_lights(direction, words)`, with `for_model=False` and `needs="home"`.
  - The local rule `(?P<direction>dim|brighten) {LIGHT}` gives `Action("dim_lights", {"direction": "down" or "up", "words": <the LIGHT words>})`. "Dim" and "brighten" leave today's Assist rule; "set the lamp to 30 percent" stays with it.

- [ ] **Step 1: Write the failing tests:**
  - `test_lights_come_with_their_other_names` - a snapshot alias "main light" for `light.bedroom_light` gives `["main light"]`; with no registry, `[]`;
  - `test_claude_is_told_the_lights_other_names` - the `describe_lights` line above;
  - `test_lights_are_found_by_name_other_name_or_room` - with Nova in the Bedroom: "the lights" are the Bedroom's two, "main light" and "bedroom light" are `light.bedroom_light`, "lamp" is `light.lamp`, "kitchen lights" are the Kitchen's, and "attic lights" is None;
  - `test_dimming_steps_each_light_down_from_where_it_is` - the lamp at 80% and the bedroom light at 20% get `{"entity_id": ["light.lamp"], "brightness_pct": 50}` and `{"entity_id": ["light.bedroom_light"], "brightness_pct": 5}`, and a chime;
  - `test_dimming_never_switches_a_light_off` - a light at 5% gets no call, a light that's off isn't touched, an on/off-only light and an unavailable one are left alone, and `light.turn_off` is never called. With every light at 5%, it's "They're as dim as they go.";
  - `test_brightening_switches_an_off_light_on_at_one_step` - off gives 30, and 90 gives 100;
  - `test_dimming_a_light_nova_doesnt_know_goes_to_claude` - `not_understood` is True;
  - `test_dimming_with_no_room_known_asks_which` - "Which room?";
  - `test_dimming_is_done_without_claude` - "Dim the lights." gives `Action("dim_lights", {"direction": "down", "words": "the lights"})`; "Brighten the lamp." gives "up" and "the lamp"; and "Dim the bedroom lights." and "Dim the main light." give their own words. "Set the lamp to 30 percent." is still `control_home`;
  - `test_dim_lights_isnt_offered_to_claude`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py tests/test_intents.py tests/test_actions.py -k "dim or other_names or found_by_name or brighten"`. Expected: they fail, with `AttributeError` for `dim` and `lights_called`, and a missing `aliases` key.
- [ ] **Step 3: Implement** as the Interfaces say. `_light(state, aliases=())` takes the aliases, so the settings page's lights read the same way.
- [ ] **Step 4: Run them again** - expected: pass. Then the three files whole.
- [ ] **Step 5: Commit** - "Dim the lights without Claude, and tell Claude the lights' other names".

### Task 3: The guard knows every name, Proxmox and the PC's buttons

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`_guarded`, `_names_guarded`, `command`, a new `switch_lights`, and the module docstring); `voice_assistant/intents.py` (the light rule's handler); `voice_assistant/actions.py` (`control_home`).
- Test: `tests/test_home_assistant.py`, `tests/test_intents.py`, `tests/test_actions.py`.

**Interfaces:**
- Consumes: `Snapshot.platform`, `aliases` and `hass_agent_buttons`, and `PC_SAFE`, from Task 1; `lights_called` from Task 2.
- Produces:
  - `_guarded(entity_id: str, attributes: Dict, snapshot: Optional[Snapshot]) -> Optional[Sequence[str]]` - the words Assist takes as all of a kind (as now), `()` for a guarded thing with no kind words, or None for anything that may be worked by voice. It adds, from the snapshot, platform `proxmoxve`, and a HASS.Agent button whose kind isn't in `PC_SAFE`.
  - `_names_guarded(request: str, states: List[Dict], snapshot: Optional[Snapshot]) -> bool` - True if any guarded thing's name, entity-id words or other name, or a kind's word, is in the request as whole words.
    - Batch 1's exemption, where a harmless name holding a guarded one let it through, goes, and with it `_found_outside`. Assist reads a room before a name, so "bedroom front door" can be the lock "front door" in the bedroom.
    - What that refuses that batch 1 let through is a light sentence, and those are now done by Nova's own lights (below).
  - `command(request)` gives `{"success": False, "message": REFUSED, "refused": True}` when it refuses, with `REFUSED = "I don't lock, unlock or open anything by voice."`.
  - `HomeAssistant.switch_lights(on: bool, words: str, brightness: Optional[int] = None) -> Dict` - finds the lights with `lights_called`, then calls `light.turn_on`, with `brightness_pct` if given, or `light.turn_off`, by entity id. It's chimed. Words it can't place are `not_understood`, as `dim` has it; with no room known it asks "Which room?".
  - The light rule gives `Action("control_home", {"command": <the request>, "on": bool, "words": <the LIGHT words>, "brightness": Optional[int]})` - one rule for each of its shapes, so each can name its groups. "set X to N percent" is `on=True` with the brightness.
  - `control_home(command, on=None, words=None, brightness=None)`: when `command()` refuses and `on` is given, it does `switch_lights(on, words, brightness)` instead. So a light sentence the guard refuses goes to Nova's own lights - lights only - rather than being refused (the controller's ruling of 24 October).

- [ ] **Step 1: Write the failing tests** with `make_home`, `_thing` and a snapshot for the registry. Each refusal asks for `/api/states` and nothing else:
  - `test_a_harmless_thing_holding_a_locks_name_doesnt_hide_it` - the lock "Front door" and a switch "Bedroom front door": "turn off the bedroom front door" is refused;
  - `test_a_lock_called_door_is_refused_beside_a_sensor_called_back_door` - "turn off the back door" is refused;
  - `test_a_guarded_thing_is_refused_by_its_other_name` - `valve.water_main`, "Water main", with the alias "garden tap" in the snapshot: "turn on the garden tap" is refused, and without the snapshot it isn't, so it's the alias that did it;
  - `test_proxmox_is_refused_whatever_its_called` - `button.bedroom_pve1_shut_down`, named "PVE2 Shut down", platform `proxmoxve`: "turn on pve2 shut down" is refused. So is "turn off navidrome", for a `proxmoxve` switch named "Navidrome";
  - `test_the_pcs_shutdown_restart_and_web_address_buttons_are_refused` - a HASS.Agent device "Desk PC": "turn on desk pc shutdown", "... restart" and "... launchurl" are refused, and "turn on desk pc sleep" goes to Assist;
  - `test_a_refused_light_sentence_is_done_by_novas_own_lights` - the lock "Front door", and `light.porch` "Porch light" with the alias "front door light": `control_home("turn on the front door light", on=True, words="the front door light")` calls `light.turn_on` with `{"entity_id": ["light.porch"]}`, never Assist, and chimes;
  - `test_a_refused_light_sentence_naming_no_light_goes_to_claude` - `understood` is False;
  - `test_the_lights_go_straight_to_home_assistant` (in test_intents.py) - its cases gain `on`, `words` and `brightness`, e.g. "Set the bedroom lamp to 30 percent." has `on: True`, `words: "the bedroom lamp"`, `brightness: 30`.

  `test_a_light_whose_name_holds_a_locks_is_that_light` changes to match: `command()` refuses, and `control_home` turns the light on with `light.turn_on`. The rest of `test_a_harmless_name_never_hides_a_guarded_one` stands as it is.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py tests/test_intents.py tests/test_actions.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say. The module docstring says what's guarded now, and why a light sentence goes to Nova's own lights when it's refused.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Refuse a guarded thing by any of its names, and Proxmox and the PC's shutdown through Assist".

### Task 4: Ask Assist what a sentence would act on, and fail shut

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`command`, and a new `_assist_targets`); `voice_assistant/actions.py` (`control_home`).
- Test: `tests/test_home_assistant.py`, `tests/test_actions.py`.

**Interfaces:**
- Consumes: `HomeAssistant.socket` and `_snapshot()` from Task 1, and `_guarded` and `_names_guarded` from Task 3.
- Produces:
  - `AssistMatch = NamedTuple("AssistMatch", [("matched", bool), ("intent", Optional[str]), ("targets", List[str])])`.
  - `HomeAssistant._assist_targets(request: str) -> Optional[AssistMatch]` - Assist's own sentence debugging, `{"type": "conversation/agent/homeassistant/debug", "sentences": [request], "language": "en"}`, with `device_id` when Nova's device is known. It gives `AssistMatch(False, None, [])` when Assist matches nothing, `AssistMatch(True, <intent name>, <the matched entity ids>)` when it matches, and None when it doesn't answer (`HomeSocketError`, or an answer of the wrong shape).
  - `command(request: str, wider: bool = False, only: Optional[Sequence[str]] = None) -> Dict`, in this order:
    1. the words in `SENSITIVE`: refused, as now;
    2. `/api/states`, read afresh, and the snapshot;
    3. if `wider` and there's no snapshot: `{"success": False, "message": "Home Assistant didn't understand that.", "not_understood": True}`, with nothing sent - the wider rule is off;
    4. `_names_guarded`: refused;
    5. `_assist_targets`. None: a wider request is `not_understood`, as in 3, and a light sentence carries on, with today's checks. Not matched: `not_understood`, without asking Assist to act. Matched with an intent not in `only`, when `only` is given: `not_understood` too - plan 2's lists use it, so a sentence Assist would read as removing an item is never sent. Matched with no targets on the wider rule, whatever the intent: `not_understood`, since the guard can't see what it would act on - as with a script that isn't exposed ("run goodnight"), an automation's own sentence trigger, or "add milk to notes" with no list called notes (the controller's rulings of 24 October: it fails shut). Any target `_guarded`: refused;
    6. `/api/conversation/process`, as now.
  - "The wider rule is off" is logged once when it goes off - for the registry, or for the debugging - and once when it's back on.
  - `control_home(command, wider=False, on=None, words=None, brightness=None)` passes `wider` on.

- [ ] **Step 1: Write the failing tests** with a `FakeSocket` on `home.socket` that answers the debugging, and a fake registry whose `snapshot()` the test sets. `make_home` now gives every home a `FakeSocket` that raises `HomeSocketError` unless the test gives it answers - so no test reaches a network, and the existing light tests carry on as today.
  - `test_assist_is_asked_what_a_sentence_would_act_on_before_acting` - targets `{"switch.tv": {"matched": true}}`; the debugging is asked first, with the device id, then Assist acts;
  - `test_a_lock_exposed_as_front_door_is_never_reached_either_way` - `lock.main`, "Main lock", exposed with the alias "front door". "Turn off the front door", with `wider=True`, fails and `/api/conversation/process` is never asked: with the registry and the debugging both answering, with no snapshot, and with the socket raising (0008 decision 14: "There's a test for each");
  - `test_a_guarded_target_assist_would_act_on_is_refused` - the snapshot hasn't got the lock's new alias "the shed" yet, but the debugging's target is `lock.main`: "turn off the shed" is refused;
  - `test_one_guarded_target_among_many_refuses_the_lot` - targets `light.lamp`, `light.bedroom_light` and `lock.main`;
  - `test_a_sentence_assist_matches_nothing_in_isnt_sent` - `null` gives `not_understood`, and Assist isn't asked to act;
  - `test_only_the_intents_asked_for_are_sent` - with `only=("HassListAddItem",)`, a sentence the debugging reads as `HassListRemoveItem` is `not_understood`, and Assist isn't asked to act;
  - `test_a_match_with_no_targets_isnt_sent_on_the_wider_rule` - the debugging matches "run goodnight" as `HassTurnOn` with no targets, as it does for a script that isn't exposed: with `wider=True` it's `not_understood`, and Assist isn't asked to act. So is `HassListAddItem` with no targets, even with `only=("HassListAddItem",)`;
  - `test_the_wider_rule_is_off_without_the_registry` - no snapshot: a wider request is `not_understood` with nothing sent, and "turn on the lamp" still goes to Assist;
  - `test_the_wider_rule_is_off_when_assist_cant_be_asked_what_it_would_do` - the socket raises: the same;
  - `test_the_wider_rule_comes_back_once_both_answer` - off, then on again, with one log line each way (caplog).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py -k "assist or wider or reached or guarded_target"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Ask Assist what a sentence would act on before it acts, and fail shut without it".

### Task 5: Scripts and scenes - check what they do, and run them by voice

**Files:**
- Modify: `voice_assistant/home_assistant.py` (new `works_guarded` and `run_script`; `command`; `run`); `voice_assistant/actions.py` (a `run_home_script` tool).
- Test: `tests/test_home_assistant.py`, `tests/test_actions.py`.

**Interfaces:**
- Consumes: `Snapshot.exposed`, `aliases` and `on_device` from Task 1, `_guarded` from Task 3, and `_assist_targets` from Task 4.
- Produces:
  - `HomeAssistant.works_guarded(entity_id: str, states: List[Dict], snapshot: Optional[Snapshot]) -> bool` - True if a script or scene works something `_guarded`, or can't be checked:
    - a scene, by its members - its state's `entity_id` attribute;
    - a script, by its configuration from `GET /api/config/script/config/<key>`, the key as live check 9 found. Its configuration is walked whole:
      - any string that's the entity id of something guarded;
      - an `action` or `service` in the `lock`, `valve` or `alarm_control_panel` domain, or a device action whose `domain` is one of those;
      - a `device_id` whose device has something guarded (`on_device`);
      - an `area_id`, `floor_id` or `label_id` target of a `homeassistant.*` or `cover.*` action, which could reach a guarded thing in it. `light.turn_off` for an area is fine;
      - a script or scene it runs, by entity id or through `script.turn_on` or `scene.turn_on`, checked the same way, each once;
      - a template (a string with `{{`) is left alone - it's the user's own, and they exposed it (0008, "My calls");
    - a configuration that can't be read (any HTTP error, as with a token that isn't an administrator's, or an answer that isn't JSON) is True.
  - `command()` also refuses when any target from `_assist_targets` works something guarded, and so does a script or scene named in the request by its name or other name. The second covers a light sentence while the debugging isn't answering.
  - `HomeAssistant.run_script(name: str) -> Dict` - for Claude:
    - with no snapshot: "I can't tell which scripts can be run by voice just now.";
    - it matches the name against every script's and scene's friendly name and other names, ignoring case, punctuation, a leading "my" or "the", and a last word "script", "scene" or "routine". Several: "Which one, Goodnight or Good morning?". None: "I haven't got a script or scene called that.";
    - not exposed to Assist: "Goodnight isn't exposed to Assist, so I don't run it by voice.";
    - `works_guarded`: `REFUSED`;
    - otherwise `script.turn_on` or `scene.turn_on`, through `_call_service`, saying "Started Goodnight." or "Set Movie night.", as `run()` does.
  - `run()`, for the settings page, refuses what `works_guarded` refuses too, with its own message as now.
  - The tool `run_home_script(name)`, `needs="home"`, with the description "Run one of the user's Home Assistant scripts or scenes by name, for wordings like 'do my goodnight thing'. Only those exposed to Assist run. What happened is said for you, so just reply 'Okay.'".

- [ ] **Step 1: Write the failing tests** with configurations as live check 9 found them:
  - `test_a_scene_with_a_lock_among_its_members_is_refused`;
  - `test_a_script_that_unlocks_is_refused` - a step `{"action": "lock.unlock", "target": {"entity_id": "lock.front_door"}}`;
  - `test_a_script_that_runs_one_that_unlocks_is_refused` - and a script that runs itself is checked once, without looping;
  - `test_a_script_with_a_locks_device_action_is_refused`;
  - `test_an_area_target_is_refused_only_for_services_that_could_reach_a_lock` - `homeassistant.turn_off` for an area is refused, and `light.turn_off` for the same area isn't;
  - `test_a_templated_target_is_left_to_the_user`;
  - `test_a_script_that_cant_be_checked_is_refused` - a 404, a 403, and an answer that isn't JSON;
  - `test_a_script_that_only_works_the_lights_runs_by_voice` - `script.turn_on` is called, and it says "Started Goodnight.";
  - `test_a_script_not_exposed_to_assist_isnt_run_by_voice`;
  - `test_without_the_registry_no_script_is_run_by_voice`;
  - `test_two_scripts_that_fit_are_asked_about`;
  - `test_assist_never_runs_a_script_that_unlocks` - the debugging's target is `script.evening`, which unlocks: `command("run evening", wider=True)` is refused;
  - `test_a_light_sentence_naming_a_script_that_unlocks_is_refused_without_the_debugging` - the socket raises, and "turn on the porch light", where `script.porch_light` unlocks, is refused;
  - `test_the_settings_page_wont_run_a_script_that_unlocks_either`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py tests/test_actions.py -k "script or scene"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say. The walk is one recursive function, with the set of scripts already checked passed down.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Check what a script or scene does before running it, and let Claude run the exposed ones".

### Task 6: Assist for more than the lights

**Files:**
- Modify: `voice_assistant/intents.py` (the Assist shapes, last in `_rules`); `voice_assistant/routines.py` (the docstring); `README.md` (the Home Assistant section: what Assist does now, and what the user exposes); `docs/troubleshooting.md` (a part on something Assist won't work).
- Test: `tests/test_intents.py`, `tests/test_conversation.py`.

**Interfaces:**
- Consumes: `control_home(command, wider=True)` from Task 4.
- Produces:
  - Rules after every one of Nova's own, so Nova's commands are matched first. Each gives `Action("control_home", {"command": <the request>, "wider": True})`. THING is one to four words, none of them "and", after an optional "the", "my" or "all the":
    - `(?:turn|switch) (?:on|off) THING` and `(?:turn|switch) THING (?:on|off)`;
    - `(?:pause|resume|unpause) THING`;
    - `(?:next|previous) (?:track|song) on THING`;
    - `set (?:the )?THING volume to \d{1,3}(?: percent)?`;
    - `is THING (?:on|off|open|closed|playing|paused)`;
    - `(?:activate|run) THING`;
    - `(?:open|close) THING`.
  - The handler gives None, leaving the request to Claude, when a word of THING is in `NOVAS_OWN` - timer(s), alarm(s), reminder(s), music, song(s), track(s), playlist, volume, notification(s), microphone, mic, mute, shuffle, repeat, spotify, nova, weather, forecast, calendar - or THING is only one of "it", "this", "that", "them", "these" or "those".
  - `routines.py`'s docstring says a step like "run goodnight" reaches Home Assistant's scripts through Assist, as the user would by voice.
  - README: exposing a thing to Assist now means Nova works it by voice, scripts aren't exposed by default (goodnight needs exposing), and it's worth checking what's exposed already, such as AdGuard's protection switch.

- [ ] **Step 1: Write the failing tests:**
  - `test_assists_own_sentences_go_to_assist` - "Pause the TV.", "Turn off AdGuard protection.", "Switch the fan on.", "Next track on the TV.", "Set the TV volume to 40.", "Is the fan on?", "Run goodnight.", "Activate movie night." and "Close the blinds." each give `control_home` with `wider: True`;
  - `test_what_the_assist_rule_mustnt_catch_is_left_alone` - "Turn off the microphone.", "Turn off my alarm.", "Turn off the timer.", "Turn on shuffle.", "Turn on repeat.", "Turn it off.", "Pause it.", "Is it on?", "Turn off the music.", "Turn off notifications.", "Turn off the TV and the lamp." and "Turn up the heating." are None; "Pause.", "Next track." and "Set the volume to 40." are still Nova's own;
  - `test_a_routine_runs_a_home_assistant_script_through_assist` (test_conversation.py) - a step "run goodnight" reaches `control_home` with `wider: True`;
  - `test_anything_else_is_left_to_claude` - its cases still pass.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_intents.py tests/test_conversation.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the rules, `NOVAS_OWN` and the handler, then the docs.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Send switches, media players, scripts and scenes to Assist, after Nova's own commands".

### Task 7: home_status, read by Claude

**Files:**
- Modify: `voice_assistant/home_assistant.py` (a new `status`); `voice_assistant/actions.py` (`tool(..., reads=False)`, `ActionExecutor.read_tools()`, a `home_status` tool); `voice_assistant/llm.py` (`LLMHandler.__init__`, `process_query`); `voice_assistant/conversation.py` (`_ask_claude`); `voice_assistant/session.py` (the `LLMHandler` it makes).
- Test: `tests/test_home_assistant.py`, `tests/test_actions.py`, `tests/test_llm.py`, `tests/test_conversation.py`.

**Interfaces:**
- Consumes: `Snapshot.area_name` from Task 1, and `format_duration` from `clock.py`.
- Produces:
  - `HomeAssistant.status(about: str, now: Optional[float] = None) -> Dict`. One read of `/api/states`; nothing is called.
    - It leaves out the batteries and charging states - device class `battery` or `battery_charging`, and ids ending `_battery_level` or `_battery_state` - which are 0007's. It leaves out entities ending `_geocoded_location` too, and gives no attributes at all, so no address or coordinates.
    - Each entity's score is how many of the asked words begin a word of its friendly name, its entity id or its area's name. The best `MOST_STATUS = 8` with a score above 0 are given, the highest first, then by name.
    - Each is "Name (entity_id, Area): state unit", with ", as of 3 hours ago" when its `last_reported` (else `last_updated`) is more than an hour old. A state that's a timestamp, as `sensor.*_next_setting` has, is given in local time as `YYYY-MM-DD HH:MM`.
    - None: "Nothing in Home Assistant matches that." Home Assistant down: `CANT_REACH`.
  - The tool `home_status(about)`, `needs="home"`, `reads=True`, described: "Read the state of things in Home Assistant - the house, the devices and the homelab - for 'when's sunset?', 'is Navidrome up?', 'any updates?', 'is the PC on?' or 'how warm is the bedroom?'. Give the words asked about. For batteries, use get_batteries. You get the answer to reply from."
  - `ActionExecutor.read_tools() -> List[str]` - the names of the tools with `reads=True`.
  - `LLMHandler(..., read_tools: Sequence[str] = ())`, and `process_query(text, context=None, lights=None, read: Optional[Callable[[Action], str]] = None)`. When every tool call in a response is a read tool, and `read` is given, it sends each answer back as a `tool_result` and asks again, at most `MOST_READS = 2` times. The final response's text and tool calls are the result. A read tool called beside another tool is run as an ordinary action, as now.
  - `Conversation._ask_claude` passes `read=lambda action: self.actions.execute(action).say`, and the session passes `read_tools=self.actions.read_tools()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_status_gives_the_best_few_matches_with_their_state_and_age` - "navidrome" ranks `binary_sensor.navidrome_status` first; an old one says ", as of 3 hours ago";
  - `test_status_leaves_out_batteries_and_where_things_are` - "s25 ultra" gives neither `sensor.s25_ultra_battery_level` nor `sensor.s25_ultra_geocoded_location`, and no attributes;
  - `test_status_says_when_nothing_matches` and `test_status_with_home_assistant_down_says_so`;
  - `test_a_read_tools_answer_goes_back_to_claude` (test_llm.py) - the fake client first calls `home_status` with `{"about": "sunset"}` (id "t1"), then answers "Sunset's at 5:52 PM."; `read` is given `Action("home_status", {"about": "sunset"}, from_model=True)`; the second request ends with the assistant's content, then a `tool_result` for "t1"; the result is that sentence, with no actions;
  - `test_reads_stop_after_two_rounds`;
  - `test_a_read_tool_beside_another_tool_is_run_as_an_action`;
  - `test_claude_reads_home_status_rather_than_it_being_said` (test_conversation.py);
  - `test_home_status_is_only_offered_with_home_assistant`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py tests/test_actions.py tests/test_llm.py tests/test_conversation.py -k "status or read"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say. The history keeps only the final reply, as for any other turn.
- [ ] **Step 4: Run** the changed files, then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff. Expected: all pass, and ruff is clean.
- [ ] **Step 5: Commit** - "Let Claude read Home Assistant with home_status".
