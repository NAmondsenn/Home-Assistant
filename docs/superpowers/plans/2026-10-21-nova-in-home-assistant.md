# Nova in Home Assistant - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Home Assistant decides Nova's room and does the controlling: Nova joins it as an MQTT device with its own controls, tells Assist which device is speaking, falls back to Claude with a light tool when Assist doesn't understand, and the settings page gets colour and warmth.

**Architecture:** `HomeAssistant` (REST) learns Nova's device id and area, sends `device_id` with every Assist request, and gains light reading and setting. A new `NovaDevice` (paho-mqtt) publishes Nova's discovery, state and availability, and turns Home Assistant's commands into calls on Nova. `Conversation` sends an Assist "not understood" on to Claude, with the lights described beside the timers.

**Tech Stack:** Python 3.13 on the Pi, requests, paho-mqtt (new), Home Assistant 2026.10 REST API and MQTT discovery, plain JS/CSS on the settings page.

**Spec:** [docs/decisions/0005-nova-in-home-assistant.md](../../decisions/0005-nova-in-home-assistant.md)

## Global Constraints

- One new dependency only: paho-mqtt, in `requirements.txt` and pinned in `requirements.lock`.
- MQTT is off unless `mqtt.host` is set; with it off, or the broker down, Nova works exactly as today - Assist over REST never depends on MQTT.
- Nothing said to Nova, and none of its answers, is published to Home Assistant.
- Locks, garage doors and doors stay refused (`SENSITIVE`); Claude's light tool touches `light.*` and HA areas only.
- No passwords or the user's addresses in the repo: `MQTT_PASSWORD` lives in `~/.config/nova/.env`, addresses in `~/.config/nova/config.yaml`.
- House style: comments in full sentences, British spelling, spaced " - " dashes, *why* not *what*; Google-style docstrings; LF endings; one commit per task.
- Tests run on the Pi: `bash <scratchpad>/pi_test.sh [args]` (copies the working tree to `/tmp/nova-check/work`, runs pytest with the live venv and scratch config/data); also `TZ_OVERRIDE=UTC`; ruff with `/tmp/nova-check-pytest/bin/ruff check .` there.
- Entity names exactly: Status, Volume, Alarm volume, Music volume, Next alarm, Timers, Stop, Restart, Speak, Backend, Microphone muted.

## Review Focus

1. Broker unreachable or wrong password at start - Nova starts, logs once, keeps retrying in the background (Task 4 test `test_a_broker_which_cant_be_reached_doesnt_stop_nova`).
2. Junk from Home Assistant - "abc" or "150" for a volume, an empty Speak - ignored or clamped, never raised (Task 4 test `test_junk_commands_are_ignored`).
3. Nova with no area yet (device not found, or not placed) - Assist is asked without `device_id`, Claude is told the room isn't known (Task 1 `test_without_a_device_assist_is_asked_as_before`, Task 3 `test_claude_is_told_when_novas_room_isnt_known`).
4. Speak arriving mid-conversation or while muted - it waits its turn, and is still said when muted, since mute only stops the listening (Task 5 `test_speak_is_said_in_turn_even_when_muted`).
5. The user renaming Nova's entities or moving it to another area - found again by MQTT identifier, the new area used within the next lookup (Task 1 `test_the_device_is_found_by_its_identifier_not_an_entity_name`).

---

### Task 1: Assist told which device is speaking, and "not understood" told apart

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`HomeAssistant.__init__`, `command`, new `find_device`)
- Modify: `voice_assistant/actions.py` (`ActionResult` gains `understood`; `from_reply` reads it)
- Test: `tests/test_home_assistant.py`, `tests/test_actions.py`

**Interfaces:**
- Produces: `HomeAssistant.device_id: Optional[str]`, `HomeAssistant.area: Optional[str]` (area *name*, e.g. "Bedroom"), `HomeAssistant.find_device(identifier: str) -> bool`; `command()` returns `{"success": False, "message": ..., "not_understood": True}` when Assist matched nothing; `ActionResult.understood: bool = True`.

- [ ] **Step 1: Write the failing tests**

