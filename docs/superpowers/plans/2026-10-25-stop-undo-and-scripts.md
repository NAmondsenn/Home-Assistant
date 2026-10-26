# Stop, Undo, and scripts that ask first - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The page's masthead shows Stop while anything rings or Nova is speaking, and it stops them without deleting an alarm. Forgetting a memory, or removing an alarm, timer or reminder, can be undone for 10 seconds. Every script asks before it runs, and each one Nova runs by voice says so.

**Architecture:** `Brain.stop_everywhere` runs Home Assistant's own Stop code on each satellite where something rings or speaks, and the page's quick check (`/api/live`) says whether there's anything to stop. A new `voice_assistant/undo.py` keeps what was removed for a minute, behind a one-use token, and `Clock.put_back` and `Memory.put_back` restore it. The scripts' confirm is the page's, backed by the brain refusing a script run without it.

**Tech stack:** Python 3.13 on the brain, the settings page's HTML, CSS and JavaScript, pytest. No new dependency.

**Spec:** `docs/decisions/0009-the-settings-page.md` - decisions 3, 4 and 5, and the user's decision 3. Read it before starting any task. This is the second of 0009's four plans (the split is said at the top of `2026-10-25-settings-straight-away.md`), and it needs nothing from the first.

## Global constraints

- **Stop does what Home Assistant's Stop does, through the same code** (`Session._stop_from_home_assistant`, renamed below): it dismisses what rings and stops what Nova says, on each connected satellite where something is. A ringing alarm is dismissed, never deleted, so a repeating one rings tomorrow. The music carries on.
- **Undo puts back exactly what went:** the same fact in the same place, or the same alarm, timer or reminder with its time, repeat, label and satellite, whose copy is sent to the satellite again. A timer or one-off alarm whose time has passed isn't put back. The page offers Undo for 10 seconds (`UNDO_SHOWN = 10000` ms); the brain keeps it 60 seconds (`UNDO_KEPT = 60.0`), in memory only, never across a restart. Deleting a routine and restarting still ask first, as now.
- **Scripts ask first; scenes stay one tap.** A script or scene the guard refuses is never offered (as today, through `works_guarded` in `HomeAssistant.overview`). Every script is shown; there's no switch to hide one. Exposing a script is done in Home Assistant.
- **The page's security stays as it is, never weaker:** 127.0.0.1 only, behind `tailscale serve`; only `settings_page.allow`'s logins; every POST needs `X-Nova: page` and a same-origin `Sec-Fetch-Site`; `SAFETY_HEADERS` on every answer; `MOST_BODY`. New routes go through `ACTIONS` and `do_GET`. An undo token is unguessable (`secrets.token_urlsafe(16)`) and works once.
- **What's removed is never logged in full** - the clock and memory log as they do now, with `extra=TRANSCRIPT`.
- **The repo is public.** No hostname, IP address, email address or token in code, tests, docs or commits.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'`. `node --check voice_assistant/web/settings.js` for the page's script.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **The satellite's 10 s recording cap** becomes the brain's "Longest request" (decision 11) in plan 3, `2026-10-25-satellites-on-the-page.md`, the only one of 0009's plans that needs the Pi updated - which is the user's to do. Nothing in this plan changes the satellite.
- **Safety:** never push or deploy; touch no machine except through those commands.

## Review focus

1. **Stop on a repeating alarm** dismisses today's ring and leaves tomorrow's due. (Task 1, `test_stop_never_deletes_a_repeating_alarm`.)
2. **Undo after the time has passed** - a timer that would have finished, a one-off alarm whose minute has gone - isn't put back to ring at once, and the page says why. (Task 2, `test_a_timer_whose_time_has_passed_isnt_put_back`.)
3. **Undo used twice, or after a minute, or after a restart** puts nothing back a second time. (Task 2, `test_an_undo_works_once_and_not_after_a_minute`.)
4. **Undo of an alarm when another has been set in the same minute meanwhile** refuses, rather than making two that can't be told apart. (Task 2, `test_an_alarm_clashing_with_one_set_since_isnt_put_back`.)
5. **A script run without the page's yes** - a stray POST, or an old tab - is refused by the brain. (Task 4, `test_a_script_runs_only_with_a_yes`.)

## Live checks before Task 1

None - everything here is the brain's own, and Home Assistant's exposure is already read by 0008's registry.

---

### Task 1: Stop, on the page

