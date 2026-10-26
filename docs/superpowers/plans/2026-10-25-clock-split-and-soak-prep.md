# Splitting the clock, and getting ready for the soak - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Where this sits.** The last work before the 72-hour soak. The user, 23 October: "split clock.py before the soak. the soak should kinda be the final thing". So: the split (Tasks 1-4, a move and nothing else), the few fixes a soak would trip over (Tasks 5-8), then the soak itself, by the checklist at the end.

**Goal:** `voice_assistant/clock.py` (2,521 lines) becomes five focused files with no change in behaviour, the four things a 72-hour unattended run would trip over are put right, and the soak can watch the brain as well as the Pi.

**Architecture:** `Clock` stays one class, with one lock and one set of stores, so it's split the only way that moves code without changing it: four mixin classes, each in its own module, which `Clock` inherits. Every method moves byte for byte and is still reached as `self.x` and `Clock.x`. The stores, their files and the tick stay in `clock.py`, which re-exports every name callers import today, so no caller and no test changes.

**Tech stack:** Python 3.13, pytest, bash. No new dependency.

**Spec:** the user's words above; `.superpowers/sdd/2026-10-22-the-pi-as-a-satellite/audit-quality.md` findings 5 (the soak can't see the brain) and 13 (the clock's split); and the ledger items quoted in each of Tasks 5-7. There's no design document.

## Global constraints

- **Tasks 1-4 are a move only.** The same messages, log lines word for word, order of checks, exceptions and saved files. No test changes in them; they only add the pins Task 1 names.
- **Nothing outside the clock's files changes for the split.** Every `from voice_assistant.clock import ...` and `clock_module.X` in `voice_assistant/` and `tests/` keeps working: `Clock`, `format_duration`, `spoken_clock_time`, `parse_repeat`, `seconds_until`, `_time_of_day_candidates`, `TIMERS_FILE`, `ALARMS_FILE`, `PLAUSIBLE_TIME_AFTER`, `ALARM_START_LEVEL`, `ALARM_SOUND`, `storage`, `time`.
- **The tests patch `clock_module.TIMERS_FILE` and `ALARMS_FILE`, so whatever reads them stays in `clock.py`** - `_save`, `_load`, `_save_alarms` and `_load_alarms`. Moved elsewhere, they'd read their own module's copy, and the tests would write into the real data folder.
- **The Pi's code doesn't change.** `satellite.py` and everything it imports (`fallback`, `speaker`, `satellite_link`, `wake_word`, `config` and the rest) never import the clock. The satellite only sees `copy_for`'s list and gives back its ids, and that code moves unchanged. So the split needs nothing on the Pi, and today's Pi works with the split brain. Only Task 5 might touch `satellite.py`. If it does, the Pi needs updating, which is the user's to do.
- **Hardware:** nothing here is shaped by hardware. The soak's memory mark is read against the brain container's 4 GB. Log rotation (5 MB, three old ones) and `latency.jsonl`'s keep both machines' disks safe for 72 hours: the brain's 8 GB root, and the Pi's SD card.
- **The repo is public.** No hostname, IP address, email address or token in code, tests, docs or commits.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. That means full-sentence comments saying why, Google docstrings, type hints, LF, British English, and spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'`. Shell: `bash -n update.sh`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage files by name.
- **Safety:** never push or deploy, and never run `git stash`. Touch no machine except through those commands. Deploying the brain, updating the Pi and starting the soak are the user's.

## Review focus

1. **A test's patched file path still catches every save** after the move, so no test run writes into the real `timers.json`. (Task 1, `test_the_clock_still_saves_where_the_tests_point_it`.)
2. **Every log line keeps its logger's name**, `voice_assistant.clock`. The format prints it, and `caplog` and anyone grepping the logs go by it. (Task 1, `test_every_part_of_the_clock_logs_as_the_clock`.)
3. **A wake-up alarm on a satellite removed mid-ring keeps ringing on the default**, even when the default's same daily alarm was set only minutes ago - and one the default rang, skipped, or rang from its own copy while the brain was off, doesn't ring again. (Task 6, its first test and its three pins.)
4. **An update cut off mid-step - the phone's SSH session dropped during `pip install` - never leaves a reboot to start the new code.** (Task 7, `test_an_update_cut_off_mid_step_takes_the_new_code_back_out`.)
5. **The brain's monitor run as the wrong user reads an empty logs folder and reports nothing wrong.** It refuses to start instead. (Task 8, `test_a_missing_log_stops_the_monitor_before_it_starts`.)

## Live checks before Task 8

For the controller, read-only, with the user's OK. On the brain, as the `nova` user with its own home (`sudo -iu nova`, or `sudo -u nova -H` if sudo allows only that):
- does `systemctl show -p MainPID,NRestarts --value nova-backend` answer without root?
- is `/proc/<that pid>/fd` readable?
- where's the brain's venv Python (setup.sh's `$PYTHON`)?

---

## The move, for Tasks 1-4

- **Mixins.** Each new module holds one class with no `__init__` and no state of its own. Its docstring says it's part of `Clock`, using the lock and stores `clock.py` sets up. `class Clock(ClockRequests, Ringing, SatelliteCopies, ClockWords)`. No name is defined twice and no method uses `super()`, so the order of those doesn't matter.
- **What moves, moves as it is:** docstrings, comments, decorators and blank lines, in the order it was in `clock.py`. Nothing is renamed, the underscores included (`tests/test_clock.py` imports `_time_of_day_candidates`). Unlike plan 0008's Task 5b, a moved private name keeps its underscore.
- **Imports go one way:** `clock.py` imports from the four, and `clock_ringing`, `clock_copies` and `clock_requests` may import from `clock_words`, and `clock_copies` from `clock_ringing`. None of them imports `clock.py`.
- **Each module that logs** has `logger = logging.getLogger("voice_assistant.clock")`, with a comment that it's so every line keeps the name it had.
- **`clock.py` re-exports** what callers import and no longer uses itself, in one import marked `# noqa: F401` with a comment saying why, as `microphone.py:30` does.
- **Its module docstring** gains one paragraph naming the four parts and what each holds.
- **The check, after each task:** `python3 .superpowers/tools/clock_split_check.py 1ffccab` on the CT. It's a local script, git-ignored, and the controller writes it before Task 1. It parses the old `clock.py` (`git show 1ffccab:voice_assistant/clock.py`) and the new `clock.py` and `clock_*.py` with `ast`. Every module-level function and constant, and every name in `Clock`'s body, must be defined exactly once in the new files (at module level, or in `Clock` or a mixin), with `ast.dump` equal to the old one. Module-level statements that aren't definitions (`REPEAT_WORDS.update(...)`, `clock.py:41`) are compared too, each found once with its `ast.dump` unchanged. There must be no new names except the mixin classes, their docstrings and `logger`. It prints where each name went and exits 1 on any difference. Comments aren't in the AST, so `git diff -M --color-moved=zebra` shows they moved too.

### Task 1: The clock's words in a module of their own

**Files:**
- Create: `voice_assistant/clock_words.py`.
- Modify: `voice_assistant/clock.py`.
- Test: `tests/test_clock.py` (four pins).

**Interfaces:**
- Produces, in `clock_words.py`:
  - at module level: `WEEKDAY_NAMES`, `REPEAT_WORDS` with its update, `format_duration`, `spoken_clock_time`, `parse_repeat`, `seconds_until`, `_duration_units` and `_time_of_day_candidates`;
  - in `class ClockWords`: the six word sets (`DISMISSAL_WORDS`, `STOP_WORDS`, `YES_WORDS`, `NO_WORDS`, `REFUSAL_WORDS`, `FILLER_WORDS`), `looks_like_dismissal`, `answer_to_offer`, `_repeat_description`, `_spoken_due`, `_list_entry`, `_deleted_message`, `_describe`, `_timer_matches`, `_spoken_time_of_day` and `_describe_alarm`.
- No logger: nothing here logs.

- [ ] **Step 1: Record the full suite's count** in both time zones before anything moves. It's the count every later task matches, plus Task 1's four pins.
- [ ] **Step 2: Write the pins** in `tests/test_clock.py`. They pass before the move, two of them trivially:
  - `test_the_clock_keeps_every_name_it_is_imported_by` - each name in the constraints' list is an attribute of `voice_assistant.clock`;
  - `test_the_clock_still_saves_where_the_tests_point_it` - `Clock._save`, `_load`, `_save_alarms` and `_load_alarms` all have `__module__ == "voice_assistant.clock"`;
  - `test_every_part_of_the_clock_logs_as_the_clock` - for each `voice_assistant.clock_*` module (found with `pkgutil.iter_modules`) that has a `logger`, its name is `"voice_assistant.clock"`;
  - `test_each_part_of_the_clock_imports_on_its_own` - for each such module, `subprocess.run([sys.executable, "-c", f"import voice_assistant.{name}"])` succeeds.
- [ ] **Step 3: Move** as above.
- [ ] **Step 4: Check it's a move** with the split check and `git diff -M --color-moved=zebra`. Say in the report which hunks aren't pure moves: the imports, the class line, the docstrings and the re-export.
- [ ] **Step 5: Run** the full suite in both time zones (the count from Step 1, plus 4), and ruff.
- [ ] **Step 6: Commit** - "Move the clock's words into a module of their own".

### Task 2: Ringing in a module of its own

**Files:**
- Create: `voice_assistant/clock_ringing.py`.
- Modify: `voice_assistant/clock.py`.

**Interfaces:**
- Consumes: `format_duration`, `spoken_clock_time` and `_time_of_day_candidates` (`_about_ringing`, `clock.py:1795`) from `clock_words`.
- Produces, in `clock_ringing.py`:
  - at module level: `ALARM_START_LEVEL` and `ALARM_SOUND`;
  - in `class Ringing`: `_run_round`, `_ringing_entry`, `_first_round`, `_continue_ringing`, `_end_ringing`, `_repeat_settings`, `_ring_once`, `_sound_off`, `_timer_sound`, `_ring_alarm`, `_alarm_level`, `_wait_while_ringing`, `_is_ringing`, `_record_unacknowledged`, `is_ringing`, `_rings_on`, `_is_insistent`, `ringing_state`, `silence_insistent`, `dismiss`, `missed`, `_announce`, `_about_ringing` and `_silence_kind`.
- What stays in `clock.py`: `_run`, `_tick_safely` and `_tick`. The tick decides what's due in the stores, and `FAILED_TICK_LOG_GAP` and `PLAUSIBLE_TIME_AFTER` stay with it.

- [ ] **Step 1: Move** as above.
- [ ] **Step 2: Check** with the split check and the coloured diff, as Task 1.
- [ ] **Step 3: Run** the full suite in both time zones (the same count), `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 4: Commit** - "Move ringing into a module of its own".

### Task 3: The satellites' copies in a module of their own

**Files:**
- Create: `voice_assistant/clock_copies.py`.
- Modify: `voice_assistant/clock.py`.

**Interfaces:**
- Consumes: `ALARM_SOUND` from `clock_ringing`.
- Produces, in `class SatelliteCopies`: `move_satellite`, `satellites`, `_occurrence`, `copy_for` and `apply_away`. `copy_for`'s list and `_occurrence`'s ids are what the satellite is sent, so they're the protocol and must come out of the check identical.

- [ ] **Step 1: Move** as above.
- [ ] **Step 2: Check** as Task 1.
- [ ] **Step 3: Run** the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 4: Commit** - "Move what the satellites are sent of the clock into a module of its own".

### Task 4: Setting, listing and cancelling in a module of their own

**Files:**
- Create: `voice_assistant/clock_requests.py`.
- Modify: `voice_assistant/clock.py`.

**Interfaces:**
- Consumes: `format_duration`, `parse_repeat`, `seconds_until` and `_time_of_day_candidates` from `clock_words`.
- Produces, in `class ClockRequests`: `set_timer`, `_timer_clash`, `name_pending_reminder`, `timer_count`, `describe_all`, `list_timers`, `cancel_timer`, `set_alarm`, `_gone`, `_alarm_clash`, `list_alarms`, `overview`, `remove`, `put_back`, `next_alarm` and `cancel_alarm`.
- What's left in `clock.py`, about 650 lines:
  - the module docstring and the constants;
  - `Clock` with `__init__`, `start` and `stop`;
  - the stores: `_save`, `_load`, `_note_missed`, `_save_alarms`, `_changing` and `_load_alarms`;
  - when an alarm next rings: `_next_occurrence`, `_following`, `_alarm_days_overlap` and `_repeat_weekdays`;
  - the tick: `_run`, `_tick_safely` and `_tick`.

- [ ] **Step 1: Move** as above.
- [ ] **Step 2: Check** as Task 1. The check now accounts for every name in the old file.
- [ ] **Step 3: Run** the full suite in both time zones, `tests/test_end_to_end.py` five times, ruff, and `wc -l voice_assistant/clock*.py` for the report.
- [ ] **Step 4: Commit** - "Move setting, listing and cancelling into a module of their own".

---

## Soak prep: what's in and what isn't

Taken from every `progress.md` under `.superpowers/sdd/`. A soak would trip over something that fails by chance, leaks, grows without limit, or needs a restart within 72 hours.

**In:**
- **Task 5:** the flaky `test_losing_the_brain_once_it_has_answered_says_nothing`. It failed twice on 25 October, and a false red mid-soak sends someone hunting for a bug that isn't there.
- **Task 6:** `move_satellite`'s parked FIX BEFORE MERGE, a moved ringing alarm dropped in a rare window. A soak's last fix should be in before it starts, not after.
- **Task 7:** `update.sh`'s parked failure after the pull. Deploying for the soak is an update, and a reboot during the soak must start the code being soaked.
- **Task 8:** `scripts/soak_monitor.py` only watches the Pi (quality audit finding 5, and the satellite ledger's "Soak carry"). The brain is where a leak would show.

**Not in, one line each:**
- **The clock's last tick in the watchdog** (finding 2's third bullet, never built). What was missed while the brain was off is said on the clock's own thread, and can wait for a conversation, so a tick can legitimately take minutes. Counting it would restart the brain falsely, with the missed items already let go of. It needs its own design. The soak's alarm checks cover a stuck clock meanwhile.
- **A Nemotron that hangs for good** gets an apology per request, never a restart. It's parked, and the soak watches for those apologies.
- **A long turn tripping the 150 s watchdog** is parked as rare. The soak counts restarts.
- **The one-hour cut-off for missed items** is only checked at start, so "that was 5 hours ago" can be said. It's honest, and nothing grows.
- **`SettingsPage._playlists`** grows only with the number of satellites.
- **`AudioFeed` has no cap.** It's bounded by the satellite's own most of 30 seconds, whatever the brain's longest request, and the sender has a password.
- **Undo's toasts** stack, but each goes after 10 seconds.
- **`update.sh` stops Nova before `install_service`** (`update.sh:118-120`, under `set -e`), so if that fails Nova is left stopped. It's after the settings check, so it never starts wrong code. The soak checklist's "If it fails" says so.
- **Already done, checked in the code:**
  - the reconnect session leak (`NavidromePlayer._closed`);
  - `latency.jsonl` rotation (`timeline.MAX_BYTES`);
  - the clock thread surviving errors (`_tick_safely`);
  - Spotify's refresh timeout;
  - the `serve()` stop race;
  - 0008's three parked fixes (merged 25 October).
- **Every other "minor (deferred)"** in the ledgers is wording, tests or the page's looks. None leaks, grows or needs a restart.
- **0009 plan 3's Tasks 3-5** are parked for the Pi. They're features, not soak fixes, but see the question in the handback.

### Task 5: The test of losing the brain after its answer, made steady

**Files:**
- Modify: `tests/test_satellite.py`. Also `satellite.py`, only if the cause is there.

**Interfaces:**
- Consumes: nothing from Tasks 1-4.
- Produces: nothing other tasks use.

- [ ] **Step 1: Make it fail.** Temporarily, and not committed, run it 300 times with `@pytest.mark.parametrize("run", range(300))`, with a thread busy-looping in the test from the start, to slow the satellite's own threads as a loaded Pi does: `bash .superpowers/tools/pi_test.sh tests/test_satellite.py -k answered_says_nothing`. If that won't fail, run the 300 inside the full suite (`bash .superpowers/tools/pi_test.sh`), where it failed on 25 October. Note which assertion fails.
- [ ] **Step 2: Find why**, with superpowers:systematic-debugging. A suspect to rule in or out first: the recording's limit branch (`satellite.py`, `_send_recording`, `sent >= limit`, `satellite.py:362-364`) sets `_turn_open = True` *after* the brain's `allow` has cleared it. With `SPEED = 10` (`test_satellite.py:29`) and `LIMIT_SECONDS = 10` (`satellite.py:51`), the cap is about 1 s of real time after the wake, so it takes a stall of about a second: `stop_listening` and `allow` both handled on the link thread while the last piece is in `link.send`.
- [ ] **Step 3: Fix it at the cause.**
  - If the test waits on the wrong thing, it waits for what the satellite decides on, and its microphone never reaches the cap.
  - If the satellite has a real race (a turn the brain has ended, opened again by the recording's own end), fix `satellite.py` with a RED test that forces that order. Then say in the report that **the Pi needs updating for it, which is the user's to do**. In real use it needs the brain's `allow` to arrive within one piece of the cap: rare, but real.
- [ ] **Step 4: Run** the 300 again, the way that made it fail, with no failures. Then take the repeat out, and run the full suite in both time zones and ruff.
- [ ] **Step 5: Commit** - "Make the test of losing the brain after its answer wait for what it checks", or, if `satellite.py` changed, "Never say the phrase for a turn the brain has already ended".

### Task 6: A moved ringing alarm is dropped only if the default rang that same occurrence

The ledger (`.superpowers/sdd/2026-10-25-satellites-on-the-page/progress.md`, final review, parked FIX BEFORE MERGE): `move_satellite`'s `answered` takes a stored copy on the default whose `due_at` is past the ringing occurrence as "rung, ringing or skipped there". But an alarm set on the default *after* the occurrence came due is past it too. So removing a satellite mid-ring, with the same daily alarm set on the default in the last few minutes, silences a wake-up alarm. The ruling: drop a moved ringing entry only when the default's own copy rang this same occurrence.

Today's `dropped_ids` branch in `answered` (`clock.py:2187-2188`) is the bug, not a guard: `stored_twice` matches any same-time repeating alarm on the default whatever its due (`clock.py:2172-2175`), so the moving satellite's stored copy is always dropped, and its ringing entry with it. It goes.

**Files:**
- Modify: `voice_assistant/clock.py` (`_tick`, `_load_alarms`); `voice_assistant/clock_copies.py` (`move_satellite`, `apply_away`); `voice_assistant/clock_requests.py` (`cancel_alarm`).
- Test: `tests/test_clock.py`.

**Interfaces:**
- Consumes: the modules from Tasks 1-4.
- Produces: a stored repeating alarm may carry `"moved_on_from": float`, the `due_at` of the last occurrence it rang, or had answered, on its own satellite. It's set in these places only:
  - on the line before each `alarm["due_at"] = self._following(alarm, alarm["due_at"])` (`clock.py:863`, `2352`, `2510`): rung (`_tick`), dismissed on the satellite (`apply_away`), skipped (`cancel_alarm` with `scope="today"`);
  - in `apply_away`, for each missed alarm the report names as rung, dismissed or still ringing there: the stored alarm with its id, if it repeats and its `due_at` is past the missed one's, gets `moved_on_from = item["due_at"]`, and the alarms file is saved. That's the brain off as it came due, with the satellite ringing it from its copy - `_load_alarms` had already moved the stored copy on, so the dismissed loop never sees it;
  - in `move_satellite`'s merge (below).
  Never on `_load_alarms`' own move past a missed occurrence, by `put_back`, or on a new alarm.
- `_load_alarms` keeps it when it's an `int` or `float` and not a `bool`, and leaves it out otherwise.
- `move_satellite`:
  - `dropped_ringing` is worked out **before** the merge loop, so the merge can't change what `answered` sees;
  - `answered(entry)` loses its `dropped_ids` parameter: true only when `alarm["repeat"]` and some stored copy on the default with the same time and days has `int(other.get("moved_on_from", 0)) == int(alarm["due_at"])` (as `_occurrence` names occurrences, `clock.py:2262`). A ringing one-off has already left the store, and two of them ringing at once are still caught by `doubled`;
  - `stored_twice`'s merge gives the kept copy the dropped copy's `moved_on_from` only when the dropped copy's `due_at` is strictly later than the kept one's (looked at before the `max`). On a tie the kept copy keeps its own.
- How it decides, in the first new test: before the move, the kitchen's stored copy is due tomorrow with `moved_on_from = today` (its own ring), and the bedroom's is due tomorrow with none. The ringing entry's `due_at` is today. No bedroom copy has `moved_on_from == today`, so it rings on. In the existing pin, the bedroom's own ring set its `moved_on_from` to today, so it's dropped.
- Accepted, in a comment in `move_satellite`: after a brain restart, if the kitchen is removed while its copy rings *before* the bedroom satellite has reported its own ring, the alarm rings once more on the bedroom. It's the safe side.
- Nothing the satellite is sent changes: `copy_for` builds its own entries, and `overview` names only id, name and due.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_moved_ringing_alarm_rings_on_when_the_defaults_was_set_after_it_came_due`:
    - written to `alarms.json` as the existing test does: the kitchen's daily 7:00 due `today`, and the bedroom's due `clock._next_occurrence(7, 0, "daily", after=today + 60)` with no `moved_on_from`, as one set just after today's came due (not `today + 86400`, which is an hour out across a clock change in Europe/London);
    - the kitchen's rings (`_tick(today + 1)`);
    - `move_satellite("kitchen", "bedroom")`;
    - one alarm entry is ringing, now on the bedroom, and `sorted(clock._alarms)` is the bedroom's alone;
  - `test_a_moved_ringing_alarm_the_default_skipped_today_doesnt_ring_again`:
    - the bedroom's daily 7:00, alone in the file, is skipped with `cancel_alarm("7am", scope="today")`;
    - the kitchen's same alarm is added to the file, and the clock is made again;
    - the kitchen's rings and moves: no alarm entry rings;
  - `test_a_moved_alarm_the_default_rang_while_the_brain_was_off_doesnt_ring_again`:
    - both satellites' daily 7:00 written with `due_at = now - 30`, as `test_what_was_still_ringing_while_away_rings_on_here` does, and the clock made, so both are missed and moved on;
    - `apply_away("bedroom", rang=[b], dismissed=[b])`, then `apply_away("kitchen", rang=[k], dismissed=[], ringing=[k])`, with `b` and `k` the two `alarm-<id>@<int(now - 30)>` ids: the kitchen's rings on the brain;
    - `move_satellite("kitchen", "bedroom")`: no alarm entry rings;
  - `test_an_alarm_dismissed_while_away_notes_the_occurrence_it_moved_on_from` - after `apply_away` with that occurrence dismissed, `moved_on_from` is its due;
  - `test_the_occurrence_an_alarm_moved_on_from_survives_a_restart` - a fresh `Clock` over the same files has it;
  - the existing `test_a_moved_ringing_alarm_the_default_has_answered_doesnt_ring_again` stays as it is (a pin).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_clock.py -k "moved_ringing or moved_on_from or moved_alarm"`. Expected: the first, fourth and fifth fail. The skip test and the brain-off test pass today (through `dropped_ids`), as pins that the fix mustn't break.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass, the existing pin too. Then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 5: Commit** - "Keep a moved alarm ringing unless the satellite it moves to rang that same morning's".

### Task 7: A failed update never leaves the new code for a reboot to start

The ledger (`.superpowers/sdd/2026-10-25-settings-straight-away/progress.md`, final review, parked for the soak prep): a failure after the pull but before the settings check leaves the new code checked out, with Nova untouched. Examples are a package install or a model download. Then a reboot starts the new code. The likeliest failure isn't a step's error but the phone's SSH or Remote Control session dropping mid-step: bash then runs an `EXIT` trap with `$?` = 0, so the trap can't go by the status.

**Files:**
- Modify: `update.sh`.
- Modify: `tests/test_config.py` - `_run_update_check` (`tests/test_config.py:313-331`) cuts out the take-back block as well as the settings-check block, take-back first, or `settings_check` gets "command not found" for `take_back` and two existing tests fail.
- Test: `tests/test_config.py`, beside the settings-check tests, using `_updated_clone`.

**Interfaces:**
- Produces, in `update.sh`, inside `# BEGIN take back` / `# END take back`, placed straight after the `done_ "On ..."` line (once any re-exec has happened) and so before `# BEGIN settings check`:
  - `take_back(why)` - lifted out of `settings_check`'s three branches, which call it. It keeps their wording and order, the `git reset` before any message, so a session that's gone loses only the message:
    - with no `NOVA_UPDATED_FROM`: "${why} The code checked out is in place, but Nova is still running the code it started with until it next restarts:" (true whether or not the code is new);
    - "has been taken back out" after `git reset -q --keep "$NOVA_UPDATED_FROM"`;
    - or "couldn't be taken back out, so check out ... before Nova next restarts".
  - `watch_steps` - sets `steps_done=false`, then `trap failed_after_pull EXIT` and `trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM`, so a hangup or Ctrl+C reaches the `EXIT` trap with a non-zero status.
  - `steps_finished` - sets `steps_done=true`, then `trap - EXIT HUP INT TERM`.
  - `failed_after_pull` - its first line `local status=$?`. If `steps_done` is true, it returns. Otherwise it calls `take_back "A step after fetching the new code failed - Nova is still running the code it had before."` on any failure, with or without `NOVA_UPDATED_FROM`, then adds "Python packages it installed stay installed, so put right what failed and run this again soon - until then the old code may not start with them." Then `exit $(( status == 0 ? 1 : status ))`.
- In `update.sh`'s flow: `watch_steps` straight after the block, and `steps_finished` just before `settings_check || exit 1`, which says its own piece.
- The risk left: with the brain's `--no-deps` lock (`setup.sh:243`), new pins plus old code may not start at the next reboot. The message says to run it again; nothing here can undo pip.
- Takes effect the first time it's pulled, since `update.sh` runs again from the new code after a pull - on the brain and, at the user's next update, the Pi.

- [ ] **Step 1: Write the failing tests.** A helper `_run_steps(tmp_path, step, project, updated_from=None, wait_for=None)` builds a script as `_run_update_check` does: `set -euo pipefail`, the same stand-ins, the take-back block, then `watch_steps`, the step, `steps_finished`, `echo finished`.
  - `test_a_failed_step_after_the_pull_takes_the_new_code_back_out` - step `false`, `NOVA_UPDATED_FROM` set: HEAD is back at `before`, still on `main`, the status isn't 0, and stderr says "A step after fetching the new code failed" and "taken back out";
  - `test_a_failed_step_with_no_pull_leaves_the_code_as_it_is` - the same with no `NOVA_UPDATED_FROM`: HEAD unchanged, the status isn't 0, and stderr says "A step after fetching the new code failed" and "is in place";
  - `test_an_update_cut_off_mid_step_takes_the_new_code_back_out` - parametrised over `(SIGHUP, 129)`, `(SIGINT, 130)`, `(SIGTERM, 143)`. Step `bash -c 'echo started; sleep 30'`, run with `subprocess.Popen(..., start_new_session=True)`. Once stdout reads "started", `os.killpg(proc.pid, sig)`, as a dropped terminal signals its whole group (bash waits for a foreground child before running a trap, so signalling bash alone waits out the sleep). `communicate(timeout=10)`: the return code is the expected one, HEAD is back at `before`, stderr says "taken back out", and "finished" isn't in stdout;
  - `test_steps_that_finish_leave_the_new_code_in` - step `true`: status 0, "finished" in stdout, no "PROBLEM", and `version` reads "after";
  - `test_the_trap_covers_the_steps_and_not_the_settings_check` - in `update.sh`'s text: `# BEGIN take back` comes after `done_ "On` and before `# BEGIN settings check`; `\nwatch_steps\n` comes before `"\nrun_steps "` (with the newline, as `update.sh:4` names `run_steps` in a comment); `\nsteps_finished\n` comes before `"\nsettings_check || exit 1\n"`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_config.py -k "update or failed_step or cut_off or steps_that_finish or trap"`. Expected: the five new ones fail. The existing update tests pass.
- [ ] **Step 3: Implement** as the Interfaces say, with `_run_update_check` taking both blocks, then `bash -n update.sh`.
- [ ] **Step 4: Run them again** - expected: all pass, the existing ones too. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Take the new code back out when an update fails or is cut off after fetching it".

### Task 8: The soak watches the brain too

**Files:**
- Modify: `scripts/soak_monitor.py`; `docs/release-checklist.md` ("Leaving it running", and its `Nova ready!` line, per finding 5).
- Test: `tests/test_soak_monitor.py`.

**Interfaces:**
- Produces:
  - `Target = NamedTuple("Target", [("service", str), ("user_scope", bool), ("log", str)])`, and `TARGETS = {"satellite": Target("voice-assistant", True, "smart_assistant.log"), "brain": Target("nova-backend", False, "backend.log")}`.
  - `--brain` picks the brain's target, and the satellite's is the default. `_systemctl(prop, target)` passes `--user` only for a user-scope target.
  - Before the first reading, and before making `--out`'s folder, `main` checks the target's log exists in `LOGS_DIRECTORY`. If not, it prints "No <log> in <folder> - run this as the service's own user, with its own home (sudo -iu nova on the brain)." and exits 1. With the wrong `HOME` it would otherwise make an empty logs folder and report 0 errors.
  - `count_errors(log: str, since: datetime) -> int` - counts lines with " - ERROR - " or " - CRITICAL - " whose leading timestamp (`%Y-%m-%d %H:%M:%S`, the first 19 characters) is at or after `since`, in the log and its `.1` to `.3`. A line whose first 19 characters aren't a timestamp (a traceback's, or a message's second line) is skipped, not counted. `sample` gets `since`: the monitor's start time for the first reading, so errors from before the soak aren't counted, and the last reading's time after that. A new `errors` field holds each reading's count.
  - `interactions` counts `latency.jsonl` and `latency.jsonl.1`, since it rotates. Only the satellite writes it (`satellite.py:136`), so `summarise(rows, brain: bool = False)` leaves ", N interactions" out of its first line for the brain.
  - `FILE_GROWTH = 20`. `summarise` adds, coping with readings that have no `errors` (an old `soak.csv`):
    - "Errors: N logged, at most M between two readings.";
    - "! Open files went from A to B - something isn't being closed." past `FILE_GROWTH`;
    - "! N errors logged - read them in the log." when there are any.
- The checklist's section says how to run one monitor on each machine. On the Pi, as now. On the brain, as `nova` with its own home (`sudo -iu nova`), with its venv's Python, `--brain`. The brain section's own checks are done before the monitors start, so they don't show as restarts or a changed process in the readings: restarting it, and the satellite ringing from its copy while the brain is stopped - a one-minute timer, `kill -STOP` the brain's MainPID about 20 s before it's due, the Pi rings it from its copy (it says "Lost the brain" after 15 s of silence), then `kill -CONT` 30 s after the stop. Never longer: `nova-backend.service:23` is `WatchdogSec=60`, so a minute's stop is a restart. It names no host.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_brain_is_watched_as_a_system_service` - a fake `subprocess.run` sees `nova-backend` and no `--user`;
  - `test_errors_are_counted_since_the_last_reading` - a scratch log with ERROR lines either side of `since`, one in `.1`, and a traceback line after an ERROR: the count is only those at or after `since`, the traceback line not among them;
  - `test_a_missing_log_stops_the_monitor_before_it_starts` - `main` with `LOGS_DIRECTORY` patched to an empty scratch folder exits 1, says "No backend.log", and makes no `soak.csv`;
  - `test_interactions_count_the_rotated_latency_file_too`;
  - `test_growing_open_files_and_any_errors_are_pointed_out`;
  - `test_the_brains_summary_leaves_interactions_out` - `summarise(rows, brain=True)`'s first line has no "interactions";
  - the existing four still pass.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_soak_monitor.py`. Expected: the new ones fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the checklist.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Let the soak monitor watch the brain, count errors and notice open files growing".

---

## The soak checklist

Not a code task. The user runs it, after deploying the brain and updating the Pi to this plan's last commit. Everything is measured read-only: `/proc`, `systemctl show`, and the logs.

**Before it starts**
- Note the commit each machine runs (`git log -1 --format=%h` in each clone). The soak is of that code.
- Do the brain section's restart and `kill -STOP` checks (Task 8) now, before the monitors.
- Leave "Keep conversations in the logs" on (`logging.log_transcripts`). Otherwise the "Timer finished:" lines the timing check reads aren't written.
- Start both monitors for 72 hours, in something that outlives the terminal (`tmux` or `nohup`):
  - on the Pi: `venv/bin/python scripts/soak_monitor.py --hours 72`;
  - on the brain: the same with its venv's Python and `--brain`, after `sudo -iu nova` (it refuses to start under the wrong home). Errors from before it started aren't counted.
- Set what the soak should ring, by voice, each at a time nothing else rings near:
  - a daily alarm at a time the user's awake (three rings);
  - a reminder in 24 hours, and another in 48;
  - a one-hour timer.
  That's six rings to check. Note each one's due time.
- Note `df -h` for the brain's root and the Pi's SD card.

**During it**
- No deploys, updates or restarts by hand. Use Nova as usual: wake word, music, lights, the page.
- The user keeps a rough tally of wake words missed and false wakes.

**What it watches, and what passes**

| What | How, read-only | Passes when |
|---|---|---|
| Memory (brain, Pi) | `soak.csv` `rss_mb`, read by hand from the hour-2 row (the summary's figure is from the first reading) | No more than 50 MB of growth from hour 2 to the end on each, and the brain well under its 4 GB |
| Threads | `soak.csv` `threads` | End minus start at most 10, with no steady climb |
| File handles | `soak.csv` `open_files` | End minus start at most 20, with no steady climb |
| Restarts | `soak.csv` `restarts` and `pid`, and `journalctl -u nova-backend \| grep -i watchdog` | None on either machine |
| Errors | `soak.csv` `errors`, then the lines themselves | No "The clock's tick failed", no traceback, and nothing unexplained. "Couldn't reach ..." during a known internet or Wi-Fi drop is fine |
| Alarms and timers | `grep -E "Timer finished:\|Dismissed" backend.log*` in nova's logs folder, against each one's due time. "Timer finished:" comes once per announcement - every round until it's dismissed, and each alarm cycle - and an alarm's says the time, not which alarm, so go by time | All six rang: the first "Timer finished:" for each within 5 seconds of its due (later only while a conversation was going on), none for it after its "Dismissed" line until its next due, and none in "You missed" unless it went unanswered |
| Wake word | `grep -c "Wake word detected" smart_assistant.log*` on the Pi, against the tally | No misses the user noticed in everyday use, and no more false wakes than in the week before |
| Audio | `journalctl --user-unit voice-assistant \| grep -E "ran dry\|overflowed"` on the Pi | None, or a handful explained by load |
| The link | "Lost the brain" in the Pi's log | None, or each one during a known Wi-Fi drop, followed by reconnecting |
| Disk | `df -h`, and the logs' sizes | Logs at most 5 MB each with three old ones, and free space much as at the start |

**If it fails:** the failure is found and fixed, with a test, and the soak starts again from zero - 72 hours. The soak is the final thing, so a passing one is followed only by the merge. An update that stops at "Restarting" may leave Nova stopped (`install_service` failed after `stop_service`): start it by hand once that's put right.