```python
def test_assist_is_told_which_device_is_speaking(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned on the lights")})
    home.device_id = "da94"
    home.command("lights on")
    assert home._session.requests[0][2]["json"]["device_id"] == "da94"

def test_without_a_device_assist_is_asked_as_before(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Done")})
    home.command("lights on")
    assert "device_id" not in home._session.requests[0][2]["json"]

@pytest.mark.parametrize("code", ["no_intent_match", "no_valid_targets"])
def test_assist_not_understanding_is_told_apart(make_home, code):
    reply = _assist("error", "Sorry, I couldn't understand that"); reply["response"]["data"] = {"code": code}
    result = make_home({"/api/conversation/process": reply}).command("dim the lights")
    assert result == {"success": False, "message": "Sorry, I couldn't understand that", "not_understood": True}

def test_the_device_is_found_by_its_identifier_not_an_entity_name(make_home):
    home = make_home({"/api/template": FakeResponse("da94|Bedroom")})
    assert home.find_device("nova_home_assistant") is True
    assert (home.device_id, home.area) == ("da94", "Bedroom")
    assert "nova_home_assistant" in home._session.requests[0][2]["json"]["template"]
    home._session.answers["/api/template"] = FakeResponse("|")
    assert home.find_device("nova_home_assistant") is False and home.device_id is None
```

`FakeResponse` needs a `text` attribute for the template endpoint (it returns plain text): add `self.text = body if isinstance(body, str) else ""`. In `tests/test_actions.py`: `assert ActionResult.from_reply({"success": False, "message": "x", "not_understood": True}).understood is False` and `ActionResult.from_reply({"success": True}).understood is True`.

- [ ] **Step 2: Run them to verify they fail** - `pi_test.sh tests/test_home_assistant.py tests/test_actions.py`; expect KeyError/AttributeError on `device_id`, `find_device`, `understood`.

- [ ] **Step 3: Implement**
  - `command()`: add `"device_id": self.device_id` to the JSON only when set; on `response_type == "error"` with `(response.get("data") or {}).get("code") in ("no_intent_match", "no_valid_targets")` add `"not_understood": True`.
  - `find_device(identifier)`: `POST /api/template` with a template that walks every entity's device and finds the one whose `device_attr(d, 'identifiers')` contains `('mqtt', identifier)`, rendering `"<device_id>|<area_name or ''>"`. Sets `device_id`/`area` (None when empty); logs at INFO only when the area changes ("Nova is in the Bedroom, as Home Assistant has it"). Any request error leaves the old values and returns False.
  - `ActionResult.understood: bool = True`; `from_reply` sets it `not reply.get("not_understood")`.

- [ ] **Step 4: Run the tests to verify they pass**, then the whole suite.

- [ ] **Step 5: Commit** - "Tell Home Assistant's Assist which device is speaking"

### Task 2: Claude's light tool, and the lights read from Home Assistant

**Files:**
- Modify: `voice_assistant/home_assistant.py` (new `lights`, `set_lights`, `describe_lights`; `_light` gains area and colour; `overview` uses `lights`)
- Modify: `voice_assistant/actions.py` (`tool(..., for_model=True)`, `tool_schemas` skips `for_model=False`; `control_home` becomes `for_model=False`; new `set_lights` tool)
- Test: `tests/test_home_assistant.py`, `tests/test_actions.py`

**Interfaces:**
- Consumes: Task 1's `HomeAssistant.area`.
- Produces:
  - `HomeAssistant.lights() -> List[Dict]` - each `{"id", "name", "area", "on", "reachable", "brightness", "colour_modes", "hue", "saturation", "kelvin", "min_kelvin", "max_kelvin"}` (`hue`/`saturation` from `hs_color` when its colour mode is hs, else None; `kelvin` from `color_temp_kelvin` when its mode is color_temp, else None). One `POST /api/template` returning JSON for every light with its area name - one request, not one per light.
  - `HomeAssistant.set_lights(on: bool, light: Optional[str] = None, area: Optional[str] = None, brightness: Optional[int] = None, colour: Optional[str] = None, warmth: Optional[int] = None) -> Dict` - `light.turn_on`/`turn_off` with `entity_id` (must match `LIGHT`) or the area's id (looked up from its name), `brightness_pct` clamped 1-100, `color_name`, `color_temp_kelvin`; neither light nor area → Nova's own area, or `{"success": False, "message": "Which room?"}` when that isn't known. Success → `{"success": True, "message": "Done.", "chime": True}`.
  - `HomeAssistant.describe_lights() -> str` - e.g. `"Nova is in the Bedroom. Lights: Lamp (light.lamp, Bedroom) off; Bedroom Light (light.bedroom_light, Bedroom) on at 40%, 2700K"`, or starting `"Nova's room isn't known."`; `""` if the lights can't be read.
  - `ActionExecutor.set_lights(on, light=None, area=None, brightness=None, colour=None, warmth=None)` tool, `needs="home"`, required `["on"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lights_come_with_their_area_and_colour(make_home): ...
    # template answer: JSON for light.lamp in Bedroom, on, hs mode (240, 100), brightness 102
    # assert lights()[0] includes {"area": "Bedroom", "on": True, "brightness": 40, "hue": 240, "saturation": 100, "kelvin": None}

def test_set_lights_defaults_to_novas_own_room(make_home):
    # home.area = "Bedroom"; template answers area id "bedroom"
    # set_lights(on=True, warmth=2700) posts /api/services/light/turn_on with {"area_id": "bedroom", "color_temp_kelvin": 2700}

def test_set_lights_asks_which_room_when_novas_isnt_known(make_home):
    assert make_home({}).set_lights(on=True) == {"success": False, "message": "Which room?"}

def test_set_lights_wont_touch_anything_but_a_light(make_home):
    assert not make_home({}).set_lights(on=False, light="lock.front_door")["success"]

def test_claude_isnt_offered_control_home_but_is_offered_set_lights():
    names = [schema["name"] for schema in ActionExecutor(home=object()).tool_schemas()]
    assert "set_lights" in names and "control_home" not in names

def test_a_local_light_command_still_reaches_assist(): # control_home still runs through execute()
```

- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement** as in Interfaces. `overview()` now builds its lights from `lights()`, sorted with Nova's own area first, then by area, then name; the settings page's existing fields (`id`, `name`, `on`, `reachable`, `dimmable`, `brightness`) stay. `set_lights`' tool description: works lights only, by name or area, "the lights" with no room means Nova's own; reply "Okay." since a chime confirms it.
- [ ] **Step 4: Run the tests, then the whole suite.**
- [ ] **Step 5: Commit** - "Let Claude work the lights through Home Assistant's own services"

### Task 3: What Assist doesn't understand goes to Claude, who's told about the lights

**Files:**
- Modify: `voice_assistant/conversation.py` (`handle`)
- Test: `tests/test_conversation.py`

**Interfaces:**
- Consumes: `ActionResult.understood` (Task 1), `HomeAssistant.describe_lights()` (Task 2), reached as `self.actions.home` (None without HA).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_local_command_assist_didnt_understand_goes_to_claude():
    intents = FakeIntents({"dim the lights": _answer("Okay.", Action("control_home", {"command": "dim the lights"}))})
    actions = FakeActions({"control_home": ActionResult(False, "Sorry, I couldn't understand that", understood=False),
                           "set_lights": ActionResult(True, "Done.", confirm="chime")})
    llm = FakeLLM(_answer("Okay.", Action("set_lights", {"on": True, "brightness": 30})))
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("dim the lights")
    assert llm.asked == ["dim the lights"] and actions.run == ["control_home", "set_lights"]
    assert "couldn't understand" not in reply.say

def test_claude_is_told_about_the_lights_beside_the_timers():
    # FakeActions.home = object with describe_lights() -> "Nova is in the Bedroom. Lights: ..."
    # FakeLLM.process_query records context; assert "Nova is in the Bedroom" in the context sent

def test_claude_is_told_when_novas_room_isnt_known():
    # describe_lights() -> "Nova's room isn't known. Lights: ..." reaches the context unchanged
