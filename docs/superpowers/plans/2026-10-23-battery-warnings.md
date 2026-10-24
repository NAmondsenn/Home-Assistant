# Battery levels and warnings - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova finds every battery Home Assistant knows of, answers "how's my Mac's battery?", and warns about low ones - out loud when the user's home, and on their phone - with every choice on the settings page.

**Architecture:** A new `voice_assistant/batteries.py` holds three things. First, the pure rules: what kind a device is, whether it's charging, and its spoken name. Second, the warning state, kept in the data folder. Third, a `BatteryWatcher` thread on the brain, which reads Home Assistant every minute and delivers warnings through callables the brain gives it. `HomeAssistant` gains two read-only template reads, `batteries()` and `people()`. Sessions answer questions through a new `get_batteries` tool, which the local commands reach too. The settings page gets a Batteries section, whose choices go straight to the watcher, with no restart.

**Tech stack:** Python 3.13 on the brain, Home Assistant's template API (`POST /api/template`), ntfy, pytest, and the page's own vanilla JS and CSS.

**Spec:** `docs/decisions/0007-battery-warnings.md` - read it before starting any task.

## Global constraints

- **Marks:** `[5]` for mobile devices (Android phones and tablets, iPhones, iPads). `[15, 5]` for everything else, including anything whose kind can't be worked out.
- **Timing:** Nova checks every 60 seconds. A reading counts as old after 3600 seconds, and is then said with its age - "88%, as of three days ago".
- **Repository defaults:** `config.yaml` gets a `batteries` section with `mobile_marks: [5]`, `other_marks: [15, 5]`, `check_every: 60` and `stale_after: 3600`. Per-device choices live only in the data folder (`paths.DATA_DIR`), in `batteries.json`, never in `config.yaml`.
- **Discovery:** nothing names an entity. Batteries are sensors with device class `battery` whose unit is `%`. The person comes from Home Assistant's `person` entities, as it runs.
- **Where warnings go:**
  - every warning is sent to `ntfy.phone`, unless the device's "send to my phone" is off. That's off by default for the phone itself;
  - as well, with the user home and outside ntfy's quiet hours (`Ntfy.quiet()`), it's said on the default satellite;
  - "Also send to my phone when Nova says it" is on by default. Off, a warning Nova says isn't sent as well;
  - a warning which ought to have been sent, but couldn't be (no ntfy, or a failed send), is said instead. In quiet hours it's held until they end, then said only if it's still true;
  - the phone's own warning is only ever said.
- **When marks start over:** the marks a device has warned at start over once it's charging, or once its level is 10 or more points above its highest mark, whatever its charging state says.
  - The second rule is my call, on top of 0007, from Home Assistant's history for 13-23 October. The Mac's `_internal_battery_state` said "Not Charging" while its level fell from 51% to 41%, so "Not Charging" means on battery. But in ten days it never said "Charging", though the Mac charged to about 70% in that time. So its charging state alone can't start the marks over. The same rule covers a remote or sensor with no charging state, when it gets a new battery.
  - The phone's `_battery_state` says `charging`, `discharging` and `full`, as expected.
- **Spoken words:** in Nova's style, e.g. "Your MacBook Pro's battery is down to 15%."
- **Never in quiet hours:** nothing is said out loud between `ntfy.quiet_from` and `ntfy.quiet_until`.
- **House style:** `.superpowers/sdd/2026-10-22-the-pi-as-a-satellite/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** they run on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes root@home-assistant 'cd /tmp/nova-check/work && /tmp/nova-check-pytest/bin/ruff check .'`.
- **Commits:** small, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Safety:** never push or deploy; touch no machine except through those two commands.

## Review focus

1. **A sensor reading `unavailable`, `unknown` or something not a number** is skipped. It's never taken as 0%, which would give a false warning. (Task 1, test `test_an_unreadable_battery_is_left_out`.)
2. **Home Assistant down, or refusing templates** (a token which isn't an administrator's), means no warnings and no crash, logged once until it answers again. Asking then says Nova can't reach Home Assistant. (Tasks 4 and 5.)
3. **A device removed from Home Assistant** drops off, with its choices kept. A new device never inherits another's warned marks. (Tasks 3 and 6.)
4. **A level such as "15.0", or one that isn't `%`**: decimals are read as numbers, and anything not in `%` is left out. (Task 1, `test_a_decimal_level_is_read_and_other_units_are_left_out`.)
5. **A battery sensor with no device**, made by a template, is "something else", named by its friendly name, with no charging state. (Tasks 1 and 2.)