**Files:**
- Modify: `voice_assistant/session.py` (`_stop_from_home_assistant` becomes `stop_what_rings_and_speaks`, and `_device_controls` passes it); `voice_assistant/brain.py` (`stoppable`, `stop_everywhere`); `voice_assistant/settings_page.py` (`live`, a `stop` action); `voice_assistant/web/settings.html`, `settings.js` and `settings.css` (the masthead's Stop).
- Test: `tests/test_session.py`, `tests/test_brain.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `Session.stop_what_rings_and_speaks()` - the old `_stop_from_home_assistant`, unchanged inside; Home Assistant's Stop calls it.
  - `Brain.stoppable() -> bool` - whether any connected satellite has something ringing (`clock.is_ringing(satellite=name)`) or a session whose `activity` is "speaking".
  - `Brain.stop_everywhere() -> List[str]` - calls `stop_what_rings_and_speaks()` on each of those sessions, and gives back their names.
  - `SettingsPage.live()` gains `"stoppable": self.brain.stoppable()`.
  - `SettingsPage.press_stop(who) -> Dict` and `POST /api/stop` - never `stop()`, which already shuts the page's server down and stays as it is: "Stopped." or "Nothing's ringing or speaking."; logged as `f"Settings page: {who} pressed Stop"`.
  - The masthead's Stop button shows while `stoppable`, from the quick check every 1.5 s, and goes by itself.

- [ ] **Step 1: Write the failing tests:**
  - `test_stop_silences_what_rings_and_what_nova_says_on_each_satellite` - two sessions, one ringing and one speaking: both are stopped, and a third, idle, isn't touched;
  - `test_stop_never_deletes_a_repeating_alarm` - a daily alarm ringing: after Stop, `clock.overview()` still has it, due tomorrow;
  - `test_stop_leaves_the_music_playing` - `speaker.stop` is only given `["reply", "announcement"]`;
  - `test_the_quick_check_says_when_theres_something_to_stop` - `live()["stoppable"]` is True while ringing and False after;
  - `test_home_assistants_stop_is_the_same_code` - the device controls' `stop` is `stop_what_rings_and_speaks`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_session.py tests/test_brain.py tests/test_settings_page.py -k "stop"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the button, big enough for a thumb (at least 44 px).
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Add Stop to the page, doing what Home Assistant's Stop does".

### Task 2: Undo removing an alarm, timer or reminder

**Files:**
- Create: `voice_assistant/undo.py`, with a module docstring saying what it keeps and for how long (0009 decision 4).
- Modify: `voice_assistant/clock.py` (`remove` gives back what it removed; a new `put_back`); `voice_assistant/settings_page.py` (`remove`, an `undo` action); `voice_assistant/web/settings.js` and `settings.css` (the toast's Undo button).
- Test: `tests/test_undo.py` (new), `tests/test_clock.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `UndoShelf(keep_for: float = UNDO_KEPT, clock: Callable[[], float] = time.monotonic)`, with `put(kind: str, item: Any) -> str` (a new token) and `take(token: str) -> Optional[Tuple[str, Any]]` - once only, and None after `keep_for`. Old entries are dropped as new ones are put. A lock guards it.
  - `Clock.remove(kind, item_id)` gives back, as well as now, `"removed": <a deep copy of the item>` - for the page alone, never sent to it.
  - `Clock.put_back(kind: str, item: Dict, now: Optional[float] = None) -> Dict`, inside `_changing()` so the satellite is sent its copy again:
    - a timer or reminder whose `due_at` has passed: `{"success": False, "message": "Its time has passed, so it isn't put back."}`;
    - a one-off alarm whose `due_at` has passed: the same. A repeating one is put back at its next occurrence;
    - an alarm or timer that clashes with one set since, by the same-minute check `set_alarm` or `set_timer` makes: "You already have a ..." as each says it;
    - else it's back under its own id if that's free, else a new one, with its time, repeat, label and satellite: "Put back the {name}.".
  - `SettingsPage.remove(kind, item_id, who)` takes `removed` out of the clock's answer, puts it on its `UndoShelf`, and answers with the message and `"undo": <token>` only.
  - `SettingsPage.undo(token, who) -> Dict` and `POST /api/undo` `{"token": ...}` - "That can't be undone now." for a token unknown, used or too old.
  - The toast shows an Undo button for `UNDO_SHOWN` ms after a removal, which sends the token.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_undo_works_once_and_not_after_a_minute` - `take` twice gives the item then None; a fake clock past 60 s gives None; a new `UndoShelf` (a restart) has nothing;
  - `test_undo_puts_back_the_same_alarm` - a weekday 07:00 alarm on "bedroom" comes back with its id, repeat and satellite, and `on_change` is called so the satellite gets its copy;
  - `test_undo_puts_back_the_same_timer_or_reminder` - its label, satellite and `due_at`, unchanged;
  - `test_a_timer_whose_time_has_passed_isnt_put_back` - and a one-off alarm likewise, with the message;
  - `test_a_repeating_alarm_comes_back_at_its_next_time` - its minute has passed today, so it's due tomorrow, never now;
  - `test_an_alarm_clashing_with_one_set_since_isnt_put_back` - and a timer likewise;
  - `test_removing_on_the_page_offers_an_undo` - the answer has a token and no item; `/api/undo` with it puts the alarm back; with it again, "That can't be undone now.".
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_undo.py tests/test_clock.py tests/test_settings_page.py -k "undo or put_back or passed or clashing"`. Expected: they fail, with `ModuleNotFoundError: No module named 'voice_assistant.undo'`.
- [ ] **Step 3: Implement** as the Interfaces say, then the toast's button.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Let a removed alarm, timer or reminder be undone for a little while".

### Task 3: Undo forgetting a memory

**Files:**
- Modify: `voice_assistant/memory.py` (`forget` says what it forgot, and where; a new `put_back`); `voice_assistant/settings_page.py` (`forget`, `undo`).
- Test: `tests/test_memory.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: `UndoShelf` and `SettingsPage.undo` from Task 2.
- Produces:
  - `Memory.forget(what)` gives back, on success, `"forgotten": <the fact>` and `"at": <its index>` as well. What's said is unchanged. The voice path's `forget` action takes both out before its result reaches Claude or a log.
  - `Memory.put_back(fact: str, at: int) -> Dict` - puts it back at `min(at, len(facts))`. If the same fact has been remembered since, it isn't added twice ("I already knew that."). With the memory full: "My memory's full, so it can't be put back.". Else "Put it back.".
  - `SettingsPage.forget` takes `forgotten` and `at` out, shelves `("fact", (fact, at))`, and answers with the message and `"undo"` only; `undo` sends a fact to `Memory.put_back`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_forgotten_fact_goes_back_in_its_place` - three facts, the middle one forgotten and put back, in the middle;
  - `test_a_fact_remembered_again_meanwhile_isnt_put_back_twice`;
  - `test_a_full_memory_says_it_cant_put_one_back`;
  - `test_forgetting_on_the_page_offers_an_undo` - and the answer has no `forgotten` or `at`;
  - `test_forgetting_by_voice_tells_claude_nothing_more` - the action's result has neither.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_memory.py tests/test_settings_page.py -k "put_back or forgotten or forgetting or remembered_again or full_memory"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Let a forgotten memory be undone for a little while".

### Task 4: Scripts ask first, and say which Nova runs by voice

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`overview` marks each one exposed); `voice_assistant/settings_page.py` (`run` needs a yes for a script); `voice_assistant/web/settings.js` (the confirm, and the "By voice" mark).
- Test: `tests/test_home_assistant.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: 0008's `Snapshot.exposed(entity_id)`.
- Produces:
  - Each of `overview()`'s routines gains `"by_voice": bool` - exposed to Assist in the snapshot; False without one.
  - `SettingsPage.run(entity_id, who, satellite=None, confirmed: bool = False)`; `/api/run` passes `body.get("confirmed", False)`. A `script.*` without `confirmed is True` is refused, `{"success": False, "message": "A script only runs once you've said yes."}`, before Home Assistant is asked. A scene runs as now.
  - The page asks `confirm(`Run ${name}?`)` before a script, and sends `confirmed: true` only on a yes. A script that's exposed shows "By voice" beside its name.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_script_runs_only_with_a_yes` - without `confirmed`, nothing reaches Home Assistant; with it, `script.turn_on` is called;
  - `test_a_scene_still_runs_with_one_tap`;
  - `test_each_script_says_whether_nova_runs_it_by_voice` - an exposed one is `by_voice: True`, one that isn't is False, and with no snapshot all are False;
  - `test_a_guarded_script_is_still_never_offered` - 0008's check, pinned for the page.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_home_assistant.py tests/test_settings_page.py -k "script or scene"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Ask before running a script from the page, and mark those Nova runs by voice".