```

`FakeLLM.process_query` keeps `context` in `self.contexts`; `FakeActions` gains `home=None`.

- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement** - after a local intent's actions run, if any result has `understood=False`, run the request through Claude exactly as if no intent had matched (the local intent's `remember` is undone by not calling it until the outcome is known). The context sent to Claude is the clock's lines plus `describe_lights()` when it's non-empty, joined with "; ".
- [ ] **Step 4: Run the tests, then the whole suite.**
- [ ] **Step 5: Commit** - "Ask Claude when Home Assistant doesn't understand, and tell it about the lights"

### Task 4: Nova as an MQTT device

**Files:**
- Create: `voice_assistant/ha_device.py`
- Modify: `config.yaml` (new `mqtt:` section), `voice_assistant/settings_page.py` (`KEYS` gains `("MQTT_PASSWORD", "Home Assistant's MQTT")`), `requirements.txt`, `requirements.lock`
- Test: `tests/test_ha_device.py`

**Interfaces:**
- Produces:
  - `DeviceControls` dataclass of callables: `status() -> str`, `volumes() -> Dict[str, int]`, `set_volume(category: str, percent: int) -> None`, `clock() -> Dict` (Clock.overview's shape), `backend() -> Optional[bool]`, `muted() -> bool`, `set_muted(muted: bool) -> None`, `stop() -> None`, `restart() -> None`, `speak(message: str) -> None`.
  - `NovaDevice(host: str, port: int, username: str, password: str, controls: DeviceControls, name: str = "Nova", node_id: Optional[str] = None, client_factory=None)`; `NovaDevice.from_config(config, password: Optional[str], controls) -> Optional[NovaDevice]` (None unless `mqtt.host` is set; warns if the password is missing); `.identifier -> str` (the MQTT device identifier, `node_id`); `.start()`, `.stop()`, `.publish_state()`.
- Config: `mqtt: {host: "", port: 1883, username: "", name: "Nova"}` with comments in the file's style.
- Topics, with `base = f"nova/{node_id}"` and `node_id = slug(f"{name}_{socket.gethostname()}")` (e.g. `nova_home_assistant`): availability `base/availability` (`online`/`offline`, retained, last will `offline`); state `base/status`, `base/volume/<category>`, `base/next_alarm` (ISO time or `None`), `base/timers` (count) with `base/timers/attributes` (JSON list of name and end time), `base/backend` and `base/muted` (`ON`/`OFF`); commands `base/volume/<category>/set`, `base/muted/set`, `base/stop`, `base/restart`, `base/speak`. Discovery: one retained device-based message to `homeassistant/device/<node_id>/config` (`dev` with identifiers `[node_id]`, name, manufacturer "Nova", model "Raspberry Pi"; `o` origin; `cmps` for the eleven entities), sent on connect and again whenever `homeassistant/status` says `online`.

- [ ] **Step 1: Write the failing tests** (a `FakeClient` records publishes and subscriptions and lets the test deliver messages)

```python
def test_nova_announces_itself_with_its_eleven_controls(): ...
    # after connect: one retained publish to homeassistant/device/nova_test/config whose cmps names are exactly
    # {"Status", "Volume", "Alarm volume", "Music volume", "Next alarm", "Timers", "Stop", "Restart", "Speak", "Backend", "Microphone muted"}
def test_state_is_published_only_when_it_changes(): ...
def test_home_assistants_commands_reach_nova(): ...
    # volume/alarm/set "40" -> set_volume("alarm", 40); muted/set "ON" -> set_muted(True); stop -> stop(); speak "Washing's done" -> speak(...)
def test_junk_commands_are_ignored(): ...
    # volume/general/set "abc" -> nothing; "150" -> set_volume(..., 100); speak "" -> nothing
def test_a_broker_which_cant_be_reached_doesnt_stop_nova(): ...
    # client.connect raising OSError: start() returns, logs once, the client keeps reconnecting (loop_start called)
def test_its_off_without_a_broker(): assert NovaDevice.from_config(config_without_host, "pw", controls) is None
def test_nothing_said_is_published(): ...  # no topic or payload carries a transcript - only the topics listed above
```

- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement** with paho-mqtt's v2 callback API: `connect_async` + `loop_start` (it reconnects by itself), `will_set` for availability, subscriptions renewed in `on_connect`. A daemon thread calls `publish_state()` every second; only changed values are published (retained). Install paho-mqtt on the Pi (`pip install paho-mqtt` into a scratch target to learn the current version), add `paho-mqtt>=2.1` to `requirements.txt` and the exact version to `requirements.lock`.
- [ ] **Step 4: Run the tests, then the whole suite and ruff.**
- [ ] **Step 5: Commit** - "Add Nova to Home Assistant as a device of its own, over MQTT"

### Task 5: Muting the microphone, and Nova wired to its device

**Files:**
- Modify: `voice_assistant/wake_word.py` (mute), `main.py` (build `NovaDevice` and its controls; a device lookup thread), `voice_assistant/settings_page.py` (`state()` gains `"muted"`; `POST /api/mute`), `voice_assistant/web/settings.{html,js,css}` (a mute switch beside the status)
- Test: `tests/test_wake_word.py`, `tests/test_main.py`, `tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 1's `find_device`, Task 4's `NovaDevice`, `DeviceControls`.
- Produces: `WakeWordDetector.muted: bool` (property), `WakeWordDetector.set_muted(muted: bool) -> None`, kept in `paths.DATA_DIR/microphone.json` (`{"muted": true}`), path overridable for tests; `SettingsPage.set_muted(muted: bool, who: str) -> Dict`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_muted_microphone_isnt_woken(tmp_path): ...       # scores above sensitivity don't return while muted; do once unmuted
def test_mute_lasts_through_a_restart(tmp_path): ...         # a new detector reading the same file starts muted
def test_speak_is_said_in_turn_even_when_muted(): ...        # SmartAssistant's speak control calls _announce(message), whatever the mute
def test_stop_silences_what_rings_and_what_nova_is_saying(): # stop control: clock.dismiss() when ringing; speaker.stop(["reply", "announcement"])
def test_the_device_is_looked_up_until_found_then_now_and_then(): # every 30s until find_device is True, then every 300s
def test_the_settings_page_shows_and_changes_mute(): ...
```

- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement.** Mute checks happen where a score crosses the sensitivity, so the microphone keeps being read (positions stay right for catch-up). The controls in `main.py`: `status=lambda: self.activity`; volumes and `set_volume` go the way the settings page's do (music through the player); `clock=self.clock.overview`; `backend` from `self.stt.reachable` when it has one; `restart=restart_service`; `speak=lambda message: self._announce(message)` on a thread of its own. Started in `run()` beside ntfy, stopped in `_clean_up`. The page's mute switch sits in the status panel and uses the same `send()` helper and toast as the other switches.
- [ ] **Step 4: Run the tests, then the whole suite and ruff.**
- [ ] **Step 5: Commit** - "Mute the microphone, from Home Assistant or the settings page"

### Task 6: Colour and warmth on the settings page

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`set_light` gains `hue`, `saturation`, `kelvin`), `voice_assistant/settings_page.py` (`/api/light` passes them on; `home_state` adds `"area"`), `voice_assistant/web/settings.{js,css}` (`lightRow`, grouping)
- Test: `tests/test_home_assistant.py`, `tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 2's light fields.
- Produces: `HomeAssistant.set_light(entity_id, on=None, brightness=None, hue=None, saturation=None, kelvin=None) -> Dict` - `hs_color` when hue and saturation are given, `color_temp_kelvin` clamped to the light's range when kelvin is.