---

### Task 1: Home Assistant's batteries and people

**Files:**
- Modify: `voice_assistant/home_assistant.py` - two template constants beside `LIGHTS_TEMPLATE`, and two methods after `lights()`.
- Test: `tests/test_home_assistant.py`

**Interfaces:**
- Produces:
  - `HomeAssistant.batteries(timeout: Optional[float] = None) -> List[Dict]`, one dict per battery sensor in `%`, with readable levels only. Keys:
    - `"id"`: the entity id;
    - `"name"`: the device's `name_by_user`, else its name, else the entity's friendly name;
    - `"level"`: a float;
    - `"changed"`: epoch seconds of `last_reported` - when Home Assistant last heard the reading, not when its value last moved (`last_changed` would make a steady level look old);
    - `"device"`: the device id, or None;
    - `"maker"`, `"model"`: or None;
    - `"app"`: a bool, whether the battery sensor belongs to the Home Assistant app (`battery.entity_id in integration_entities('mobile_app')` in the template);
    - `"charging"`: the raw state of the same device's charging entity, or None. That's a `binary_sensor` with device class `battery_charging`, else a `sensor` whose entity id ends in `battery_state`.
  - `HomeAssistant.people(timeout: Optional[float] = None) -> List[Dict]`, with keys `"id"`, `"state"` (e.g. `"home"`, `"not_home"`) and `"devices"`: the device ids of the person's `device_trackers`.
  - Both raise like `lights()`: `requests.RequestException`, or `ValueError` for an answer that isn't the expected JSON.

- [ ] **Step 1: Write the failing tests** with the file's `make_home` and `FakeSession`, answering `/api/template` with the JSON a template would render:
  - `test_batteries_are_read_with_their_device_and_charging_state`: the S25 (`level "46"`, maker samsung, model SM-S938B, charging "discharging") comes back as `{"id": "sensor.s25_ultra_battery_level", "name": "s25-ultra", "level": 46.0, ...}`;
  - `test_an_unreadable_battery_is_left_out`: states `unavailable`, `unknown` and `""` are absent from the result;
  - `test_a_decimal_level_is_read_and_other_units_are_left_out`: `"15.0"` gives `15.0`, and a battery in `V` is absent;
  - `test_a_battery_with_no_device_is_named_by_its_friendly_name`;
  - `test_people_come_with_their_trackers_devices`;
  - `test_a_refused_template_raises_as_for_the_lights`.
- [ ] **Step 2: Run them** - `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py -k "batter or people"`. Expected: they fail, with `AttributeError: 'HomeAssistant' object has no attribute 'batteries'`.
- [ ] **Step 3: Implement** `BATTERIES_TEMPLATE`, `PEOPLE_TEMPLATE`, `batteries()` and `people()`. Each template renders the list as JSON with `to_json`, as `LIGHTS_TEMPLATE` does. The filtering - unit, readable level, the float - is done in Python, where it's tested, not in Jinja.
- [ ] **Step 4: Run them again** - expected: they pass. Then run the whole file.
- [ ] **Step 5: Commit** - "Read Home Assistant's batteries and people for battery warnings".

### Task 2: What a battery is - its kind, charging and name

**Files:**
- Create: `voice_assistant/batteries.py`, with the module docstring saying what 0007 decided and why.
- Test: `tests/test_batteries.py`, new.

