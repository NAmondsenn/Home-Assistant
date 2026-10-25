# Settings that work straight away - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**The split.** Design 0009 is four parts that each ship alone, so it's four plans, built in this order:
1. **This plan** - settings checked for sense, used straight away with no restart, the safe settings and the speaking speed joining the page, and "Restart now" in the save bar (decisions 1, 2, 9, 12, and 14's save bar).
2. `2026-10-25-stop-undo-and-scripts.md` - Stop, Undo, and scripts asking first (3, 4, 5). It needs nothing from this plan.
3. `2026-10-25-satellites-on-the-page.md` - adding, removing and renewing satellites, each one's Spotify speaker, the longest request, and a routine's satellite (6, 7, 10, 11). It's the only plan that needs the Pi updated, and its longest request uses this plan's running settings.
4. `2026-10-25-calendars-sign-ins-and-the-phone.md` - Spotify's and Google's sign-ins, the Calendars and lists section, and the phone layout (8, 13, 14). It's last, as the phone layout sizes the buttons plans 2 and 3 add.

**Goal:** A setting saved on the page is checked for whether it makes sense, then used from its next use with no restart - except the few that make a connection, which say they wait for one. The sounds, web search, the conversation's thread, Spotify's pause, MQTT and the speaking speed join the page.

**Architecture:** `config.py` gains `LIMITS`, one rule for each setting's sense, used at start and on saving, and `Config.apply`, which swaps new values into the running settings. The page saves as now, then hands the "straight away" changes to `Brain.apply_settings`, which applies them and tells the few parts that copy a setting once (ntfy, the transcripts switch, Piper). Everything else that copied a setting at start reads it at use instead.

**Tech stack:** Python 3.13 on the brain, the settings page's own HTML, CSS and JavaScript, Piper's `--length_scale`, pytest. No new dependency.

**Spec:** `docs/decisions/0009-the-settings-page.md` - decisions 1, 2, 9, 12 and 14 (the save bar), and the table "Every setting on the page" except `thresholds.recording_timeout`, which is plan 3's. Read it before starting any task.

## Global constraints

- **Two kinds of setting, said on the page and after saving:** "Used straight away" or "Used once Nova restarts", exactly as the spec's table has them. A connection or a player waits: `music.player`, the three addresses, `navidrome.username`, `ntfy.alerts` and the three `mqtt.*`. Everything else in the table is straight away. Keys still wait, as now.
- **"Used straight away" must be true on every satellite, at each one's next use** - never a session holding a copy from when it started. Each straight-away setting has a test that it takes effect at its next use with no restart.
- **The ranges are the spec's table, exactly.** Out of range stops Nova starting with a message naming the setting, as a wrong kind does today; the page refuses it, naming the setting by its label.
- **The page's security stays as it is, never weaker:** it listens on 127.0.0.1 only, behind `tailscale serve`; only logins in `settings_page.allow` (the `Tailscale-User-Login` header) get in; every POST needs `X-Nova: page` and a same-origin `Sec-Fetch-Site`; `SAFETY_HEADERS` (the strict content policy, `no-store`) go with every answer; a body is at most `MOST_BODY`. Every new route goes through `ACTIONS` or `do_GET`, so it gets all of that.
- **One lock round every save:** `SettingsPage._saving` covers reading the file, writing it and changing the running settings (the spec's constraint on batch 1's lock).
- **Secrets stay on the brain.** Keys are only ever written, never shown.
- **Nothing in `config.yaml` names an entity.**
- **Hardware:** only the voice's choice is shaped by hardware, and it isn't in this plan (decision 12: each voice is about 60MB on PVE2's nearly full local-lvm, and the better ones need the AI machine's GPU). The speed is a Piper flag, so costs nothing.
- **The repo is public.** No hostname, IP address, email address, token or entity id carrying a hardware address in code, tests, docs or commits. Test addresses are `http://navidrome.local:4533` and the like.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'`. `settings.js` has no tests of its own: `node --check voice_assistant/web/settings.js`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **The satellite's 10 s recording cap** becomes the brain's "Longest request" (decision 11) in plan 3, `2026-10-25-satellites-on-the-page.md`, the only one of 0009's plans that needs the Pi updated - which is the user's to do. Nothing in this plan changes the satellite.
- **Safety:** never push or deploy; touch no machine except through those commands.

## Review focus

1. **A hand-edited settings file with an old value now out of range** - `silence_duration: 5` from before the limits - names the setting and stops the start, and `update.sh` refuses to restart into it rather than leaving Nova down. (Task 1, `test_an_out_of_range_value_stops_the_start_naming_it` and `test_update_checks_the_settings_before_stopping_nova`.)
2. **A save that's partly straight away and partly waits** - the night's start and the music player together - uses the first at once, says the second waits, and shows Restart. (Task 2, `test_a_mixed_save_uses_some_now_and_says_the_rest_waits`.)
3. **A session that started before the save** reads the new silence, sound or web search setting at its next request, on every satellite. (Tasks 2 and 3, `test_a_session_started_before_the_save_uses_the_new_value`.)
4. **Using a change fails** - ntfy can't be told - yet it's saved, the page says it's used at the next restart, and Restart shows. (Task 2, `test_a_change_that_cant_be_used_now_is_kept_for_the_restart`.)
5. **A speed change** never slows the next reply after the first: the waiting Piper is started again at the new speed straight away, and the kept phrases are dropped. (Task 4, `test_a_new_speed_starts_the_next_piper_at_once`.)

## Live checks before Task 1

For the controller, read-only, with the user's OK.

1. **The brain's own settings file** (`~/.config/nova/config.yaml` as the `nova` user), **and the Pi's** (`~/.config/nova/config.yaml` there, which the same `config.py` checks at its next start or update): which settings each sets, and whether each is inside the table's limits. Write down the names and "in range" or "out of range" only - no addresses. One out of range is put right by the user before this is deployed (decision 2).
2. **Piper on the brain takes `--length_scale`** (`models/piper/piper --help`), and how long a fresh Piper takes to load its voice there.

---

### Task 1: Settings are checked for whether they make sense

**Files:**
- Modify: `voice_assistant/config.py` (`LIMITS`, `limit_problem`, `clashes`, `check`); `config.yaml` (a `voice` section, `speed: 1.0`, with a comment); `update.sh` (a check before stopping the service).
- Test: `tests/test_config.py`.

**Interfaces:**
- Produces:
  - `Limit = NamedTuple("Limit", [("form", str), ("low", Optional[float]), ("high", Optional[float]), ("whole", bool)])`, with `form` one of "number", "time", "address", "topic", "topics" or "host".
  - `LIMITS: Dict[Tuple[str, str], Limit]`: `ntfy.quiet_from` and `quiet_until` time; `navidrome.url`, `home_assistant.url` and `ntfy.url` address; `ntfy.phone`, `reminders` and `restarts` topic; `ntfy.alerts` topics; `thresholds.silence_duration` 0.2 to 3; `conversation.history_max_age` 0 to 3600, whole; `mqtt.host` host; `mqtt.port` 1 to 65535, whole; `voice.speed` 0.75 to 1.5.
  - `limit_problem(section: str, name: str, value: Any) -> Optional[str]` - the end of a sentence, or None if it's fine:
    - number: "should be from 0.2 to 3", or "should be a whole number from 1 to 65535" (numbers written with `:g`);
    - time: "should be a time of day, from 00:00 to 23:59" - `HH:MM`, hours 00 to 23;
    - address: "should be an address starting http:// or https://" - empty is fine, as it means "not set";
    - topic and topics: "should be an ntfy topic: letters, digits, hyphens and underscores" - each 1 to 64 of `[A-Za-z0-9_-]`; an empty topic is fine, an empty list too;
    - host: "should be a host name or address, without http://" - empty, or only letters, digits, dots, hyphens and colons.
  - `check()` calls it for every setting in `LIMITS`, raising `ConfigError(f"{path}: '{section}.{name}' {problem}, not {value!r}")`. `REMOVED` settings still only warn, as now.
  - `clashes(data: Dict) -> List[Tuple[str, str]]` - settings that need each other, as (the key to name, the sentence), checked on saving only: `("navidrome.url", "Navidrome as the player needs Navidrome's address.")` when `music.player` is navidrome and `navidrome.url` is empty; `("ntfy.url", "ntfy's topics need its server.")` when any of `ntfy.alerts`, `phone`, `reminders` or `restarts` is set and `ntfy.url` is empty.
  - `update.sh`, before `stop_service`: runs `(cd "$PROJECT_DIRECTORY" && as_user "$PYTHON" -c 'from voice_assistant.config import Config; Config()')`, from the clone as `setup.sh:568` does, so it imports on the Pi too. If it fails it prints `problem "The new code is in place, but Nova's settings won't start it - put them right, then run this again. Nova is still running the old code until it next restarts:"`, the error, and exits 1 without stopping Nova.

- [ ] **Step 1: Write the failing tests:**
  - `test_each_limit_is_the_tables` - one in, and one just out, for each `LIMITS` entry: 0.2 and 3 pass, 0.19 and 3.1 fail; "23:59" passes, "24:00", "7:00" and "11pm" fail; `mqtt.port` 1883 passes, 0, 65536 and 1883.5 fail; "https://ha.local:8123" passes, "ha.local" fails, "" passes; "home-lab_1" passes, "home lab" and "a/b" fail; `mqtt.host` "192.168.1.20" passes, "http://ha.local" fails;
  - `test_an_out_of_range_value_stops_the_start_naming_it` - `{"thresholds": {"silence_duration": 5}}` raises `ConfigError` with "'thresholds.silence_duration' should be from 0.2 to 3, not 5";
  - `test_the_same_quiet_hours_are_no_quiet_hours` (a pin - it's true today) - "22:00" to "22:00" passes the check, and `Ntfy.quiet()` is False all day;
  - `test_settings_that_need_each_other_are_named` - the two `clashes` sentences, and none when both are set;
  - `test_a_removed_setting_still_only_warns` - `weather.location` with the limits in place;
  - `test_battery_marks_are_from_1_to_99` (a pin) - 0007's marks already go through `marks_problem`; 0 and 100 are refused at start;
  - `test_update_checks_the_settings_before_stopping_nova` - `grep` in a pytest of `update.sh`'s text: the `Config()` check comes before the first `stop_service` call.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_config.py`. Expected: the new tests fail, with `ImportError` for `limit_problem` and `clashes` - the two pins only through that import, and they pass once it's there.
- [ ] **Step 3: Implement** as the Interfaces say, then `bash -n update.sh`.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Check settings for whether they make sense, at start and before an update restarts Nova".

### Task 2: A saved change goes into the running settings

**Files:**
- Modify: `voice_assistant/config.py` (`Config.apply`); `voice_assistant/settings_page.py` (`EDITABLE` gains a "used" field, `state`, `save_settings`, the module docstring); `voice_assistant/brain.py` (`apply_settings`, and the battery watcher's night kept as `self._night`); `voice_assistant/ntfy.py` (`Ntfy.retopic`); `voice_assistant/session.py` (`thresholds` becomes a property).
- Test: `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_brain.py`, `tests/test_ntfy.py`, `tests/test_session.py`.

**Interfaces:**
- Consumes: `limit_problem` and `clashes` from Task 1.
- Produces:
  - `Config.apply(changes: Dict[Tuple[str, str], Any])` - builds a copy of `data` with the changes and swaps it in with one assignment, under a lock of its own, so a reader sees all of a change or none. `section()` reads the current `data`.
  - `EDITABLE` entries become `(section, name, label, used)`, with `used` "now" or "restart" as the table says. `NOW = "Used straight away"`, `LATER = "Used once Nova restarts"`.
  - `state()` gives each setting `"used"` (`NOW` or `LATER`) and, from `LIMITS`, `"form"`, `"min"`, `"max"` and `"whole"` where it has them.
  - `save_settings(changes, who)`, under `_saving`, in this order:
    1. each value as now, then `limit_problem`: refused as `f"{label} {problem}."` - "Seconds of quiet which end what you're saying should be from 0.2 to 3.";
    2. `check()` and `clashes()` on the whole file, the latter as its sentence;
    3. the file written, as now;
    4. the "now" changes that differ from the running settings go to `self.brain.apply_settings(...)`.
  - The answer: `"Saved. It's used straight away."` when nothing waits; `"Saved. It's used once Nova restarts."` when all of it waits; else `f"Saved. {labels} {'is' if one else 'are'} used once Nova restarts, and the rest straight away."`. Anything `apply_settings` couldn't use adds `" Some of it couldn't be used straight away, so it's used once Nova restarts."`, and the answer has `"restart_needed": True`.
  - `restart_needed` in `state()` is true for a key set, for a "restart" setting that differs from the running one, or for a "now" one the brain couldn't use - never for a "now" one that's been used.
  - `Brain.apply_settings(changes: Dict[Tuple[str, str], Any]) -> List[Tuple[str, str]]` - applies them to `self.config` with `Config.apply`, then tells the parts that copy a setting once, each in its own `try`, logging a failure. It gives back those it couldn't tell. It tells:
    - `ntfy.phone`, `reminders`, `restarts`, `quiet_from` and `quiet_until`: `self.ntfy.retopic(...)`, and `self._night`'s hours, which the battery watcher's quiet reads when there's no ntfy;
    - `logging.log_transcripts`: `logging_setup.log_transcripts(value)`.
  - The battery watcher's `send` (`brain.py:304-307`) is always given, and reads `self.ntfy.phone` at each use, answering False when there's no ntfy or the topic is empty - so a phone topic set or cleared on the page reaches battery warnings straight away.
  - `Ntfy.retopic(phone: str, reminders: str, restarts: str, quiet_from: str, quiet_until: str)` - under its lock; the alert topics it listens to don't change.
  - `Session.thresholds` is a property, `self.brain.config.section("thresholds")`, so the silence setting is read at each recording.

- [ ] **Step 1: Write the failing tests:**
  - `test_apply_changes_only_what_it_is_given` - and a section read before still has the old values, one read after the new;
  - `test_a_straight_away_change_is_used_with_no_restart` - saving `ntfy.quiet_from` "22:00" makes `brain.ntfy.quiet_from` "22:00", and `state()["restart_needed"]` is False;
  - `test_a_mixed_save_uses_some_now_and_says_the_rest_waits` - `ntfy.quiet_from` and `music.player` together: the first is running, the second isn't, and the message is "Saved. What plays the music is used once Nova restarts, and the rest straight away.";
  - `test_a_change_that_cant_be_used_now_is_kept_for_the_restart` - a `retopic` that raises: the file has it, the message says so, and `restart_needed` is True;
  - `test_an_error_names_the_setting_as_the_page_does` - `thresholds.silence_duration` 5 gives "Seconds of quiet which end what you're saying should be from 0.2 to 3." and nothing is written;
  - `test_settings_that_need_each_other_are_refused_on_saving` - `music.player` navidrome with no address;
  - `test_each_setting_says_how_its_used_and_its_limits` - `state()` has `"used": "Used straight away"` and `"min": 0.2, "max": 3` for silence, and `"form": "time"` for the night;
  - `test_a_session_started_before_the_save_uses_the_new_value` - two sessions; saving silence 1.2 gives `record_until_silence` `silence_duration=1.2` on both at their next recording;
  - `test_the_transcripts_switch_is_used_straight_away` - `transcripts_logged()` follows the save;
  - `test_a_phone_topic_set_on_the_page_gets_battery_warnings_straight_away` - set after start, the next warning is sent to it; cleared, nothing is posted and `send` is False;
  - `test_two_quick_saves_both_reach_the_running_settings` - two threads saving different settings at once both end up in the file and running.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_config.py tests/test_settings_page.py tests/test_brain.py tests/test_ntfy.py tests/test_session.py -k "apply or straight or mixed or used_now or names_the_setting or need_each_other or limits or started_before or transcripts_switch or quick_saves or phone_topic"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say. The module docstring no longer says every setting waits for a restart.
- [ ] **Step 4: Run them again** - expected: pass. Then the five files whole, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Use a saved setting straight away, unless it makes a connection".

### Task 3: The safe settings join the page

**Files:**
- Modify: `voice_assistant/settings_page.py` (`EDITABLE`); `voice_assistant/session.py` (the three sounds become properties); `voice_assistant/conversation.py` (`chime_enabled`); `voice_assistant/llm.py` (`history_max_age`, `web_search_enabled`); `voice_assistant/spotify_controller.py` (`pause_while_speaking`); `README.md` (each service's steps send the user to Keys and Settings, with the files as the fallback).
- Test: `tests/test_settings_page.py`, `tests/test_session.py`, `tests/test_conversation.py`, `tests/test_llm.py`, `tests/test_spotify.py`.

**Interfaces:**
- Consumes: `EDITABLE`'s "used" field and `Brain.apply_settings` from Task 2.
- Produces:
  - `EDITABLE` gains, with the table's labels: `sounds.listening_enabled` "The blip when Nova's listening", `sounds.chime_enabled` "The chime for done", `sounds.reminder_enabled` "The reminder sound", `web_search.enabled` "Search the web when asked to", `conversation.history_max_age` "Seconds a conversation's thread is kept", `spotify.pause_while_speaking` "Pause Spotify while Nova talks" - all "now" - and `mqtt.host` "Home Assistant's MQTT host", `mqtt.port` "Its port", `mqtt.username` "Nova's MQTT user" - all "restart".
  - Each straight-away one is read at its use from the running settings:
    - `Session.chime_enabled`, `reminder_sound_enabled` and `listening_sound_enabled` are properties over `self.brain.config.section("sounds")`;
    - `Conversation(..., chime_enabled: Union[bool, Callable[[], bool]] = True)`, with `self.chime_enabled` a property that calls it if it's callable. The session passes `lambda: self.chime_enabled`;
    - `LLMHandler.history_max_age` and `web_search_enabled` are properties over the config it was given, falling back to the constructor's values without one;
    - `SpotifyController.pause_while_speaking` is a property over its config's `spotify` section.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_safe_settings_are_on_the_page` - each new key is in `state()["settings"]` with its label and how it's used;
  - for each of the six straight-away settings, a test that a session (or handler, or controller) made before the change uses the new value at its next use: `test_the_listening_blip_is_switched_off_straight_away`, `test_the_done_chime_is_switched_off_straight_away`, `test_the_reminder_sound_is_switched_off_straight_away`, `test_web_search_is_switched_off_straight_away` (the next request has no `web_search` tool), `test_a_shorter_thread_is_used_at_the_next_conversation`, `test_spotify_pausing_is_switched_off_straight_away`;
  - `test_mqtt_waits_for_a_restart` - saving `mqtt.host` gives "Saved. It's used once Nova restarts." and `restart_needed`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py tests/test_session.py tests/test_conversation.py tests/test_llm.py tests/test_spotify.py -k "safe_settings or straight_away or shorter_thread or mqtt_waits"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Put the sounds, web search, the conversation's thread, Spotify's pause and MQTT on the page".

### Task 4: The speaking speed

**Files:**
- Modify: `voice_assistant/text_to_speech.py` (`speed`, `set_speed`, `_start_piper`, `stream`); `voice_assistant/brain.py` (`apply_settings` tells Piper and remakes the phrase); `voice_assistant/session.py` (`hear_voice`); `voice_assistant/settings_page.py` (`EDITABLE`, a `hear_voice` route).
- Test: `tests/test_text_to_speech.py`, `tests/test_brain.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: `Brain.apply_settings` from Task 2, and `voice.speed` from Task 1.
- Produces:
  - `TextToSpeech.speed: float`, from `voice.speed` at start (1.0 without it), and `_start_piper(speed: Optional[float] = None)` passes `--length_scale` `1 / speed` (Piper's flag: below 1 is faster).
  - `TextToSpeech.set_speed(speed: float)` - under its lock: keeps the speed, empties `_recent` and takes the waiting process; then, with the lock let go (`_prepare_next` takes the same lock, which isn't reentrant), kills that process and calls `_prepare_next()`, so the next reply finds one already loaded at the new speed. `_take_piper`'s fresh process uses `self.speed` too.
  - `_remember` keeps a phrase only if the speed it was made at is still `self.speed`, so a reply under way at the old speed isn't kept after a change.
  - `TextToSpeech.stream(text, on_start=None, remember=True, speed: Optional[float] = None)` - a speed other than its own uses a one-off process and remembers nothing.
  - `Brain.apply_settings` for `voice.speed`: `self.tts.set_speed(value)`, then `self.phrase = self._make_phrase()`, sent to each connected satellite with `send_phrase`.
  - `EDITABLE` gains `voice.speed` "Speaking speed", "now", shown as a slider from 0.75 to 1.5 (Task 5).
  - `HEAR_IT = "This is how fast I'll talk."` and `Session.hear_voice(speed: float) -> bool` - says it on the satellite, at that speed, as a "reply" stream, through the same path as `_say`, without the listening that follows. False if the satellite's busy in a conversation.
  - `SettingsPage.hear_voice(speed, who, satellite=None) -> Dict` and `POST /api/voice/hear` `{"speed": ..., "satellite": ...}`. The speed is checked with `limit_problem("voice", "speed", ...)`; "That's being said on {name}." or "{name} is busy just now - try again in a moment.".

- [ ] **Step 1: Write the failing tests** with a fake `subprocess.Popen` recording its arguments:
  - `test_piper_speaks_at_the_chosen_speed` - speed 1.25 gives `--length_scale 0.8`;
  - `test_a_new_speed_starts_the_next_piper_at_once` - after `set_speed(1.25)`, the old waiting process was killed, a new one started with `0.8`, and `_recent` is empty;
  - `test_a_reply_under_way_at_the_old_speed_isnt_kept` - `set_speed` while a `stream` is part way leaves `_recent` empty after it ends;
  - `test_hear_it_speaks_at_the_sliders_speed_without_changing_nova` - `stream(..., speed=0.75)` starts a one-off process with `1.3333`, and `tts.speed` is unchanged;
  - `test_the_cant_reach_phrase_is_made_again_at_the_new_speed` - each connected fake satellite is sent a new phrase;
  - `test_hear_it_is_said_on_the_satellite_looked_after` and `test_hear_it_refuses_a_speed_out_of_range`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_text_to_speech.py tests/test_brain.py tests/test_settings_page.py -k "speed or hear_it or phrase"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 5: Commit** - "Let the speaking speed be chosen on the page, used from the next reply".

### Task 5: The page shows how each setting is used, and offers Restart where the thumb is

**Files:**
- Modify: `voice_assistant/web/settings.js` (`GROUPS`, `settingField`, `valueInput`, the save handler); `voice_assistant/web/settings.html` (the save bar's Restart button); `voice_assistant/web/settings.css`.
- Test: none of its own - `node --check voice_assistant/web/settings.js`, and `tests/test_settings_page.py` for the page's files still being served.

**Interfaces:**
- Consumes: each setting's `used`, `form`, `min`, `max` and `whole` from Task 2, and the save's `restart_needed`.
- Produces:
  - `GROUPS`: "Weather" goes; Music has `music.`, `navidrome.` and `spotify.`; "Voice" has `voice.`; "Sounds" has `sounds.`; "Claude" has `web_search.` and `conversation.`; Home has `home_assistant.` and `mqtt.`; Notifications, Listening and Privacy as now.
  - Each field shows its `used` under its label, in the detail style the page already has.
  - A number gets `min`, `max`, `step` (1 when `whole`, else 0.05) and `inputmode` "numeric" or "decimal"; a time is `type="time"`; an address is `type="url"` with `inputmode="url"`; `voice.speed` is a range slider showing its value as "1.25 times", with a "Hear it" button beside it sending `/api/voice/hear` with the slider's value.
  - The night's two fields show "No quiet hours" beside them while they're the same.
  - After a save whose answer has `restart_needed`, the save bar shows "Restart now" in place of "Save settings", asking as the masthead's Restart does. The masthead's notice stays.
  - An error from saving is shown in the save bar as well as the toast.

- [ ] **Step 1: Make the changes**, in the page's existing style. There's no RED here: the page has no JavaScript tests, so the commit says so.
- [ ] **Step 2: Run** `node --check voice_assistant/web/settings.js` and `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py`. Expected: clean, and pass.
- [ ] **Step 3: Commit** - "Show on the page how each setting is used, and offer Restart in the save bar".

The look at 390 and 1440 wide is the user's to check once it's deployed.