- [ ] **Step 1: Write the failing tests** - `set_light("light.lamp", hue=240, saturation=100)` posts `{"entity_id": "light.lamp", "hs_color": [240, 100]}` to turn_on; `kelvin=9000` on a 2500-6500K light posts 6500; the page's `/api/light` with `{"id", "hue", "saturation"}` reaches it; `home_state()["area"] == "Bedroom"`.
- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement.** In `lightRow`, a light with `hs` in its modes gets a row of colour swatches plus a hue slider; one with `color_temp` gets a warm-to-cool slider (gradient from amber to cool white) over its own kelvin range. Both send through `steadily()` as brightness does; the current colour shows as the light's icon tint. Lights are grouped under their area's name, Nova's own area first. No outside libraries (the CSP blocks them).
- [ ] **Step 4: Run the tests; check the page in Chrome at 1440 and 390 wide** against the live Pi page (its address on the tailnet) after deploying, or a local preview: no console errors or CSP refusals, swatches and sliders change the real lights.
- [ ] **Step 5: Commit** - "Choose a light's colour and warmth on the settings page"

### Task 7: README, roadmap, and switching it on at home

**Files:**
- Modify: `README.md` (a "Nova in Home Assistant" section: the HA user, the `mqtt:` settings, `MQTT_PASSWORD`, placing the device in an area), `docs/roadmap.md` (tick "The lights first")

- [ ] **Step 1: Write the docs**, then commit - "Say how to put Nova in Home Assistant".
- [ ] **Step 2: Make Nova's HA user** (the user agreed: "just make it with the token, call it Nova"), with HA's websocket API and the admin token, from a scratch script on the Pi: `config/auth/create` (name "Nova", `group_ids: ["system-users"]`, `local_only: true`), then `config/auth_provider/homeassistant/create` (username `nova`, a password generated on the Pi and written straight to `~/.config/nova/.env` as `MQTT_PASSWORD`, never shown).
- [ ] **Step 3: Find the broker's address** - Home Assistant's LAN address (the Supervisor's `/network/info`, through the websocket's `supervisor/api`), and check port 1883 answers from the Pi.
- [ ] **Step 4: Ask the user before pushing `phase-5`**, then deploy on the Pi (`git fetch && git switch phase-5 && bash update.sh`), and add `mqtt:` (host, username `nova`) to the Pi's `config.yaml`; restart.
- [ ] **Step 5: Place the device** in the Bedroom area (`config/device_registry/update`, `area_id: "bedroom"`), and tell the user.
- [ ] **Step 6: Check it live** - the device in HA with its eleven controls; volume sliders and mute work both ways; Speak from HA's developer tools is said; Nova shows unavailable when stopped; the log says "Nova is in the Bedroom". Then ask the user to try by voice: "lights on", "lamp off", "main light on", "dim the lights", "make the lamp blue", and the page's colours from their phone.