**Interfaces:**
- Consumes: Task 1's battery dicts.
- Produces:
  - `@dataclass Battery`, with fields `id: str`, `name: str` (spoken, see `spoken_name`), `level: float`, `changed: float`, `device: Optional[str]`, `kind: str` (`"mobile"` or `"other"`), `phone: bool`, `computer: bool` (on the app and an Apple Mac) and `charging: Optional[bool]`;
  - `Battery.from_home(found: Dict) -> Battery`;
  - `kind_of(maker: Optional[str], model: Optional[str], app: bool) -> str` (on the app is "mobile", unless it's an Apple Mac; off the app is "other");
  - `is_phone(maker: Optional[str], model: Optional[str]) -> bool` (only an Apple "iPhone..." or a Samsung SM-S, G, A, F, N, M or E model; every other mobile device counts as a tablet);
  - `charging_from(raw: Optional[str]) -> Optional[bool]`;
  - `spoken_name(name: str) -> str`;
  - `find(batteries: List[Battery], asked: str) -> List[Battery]`;
  - `said(battery: Battery, now: float, stale_after: float) -> str`.

- [ ] **Step 1: Write the failing tests:**
  - `test_todays_devices_are_sorted_into_kinds`: samsung SM-S938B is mobile and a phone; samsung SM-X930 is mobile and not a phone; Apple Mac16,8 is "other"; `(None, None)` is "other". Also iPhone16,2 is a mobile phone, and iPad14,1 is mobile but not a phone;
  - `test_charging_is_read_from_each_apps_words`: `"charging"`, `"Charging"`, `"full"` and `"on"` → True; `"discharging"`, `"not_charging"`, `"Not Charging"` and `"off"` → False; None, `"unknown"` and `"unavailable"` → None;
  - `test_names_are_said_as_words`: `"macbook-pro"` → `"MacBook Pro"`, `"s25-ultra"` → `"S25 Ultra"`, `"tab-s11-ultra"` → `"Tab S11 Ultra"`;
  - `test_asking_finds_the_device_meant`: against today's three, `"mac"`, `"macbook"` and `"laptop"` give the Mac; `"phone"` gives the S25; `"tablet"` and `"tab"` give the tablet; `""` and `"all"` give all three; `"toaster"` gives `[]`;
  - `test_a_fresh_level_is_said_with_whether_its_charging`: `said(...)` gives `"Your MacBook Pro's on 72%, and not charging."`;
  - `test_an_old_level_says_how_old` (three days, from `format_duration` in clock.py): `"Your Tab S11 Ultra was on 88%, 3 days ago."`;
  - `test_a_battery_with_no_device_is_something_else_and_never_charging`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_batteries.py`. Expected: they fail, with `ModuleNotFoundError: voice_assistant.batteries`.
- [ ] **Step 3: Implement** the functions in `voice_assistant/batteries.py`.
  - `spoken_name` keeps a word with a digit as it is, upper-cased (`S25`), and capitalises the rest, apart from a short list of known spellings (`MacBook`, `iPhone`, `iPad`).
  - `find` matches the asked words against each name's words, prefixes included. Then it matches against kind words: phone, tablet, and (against `computer` only) laptop, computer and mac. It keeps only the batteries matching the most asked words, at least one.
- [ ] **Step 4: Run them again** - expected: they pass.
- [ ] **Step 5: Commit** - "Tell what each battery is, whether it's charging, and its name".

### Task 3: When to warn, and remembering it

**Files:**
- Modify: `voice_assistant/batteries.py`
- Test: `tests/test_batteries.py`

**Interfaces:**
- Consumes: `Battery` from Task 2.
- Produces:
  - `@dataclass BatteryWarning`, with `battery: Battery` and `mark: int`;
  - `class BatteryChoices` holds the per-device choices and warned marks, saved to `batteries.json` with `storage.save_json` and read with `storage.load_json`:
    - `__init__(path: str, mobile_marks: List[int], other_marks: List[int])`;
    - `marks_for(battery: Battery) -> List[int]` - the device's own if set, else its kind's default, highest first;
    - `enabled(battery_id: str) -> bool`;
    - `send_to_phone(battery: Battery) -> bool` - the default is `not battery.phone`;
    - `also_send_when_said: bool`;
    - `default_marks(battery: Battery) -> List[int]` - its kind's default marks, highest first;
    - `set(battery: Battery, enabled: Optional[bool] = None, marks: Optional[List[int]] = None, send_to_phone: Optional[bool] = None)` - this saves. It takes the Battery, not its id, as checking the number of marks needs the kind; None leaves a choice as it is;
    - `reset(battery_id: str)` - forgets the device's own enabled, marks and send_to_phone, keeps its warned marks, and saves;
    - `set_also_send_when_said(on: bool)` - this saves too;
    - `due(batteries: List[Battery]) -> List[BatteryWarning]` - which warnings to give now. It updates and saves the warned marks.
    - Task 4 adds the held warnings, kept in the same file: `hold(battery_id: str, mark: int)` (saves; one held warning for each device), `held() -> Dict[str, int]` (sensor id to mark) and `release(battery_id: str)` (saves).
    - One lock inside `BatteryChoices` guards its state and every save, as the watcher thread (`due`) and the settings page's thread (`set`, `reset`, `set_also_send_when_said`) share it.
  - The rule `due` follows, for each battery:
    - if it's charging, its warned marks are cleared, enabled or not (a charge while it was switched off isn't missed);
    - if its level is at or above its start-over level, `min(highest mark + 10, 100)`, they're cleared too, whatever its charging state;
    - otherwise, only an enabled battery warns: take the marks it's under (level < mark) which haven't warned. If there are any, there's one `BatteryWarning` for the lowest of them, and all the marks it's under are marked as warned. A switched-off battery's warned marks are left as they are.

- [ ] **Step 1: Write the failing tests:**
  - `test_falling_past_a_mark_warns_once`: Mac at 16 then 15 → nothing, then one warning at 15. A third check at 15 gives nothing;
  - `test_a_wobble_round_a_mark_never_warns_twice`: 15, 16, 14, 16, 14 on battery → one warning;
  - `test_charging_starts_the_marks_over`: warned at 15; then charging at 40; then on battery at 14 → a second warning at 15;
  - `test_first_seen_below_a_mark_warns_once_for_the_lowest`: the Mac first seen at 10 → one warning at 15. The phone unplugged at 4 → one at 5. The Mac at 4 → one at 5, with 15 counted as warned too;
  - `test_a_device_with_no_charging_state_starts_over_once_its_battery_is_changed`: at 4 → warns. At 20 → nothing. At 25 or more → cleared, so 4 again warns;
  - `test_the_mac_starts_over_once_charged_though_it_still_says_not_charging`: the Mac, `charging=False` throughout, at 14 → warns at 15. At 80 → cleared. Back at 14 → warns at 15 again. At 20 → nothing, since a wobble isn't a charge;
  - `test_a_device_switched_off_gives_no_warnings` - and it's still found by `find`;
  - `test_a_devices_own_marks_replace_its_kinds`;
  - `test_the_phone_isnt_sent_its_own_warning_by_default_and_a_tablet_is`;
  - `test_choices_and_warned_marks_survive_a_restart`: a new `BatteryChoices` on the same path warns nothing again;
  - `test_a_new_device_never_takes_another_devices_warned_marks`;
  - `test_a_device_gone_from_home_assistant_keeps_its_choices`: it's absent from `due`'s input, then back with its choices.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_batteries.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** `BatteryWarning` and `BatteryChoices`. Every saved file is written whole through `storage.save_json`, the way other data files are, so a failed save leaves the last good one. `marks` given to `set` are validated: whole numbers from 1 to 99, distinct, one for a mobile device and two for anything else. Anything else raises `ValueError` with a sentence the page can show.
- [ ] **Step 4: Run them again** - expected: they pass.
- [ ] **Step 5: Commit** - "Decide when a battery warns, once for each mark, and remember it".

### Task 4: The watcher, and where each warning goes

**Files:**
- Modify: `voice_assistant/batteries.py`
- Test: `tests/test_batteries.py`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `class BatteryWatcher`.
  - `__init__(home: HomeAssistant, choices: BatteryChoices, say: Callable[[str], bool], send: Optional[Callable[[str, str], bool]], quiet: Callable[[], bool], check_every: float, stale_after: float)`:
    - `say(message)` returns whether it was said, which needs the default satellite to be connected;
    - `send(message, title)` returns whether ntfy took it, and is None without ntfy.
  - `read() -> List[Battery]` - fresh from Home Assistant. It raises like `HomeAssistant.batteries`.
  - `check(now: float)` - one pass: read, `due`, deliver. It also says any held warning which is still true once quiet hours are over.
  - `start()` and `stop()` - a daemon thread named `batteries`, calling `check` every `check_every` seconds. Every pass is wrapped, so one error is logged and the next pass still runs. Home Assistant being down is logged once until it answers again.
  - `home_now(battery: Battery) -> Optional[bool]` - from `people()`: the person whose `devices` include the battery's device, else the first person. `"home"` → True, any other known state → False, no person or an error → None.
  - Held warnings are kept in `batteries.json` too, so a restart in quiet hours keeps them.
- The delivery, for one `BatteryWarning` - its message `f"Your {name}'s battery is down to {round(level)}%."`, title `"Battery low"`:
  1. `sending` = the device's "send to my phone" and `send is not None`.
  2. If the user is home (True) and it isn't quiet hours, `say` it. If that worked: send too, if `sending` and `also_send_when_said`. Done.
  3. Otherwise, if `sending`, `send` it. If that worked, done.
  4. Otherwise - it should have been sent but couldn't be, or there's no ntfy - and it isn't the phone's own:
     - outside quiet hours, `say` it;
     - in quiet hours, or if saying fails, hold it.
  5. The phone's own warning, not said in 2, is dropped. The phone warns the user itself.

- [ ] **Step 1: Write the failing tests** with a `FakeHome` answering `batteries()` and `people()`, and recording fakes for `say`, `send` and `quiet`. One test per row:

  | Test | User | Quiet | ntfy | Device | Expect |
  | --- | --- | --- | --- | --- | --- |
  | `test_at_home_by_day_its_said_and_sent` | home | no | ok | Mac | said, and sent |
  | `test_at_home_with_also_send_off_its_only_said` | home | no | ok | Mac | said |
  | `test_away_its_sent` | away | no | ok | Mac | sent |
  | `test_at_night_its_sent_never_said` | home | yes | ok | Mac | sent |
  | `test_unsure_where_the_user_is_its_sent` | none | no | ok | Mac | sent |
  | `test_a_failed_send_by_day_is_said` | away | no | fails | Mac | said |
  | `test_a_failed_send_at_night_is_held_then_said_if_still_true` | away | yes, then no | fails | Mac | held, then said once quiet ends |
  | `test_a_held_warning_no_longer_true_is_dropped` | away | yes, then no | fails | Mac charging by then | nothing |
  | `test_the_phones_own_warning_is_only_said` | home | no | ok | S25 | said, not sent |
  | `test_the_phones_own_warning_away_is_left_to_the_phone` | away | no | ok | S25 | nothing |
  | `test_with_no_satellite_its_sent` | home | no | ok | Mac, say → False | sent |

  Also:
  - `test_a_check_which_fails_doesnt_stop_the_next`;
  - `test_home_assistant_down_is_logged_once` (caplog);
  - `test_the_person_with_the_low_device_decides_home` - two people.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_batteries.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement `BatteryWatcher`.** Delivery is one method, `_deliver(warning: BatteryWarning, now: float)`, following the numbered rules, with a comment for each saying why.
- [ ] **Step 4: Run them again** - expected: they pass. Then run each new test 20 times.
- [ ] **Step 5: Commit** - "Warn about low batteries out loud and on the phone, never at night".

### Task 5: On the brain, and asking about batteries

**Files:**
- Modify:
  - `voice_assistant/brain.py` - `__init__`, `run`, `_clean_up`, and a new `_say_battery`;
  - `voice_assistant/actions.py` - `UNAVAILABLE`, `ActionExecutor.__init__`, and a new `get_batteries` tool;
  - `voice_assistant/session.py:177` - passing `batteries=brain.batteries`;
  - `voice_assistant/intents.py` - two rules, and `_batteries`;
  - `config.yaml` - the `batteries` section, with its comments;
  - `README.md` - a short "Batteries" part where Home Assistant's features are described;
  - `docs/troubleshooting.md` - "A battery warning didn't come".
- Test: `tests/test_brain.py`, `tests/test_actions.py` (or wherever the tools' tests are), `tests/test_intents.py`, `tests/test_config.py`.

**Interfaces:**
- Consumes:
  - `BatteryWatcher` and `BatteryChoices` from Tasks 3-4;
  - `HomeAssistant.from_config(config, token)`;
  - `Ntfy.send(topic, message, title=...) -> bool`, `Ntfy.phone` and `Ntfy.quiet()`;
  - `Brain.session_for(None)` and `Session.announce(message)`.
- Produces:
  - `Brain.batteries: Optional[BatteryWatcher]` - None without Home Assistant;
  - `Brain.home: Optional[HomeAssistant]` - the brain's own client, for the watcher;
  - the tool `get_batteries(device: Optional[str] = None)`, with `needs="batteries"`. It returns the spoken sentence for each battery `find` gives, or "I can't find a battery called that.";
  - `UNAVAILABLE["batteries"] = "Battery levels need Home Assistant."`.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_brain_watches_batteries_when_home_assistant_is_set_up` - and None without it;
  - `test_a_battery_warning_is_said_on_the_default_satellite` - and `_say_battery` returns False with no session;
  - `test_battery_questions_are_answered_locally`. These phrases each give `Action("get_batteries", ...)`:
    - "how's my mac's battery" → `{"device": "mac"}`;
    - "what's my phone on" → `{"device": "phone"}`;
    - "what's the tablet's battery" → `{"device": "tablet"}`;
    - "battery levels" and "how are my batteries" → `{}`;
  - `test_get_batteries_says_each_one_found`;
  - `test_get_batteries_with_home_assistant_down_says_so`;
  - `test_the_batteries_defaults_are_checked` - `Config()` accepts the new section, and refuses `mobile_marks: "five"`.
- [ ] **Step 2: Run them** - expected: they fail.
- [ ] **Step 3: Implement.**
  - The brain makes `self.home`, then `BatteryChoices` at `os.path.join(paths.DATA_DIR, "batteries.json")`, then `BatteryWatcher`, with:
    - `say=self._say_battery`;
    - `send=lambda message, title: self.ntfy.send(self.ntfy.phone, message, title=title)` when `self.ntfy and self.ntfy.phone`, else None;
    - `quiet=self.ntfy.quiet` when there's ntfy, else 23:00-07:00 from `config.yaml`'s ntfy defaults through the same function.
  - `run()` starts the watcher, and `_clean_up` stops it.
  - The tool and the local commands share `find` and `said`, so a phrasing gets the same answer either way.
- [ ] **Step 4: Run** the changed test files, then the full suite in London time and UTC. Expected: all pass.
- [ ] **Step 5: Commit** - "Watch batteries on the brain and answer questions about them", with the docs in the same commit.

### Task 6: The settings page's Batteries section

**Files:**
- Modify:
  - `voice_assistant/settings_page.py` - `batteries_state()`, `set_battery()` and `set_battery_also_send()`, a GET route `/api/batteries`, and POST routes `/api/batteries` and `/api/batteries/also`, all following the page's own patterns as they are after fixes batch 1;
  - `voice_assistant/web/settings.html` - the section, after the Home section;
  - `voice_assistant/web/settings.js` - `loadBatteries()` and `showBatteries(state)`, drawn with `drawIfChanged` like the others;
  - `voice_assistant/web/settings.css` - only what the existing classes don't already give.
- Test: `tests/test_settings_page.py`

**Interfaces:**
- Consumes: `Brain.batteries` and `BatteryChoices` from Tasks 3-5.
- Produces:
  - `SettingsPage.batteries_state() -> Dict`, which is `{"available": bool, "also_send_when_said": bool, "devices": [...]}`. Each device has:
    - `"id"`, `"name"`, `"level"`, `"charging"`;
    - `"stale"`: the age in words, or None;
    - `"kind"`;
    - `"enabled"`, `"marks"` and `"default_marks"`;
    - `"send_to_phone"`.

    Devices are in name order. With Home Assistant unreachable, it's `{"available": False, "message": ...}`.
  - `set_battery(battery_id: str, who: str, enabled=None, marks=None, send_to_phone=None) -> Dict` and `set_battery_also_send(on: bool, who: str) -> Dict`. Each gives `{"success": ..., "message": ...}`, and logs who changed what, as the page's other changes do.
  - `set_battery` finds the `Battery` for `battery_id` with `Brain.batteries.read()` and refuses, with a message, one Home Assistant can't show just now. It passes the `Battery` to `BatteryChoices.set`; a `ValueError` becomes the message. "Back to the defaults" calls `BatteryChoices.reset(battery_id)`, and `default_marks` fills each device's `"default_marks"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_batteries_are_listed_with_their_choices`;
  - `test_a_batterys_warnings_can_be_switched_off_and_it_works_straight_away` - the next `due` honours it, with no restart asked for;
  - `test_marks_which_dont_make_sense_are_refused` - `[150]`, `[5, 5]`, three marks for the Mac, and one for the Mac, each with the error sentence;
  - `test_also_send_when_said_can_be_switched_off`;
  - `test_the_section_says_when_home_assistant_cant_be_reached`;
  - `test_a_battery_change_needs_the_pages_own_header` - as the other POST routes do.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement the page methods and routes,** then the section. Each device is a row with:
  - its name, level and charging state, plus the age if it's stale;
  - a switch for warnings, and a switch for "Send to my phone";
  - one number box per mark, each 1-99%;
  - a "Back to the defaults" button, shown only once something's changed.

  At the top, the "Also send to my phone when Nova says it" switch. It reloads every minute, like the Home section, and saves on change with `change()`.
- [ ] **Step 4: Run** the page's tests and `node --check voice_assistant/web/settings.js`. Expected: pass and clean. Then run the full suite in London time and UTC, `tests/test_end_to_end.py` 5 times, ruff and `bash -n setup.sh update.sh uninstall.sh`.
- [ ] **Step 5: Commit** - "Choose each battery's warnings on the settings page". The look at 390 and 1440 wide is the user's to check, on their phone and a laptop, once it's deployed.
