# Satellites on the page - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Satellites are added, given a new password, removed and made the default on the page, with no shell. Each satellite plays Spotify on its own speaker, and keeps listening to a long request for as long as the brain's "Longest request" says, up to 30 seconds. A routine can be given a satellite on the page, and keeps it.

**Architecture:** `satellite_passwords.py` gains the name rule, a new password and a new order. The brain handles a satellite gone from the passwords' file in one place, `Brain._on_removed`, whether the page or `remove-satellite` took it out: it empties a connected one's copy, lets it go, and moves its timers, alarms and routines to the default. Two new optional fields reach the satellite's protocol - `speaker` in its hello, and `longest_request` in the brain's `state` - so either side works with the other not yet updated. `setup.sh --new-password` is how a Pi takes a new password.

**Tech stack:** Python 3.13 on the brain and the Pi, bash, the settings page's HTML, CSS and JavaScript, pytest. No new dependency.

**Spec:** `docs/decisions/0009-the-settings-page.md` - decisions 6, 7, 10 and 11, and the user's answer 1 and decision 8. Read it before starting any task. This is the third of 0009's four plans (the split is said at the top of `2026-10-25-settings-straight-away.md`). It's built after plan 1, whose `LIMITS`, `EDITABLE` "used" field, `Session.thresholds` property and `Brain.apply_settings` Task 4 uses.

## Global constraints

- **This plan needs the Pi updated, which is the user's to do.** Tasks 3, 4 and 5 change `satellite.py`, `voice_assistant/protocol.py` and `setup.sh`, which run on the Pi. Until the Pi is updated (`bash update.sh` there), it keeps 10 seconds, one Spotify speaker name and no `--new-password`, and everything still works. The brain is deployed first or second - either way round works, and each side has a test for the other not yet updated.
- **A satellite's password is shown once:** in the answer to the request that made it, and nowhere else - never logged, never in `state()` or any other GET, never in the page's own variables beyond the panel showing it. The panel stays until the user closes it, then it's gone.
- **The longest request:** "Longest request, in seconds", from 5 to 30, default 15 (was 10), under Listening, used straight away. The satellite's own most is `MOST_SECONDS = 30`, whatever the brain says, and until the brain says, `LIMIT_SECONDS = 10`, as now. The hardware sets no limit here.
- **Removing never loses an alarm, and never leaves one to ring twice:** a connected satellite is sent an empty copy before it's let go; one that isn't connected is warned about, in the question, in the spec's words. The satellite never clears its copy on a refusal - that refusal comes before either side has proved anything.
- **Renaming stays off the page.** `add-satellite` and `remove-satellite` stay on the command line, and a removal is handled the same way whichever made it.
- **The page's security stays as it is, never weaker:** 127.0.0.1 only, behind `tailscale serve`; only `settings_page.allow`'s logins; every POST needs `X-Nova: page` and a same-origin `Sec-Fetch-Site`; `SAFETY_HEADERS`, with `no-store`, on every answer; `MOST_BODY`. The page's process can already read the passwords' file, so nothing new is exposed.
- **The repo is public.** No hostname, IP address, email address, password or token in code, tests, docs or commits. Test satellites are "bedroom" and "kitchen"; test passwords are made up and at least `MIN_PASSWORD` long.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'`. `node --check voice_assistant/web/settings.js`, and `bash -n setup.sh update.sh`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Safety:** never push or deploy; touch no machine except through those commands - pi_test's copy in `/tmp` is never the Pi's live install.

## Review focus

1. **Removing a satellite that's switched off** - its alarms move to the default, the default rings them, and the question warns that the Pi's own copy rings for a week if it's switched on. (Task 2, `test_removing_one_thats_away_moves_its_alarms_and_warns`.)
2. **Removing the default, or the last satellite** - the next becomes the default; with none left, its timers and alarms go to the next one added. (Task 2, `test_removing_the_last_satellite_keeps_its_alarms_for_the_next`.)
3. **A satellite removed with `remove-satellite` while connected** is handled the same as one removed on the page: an empty copy, let go, everything moved. (Task 2, `test_a_removal_on_the_command_line_is_handled_the_same`.)
4. **A brain or satellite not yet updated** - no `speaker` in the hello, no `longest_request` in the state, or one the satellite doesn't know - still connects, plays and records, either way round. (Tasks 3 and 4, `test_a_hello_without_a_speaker_still_connects` and `test_an_old_brain_leaves_the_satellite_at_ten_seconds`.)
5. **A password in the log or the page's state** - add, then a new password, then `state()`, with caplog at DEBUG: the password is in neither. (Task 1, `test_a_new_password_is_shown_once_and_never_logged`.)

## Live checks before Task 1

For the controller, read-only, with the user's OK. Write down yes or no, and counts - never a name, password or address.

1. **The Pi's machine name fits the rule** (lower-case letters, digits and hyphens, 1 to 63, not starting or ending with a hyphen), and so does each name in the brain's `satellites.json` (names only, from `SatellitePasswords().names()`).
2. **The Pi's `spotify.device_name`** (from its own `Config()`) is what `/etc/raspotify/conf`'s `LIBRESPOT_NAME` says.
3. **The Pi's `~/.config/nova/.env`** has exactly one `NOVA_BACKEND_TOKEN=` line (`grep -c`), and other lines too or not.

---

### Task 1: Add a satellite, or give one a new password, on the page

**Files:**
- Modify: `voice_assistant/satellite_passwords.py` (`name_problem`, `renew`); `backend.py` (`add-satellite` uses the rule); `voice_assistant/brain.py` (`add_satellite`, `renew_password`); `voice_assistant/settings_page.py` (two actions; `_satellites` says which is the default); `voice_assistant/web/settings.html`, `settings.js` and `settings.css` (Add a satellite, New password, the panel).
- Test: `tests/test_satellite_passwords.py`, `tests/test_brain.py`, `tests/test_settings_page.py`, `tests/test_backend.py`.

**Interfaces:**
- Produces:
  - `NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")` and `name_problem(name: str) -> Optional[str]` - "A satellite's name is its Pi's machine name: lower-case letters, digits and hyphens, not starting or ending with a hyphen." or None. Names already there aren't checked again.
  - `SatellitePasswords.renew(name: str) -> str` - a new `secrets.token_urlsafe(32)` in place of its old one, keeping its place in the order. `ValueError` if there's no such satellite.
  - `Brain.add_satellite(name: str) -> str` - `name_problem`, then `passwords.add(name)`; `ValueError` with the sentence. It's let in at its first try, as now.
  - `Brain.renew_password(name: str) -> str` - `renew`, then a connected session is closed and its satellite let go, so it's refused until its setup has the new password. Its alarms, routines and volumes stay.
  - `SettingsPage.add_satellite(name, who)` and `POST /api/satellites/add` `{"name": ...}`; `SettingsPage.new_password(name, who)` and `POST /api/satellites/password` `{"name": ...}`. Each answers `{"success": True, "message": ..., "password": ...}` - the only answers that ever hold one. Logged as `f"Settings page: {who} added the {name} satellite"` and `f"... gave the {name} satellite a new password"`.
  - `_satellites()` entries gain `"default": bool`.
  - `SatellitePasswords` reads, changes and writes with no lock of its own, so the page's four satellite actions (here and in Task 2) each hold `SettingsPage._saving`.
  - The panel, after either: the password, a Copy button, and for a new satellite "Type this into setup.sh on the Pi"; for a new password "On the Pi, run `bash setup.sh --new-password` and type it in - or, on a Pi not yet updated, put it after `NOVA_BACKEND_TOKEN=` in `~/.config/nova/.env` and restart the satellite". (`--new-password` is Task 5's; until the Pi is updated, the second half is the way.) The password is put straight into the panel's text and nowhere else, and closing the panel removes it from the page.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_satellites_name_is_a_machine_name` - "bedroom", "pi-2" and "a" pass; "Bedroom", "-pi", "pi-", "pi_2", "pi 2" and 64 letters fail;
  - `test_a_satellite_added_on_the_page_is_let_in_with_no_restart` - added, then a fake hello with its password is admitted by `Brain._admit`;
  - `test_a_new_password_lets_go_of_the_satellite_and_refuses_the_old_one` - a connected session is closed; the old password is refused; the new one is let in; its alarms are still its own;
  - `test_a_new_password_is_shown_once_and_never_logged` - caplog at DEBUG over add and new password has neither password; `state()` and `json.dumps(state())` don't have it;
  - `test_add_satellite_on_the_command_line_uses_the_same_rule`;
  - `test_the_default_satellite_is_marked`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_satellite_passwords.py tests/test_brain.py tests/test_settings_page.py tests/test_backend.py -k "name or added or new_password or default_satellite"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the page's parts, the Copy button at least 44 px.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Add a satellite, or give one a new password, on the page".

### Task 2: Remove a satellite, or make one the default

**Files:**
- Modify: `voice_assistant/satellite_passwords.py` (`make_default`); `voice_assistant/clock.py` (`assign_unowned` widened to `move_satellite`); `voice_assistant/routines.py` (`forget_satellite`); `voice_assistant/brain.py` (`_shut_out_removed`, `_on_removed`, `remove_satellite`, `make_default`); `voice_assistant/settings_page.py` (two actions); `voice_assistant/web/settings.js` (Remove with its question, and Make default).
- Test: `tests/test_satellite_passwords.py`, `tests/test_clock.py`, `tests/test_routines.py`, `tests/test_brain.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: `_satellites()`'s `default` from Task 1.
- Produces:
  - `SatellitePasswords.make_default(name: str)` - moves it first in the file. `ValueError` if there's none.
  - `Clock.move_satellite(from_satellite: Optional[str], to_satellite: Optional[str]) -> int` - `assign_unowned`'s body, for any one satellite's timers, alarms, ringing and missed. `assign_unowned(satellite)` becomes `move_satellite(None, satellite)`.
  - `Routines.forget_satellite(name: str) -> int` - each routine on it gets `satellite: None`, so it runs on the default; and the routines kept for it while it was away (`_missed`) are dropped, as it's never coming back to run them.
  - `Brain._names_seen: List[str]`, and `_shut_out_removed()` finds each name that's gone since and calls `_on_removed(name)` for it. That's the one place a removal is handled while the brain runs, from the page or from `remove-satellite`. A new `self._removing = threading.Lock()` is held round its compare-and-handle, as both the server's poll (`service_actions`) and `remove_satellite` call it, so one removal is never handled twice.
  - A removal made while the brain was down - `remove-satellite` with it stopped, or across an update - is caught at start: `_give_out_unowned` is widened to `_give_out_orphans()`, which moves whatever rings on no satellite, or on one not in `passwords.names()`, to the default (or leaves it on none with no satellite yet), and runs `routines.forget_satellite` for each such name. It runs at start as `_give_out_unowned` does, and as each satellite connects.
  - `Brain._on_removed(name: str)`, in this order:
    1. a connected one's session is taken out of `sessions` first, so neither the clock's `on_change` nor the hourly resend can send it its real copy; then, holding `_sending_alarms`, it's sent `send_alarms([])`; then its session is closed and its satellite let go;
    2. `clock.move_satellite(name, self.default_satellite())` - None with no satellite left, and then `_unowned_given` is False again, so the next added is given them;
    3. `routines.forget_satellite(name)`;
    4. its volumes and mute stay in the data folder, untouched.
  - `Brain.remove_satellite(name: str) -> bool` - `passwords.remove`, then `_shut_out_removed()` straight away, rather than at the next poll.
  - `Brain.make_default(name: str)` - `make_default`; ntfy's alerts, 0007's spoken warnings and routines without a satellite follow it from then on, as each reads `default_satellite()`.
  - `POST /api/satellites/remove` `{"name": ...}` - "Removed {name}." - and `POST /api/satellites/default` `{"name": ...}` - "{name} is the default now.".
  - The page asks before removing, built from what it has:
    - always: `Remove ${name}? It's let go now, and can't connect again until it's added again. Its timers and alarms move to ${next}, and routines that ran on it run there instead.`;
    - if it's the default: ` ${next} becomes the default.`;
    - if it isn't connected: ` It still has a copy of its alarms for the next week. If it's switched on before then, it'll ring them too, so wipe it or connect it first.`;
    - with no other satellite, `${next}` parts are ` Its timers and alarms go to the next satellite added.`

- [ ] **Step 1: Write the failing tests:**
  - `test_removing_a_connected_satellite_empties_its_copy_first` - the fake satellite gets `alarms` with `[]` before it's closed;
  - `test_removing_one_thats_away_moves_its_alarms_and_warns` - its weekday alarm is now the default's, and is in the default's copy;
  - `test_removing_the_default_makes_the_next_one_default` - and its alarms go to that one;
  - `test_removing_the_last_satellite_keeps_its_alarms_for_the_next` - they ring on none, and the next satellite added is given them as it connects;
  - `test_routines_on_a_removed_satellite_run_on_the_default`;
  - `test_a_removed_satellites_volumes_are_kept` - added again, it has its old volumes;
  - `test_a_removal_on_the_command_line_is_handled_the_same` - `SatellitePasswords().remove("kitchen")` outside the brain, then `_shut_out_removed()`: all of the above;
  - `test_a_removal_while_the_brain_was_down_is_caught_at_start` - alarms and a routine on "kitchen", "kitchen" taken out of the file before the `Brain` is made: once it starts, they're the default's;
  - `test_a_removal_is_handled_once` - two threads calling `_shut_out_removed()` at once move the alarms once;
  - `test_no_real_copy_reaches_a_removed_satellite` - an `on_change` during the removal sends the removed satellite nothing after its empty copy;
  - `test_routines_kept_for_a_removed_satellite_are_dropped`;
  - `test_make_default_puts_it_first` - `default_satellite()` is it, and a routine without a satellite runs there.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_satellite_passwords.py tests/test_clock.py tests/test_routines.py tests/test_brain.py tests/test_settings_page.py -k "remov or default or move_satellite or forget_satellite or orphan or real_copy or kept_for"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 5: Commit** - "Remove a satellite or make it the default on the page, moving its alarms so none is lost".

### Task 3: Each satellite's own Spotify speaker (needs the Pi updated)

**Files:**
- Modify: `voice_assistant/protocol.py` (`greet`, `admit`, the docstring); `voice_assistant/satellite_link.py` (`SatelliteLink`, which sends the hello); `satellite.py` (gives the link its speaker); `voice_assistant/remote.py` (`RemoteSatellite.speaker_name`); `voice_assistant/music.py` (`make_player`); `voice_assistant/spotify_controller.py` (`__init__`, `NOT_SIGNED_IN` unchanged here); `voice_assistant/session.py` (the player it makes); `voice_assistant/settings_page.py` (`_satellites`).
- Test: `tests/test_protocol.py`, `tests/test_satellite_link.py`, `tests/test_satellite.py`, `tests/test_spotify.py`, `tests/test_session.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `greet(stream, name, hostname, password, sample_rate, unclean, speaker: Optional[str] = None)` - the hello has `speaker` only when it's given.
  - `admit(...)` gives back `"speaker"`: the hello's, if it's text of 1 to 64 characters with no control characters, else None. Anything else in the hello is ignored, as now.
  - `SatelliteLink(..., speaker: Optional[str] = None)` passes it to `greet`. The satellite gives it its own `spotify.device_name`, from its own settings - the name its setup gave raspotify. It's changed on the Pi, with its setup, never from the page.
  - `RemoteSatellite.speaker_name: Optional[str]`, from the hello.
  - `make_player(config, speaker, volume, spotify_speaker: Optional[str] = None)` and `SpotifyController(config=None, device_name: Optional[str] = None)` - the satellite's name if given, else `spotify.device_name`, as now.
  - `_satellites()` entries gain `"spotify_speaker"` while connected - its own, or the brain's `spotify.device_name` for one that doesn't say, as that's what it plays on - and None while it isn't; the page shows "Spotify plays on {name}" in the satellite's row.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_satellite_says_its_spotify_speaker` - the hello carries `speaker`, and `admit` gives it back;
  - `test_a_hello_without_a_speaker_still_connects` - `speaker` is None; and a hello with a speaker of 200 characters, or a number, gives None and still connects;
  - `test_an_old_brain_ignores_the_speaker` - today's `admit` shape ignores the field (a hello with an unknown field is let in);
  - `test_each_session_plays_on_its_own_satellites_speaker` - two sessions, "Bedroom Pi" and "Kitchen Pi", each `SpotifyController.device_name` its own;
  - `test_a_satellite_that_doesnt_say_uses_the_brains_name` - `spotify.device_name`;
  - `test_the_satellites_list_shows_each_speaker`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_protocol.py tests/test_satellite.py tests/test_spotify.py tests/test_session.py tests/test_settings_page.py -k "speaker"` and `tests/test_satellite_link.py`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, and the protocol's docstring: `hello {name, hostname, sample_rate, unclean, nonce, speaker?}`.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 5: Commit** - "Play Spotify on each satellite's own speaker, which it says as it connects".

### Task 4: The longest request (needs the Pi updated)

**Files:**
- Modify: `config.yaml` (`recording_timeout: 15.0`, its comment saying it's the longest one request may last, and the satellite's own most); `voice_assistant/config.py` (`LIMITS`); `voice_assistant/settings_page.py` (`EDITABLE`); `voice_assistant/remote.py` (`set_state`); `voice_assistant/session.py` (`send_state`); `voice_assistant/brain.py` (`apply_settings`); `satellite.py` (`MOST_SECONDS`, `longest_request`, `_take_state`, `_send_recording`); `voice_assistant/protocol.py` (the docstring).
- Test: `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_session.py`, `tests/test_brain.py`, `tests/test_satellite.py`.

**Interfaces:**
- Consumes: plan 1's `LIMITS`, `EDITABLE` "used", `Session.thresholds` and `Brain.apply_settings`.
- Produces:
  - `LIMITS[("thresholds", "recording_timeout")] = Limit("number", 5, 30, False)`, and `EDITABLE` gains `("thresholds", "recording_timeout", "Longest request, in seconds", "now")`, in the Listening group.
  - `RemoteSatellite.set_state(volumes, muted, longest_request: Optional[float] = None)` - the `state` message has `longest_request` only when it's given.
  - `Session.send_state()` gives it `self.thresholds.get("recording_timeout", 15.0)` - so it's sent as the satellite connects, and with every volume or mute change.
  - `Brain.apply_settings` for `thresholds.recording_timeout`: `send_state()` on every session connected.
  - On the satellite: `LIMIT_SECONDS = 10` (until the brain says), `MOST_SECONDS = 30`, and `Satellite.longest_request: float`, in memory only. `_take_state` sets it to `min(value, MOST_SECONDS)` for a finite number above 0 (not a bool), and leaves it for anything else. `_send_recording` uses it.
  - The brain's own `record_until_silence` timeout is the same setting, already read at use (plan 1).

- [ ] **Step 1: Write the failing tests:**
  - `test_the_brain_sends_each_satellite_its_longest_request` - as it connects, and to every satellite when it's saved, with no restart;
  - `test_the_satellite_records_for_as_long_as_the_brain_says` - a state with 15 gives an `audio_end` `limit` after 15 s of fake microphone, not 10;
  - `test_the_satellites_own_most_is_thirty_seconds` - 90 gives 30;
  - `test_an_old_brain_leaves_the_satellite_at_ten_seconds` - a state with no field, or with "15" as text, or True, keeps 10;
  - `test_an_old_satellite_ignores_the_longest_request` - today's `StateCopy.update` with the field keeps volumes and mute as sent;
  - `test_longest_request_is_on_the_page_from_5_to_30` - and 4 is refused as "Longest request, in seconds should be from 5 to 30.".
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_config.py tests/test_settings_page.py tests/test_session.py tests/test_brain.py tests/test_satellite.py -k "longest or ten_seconds or thirty"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, and the protocol's docstring: `state {"volumes": ..., "muted": ..., "longest_request"?: seconds}`.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff.
- [ ] **Step 5: Commit** - "Let the brain's longest request reach the satellite, up to 30 seconds, and make it 15".

### Task 5: A Pi takes a new password with `setup.sh --new-password` (needs the Pi updated)

**Files:**
- Modify: `setup.sh` (`new_password`, the option, the header's list of options; `write_secrets` and the brain's closing hint send the user to the settings page's Satellites to add one, with `add-satellite` as the fallback); `README.md` (adding and replacing a satellite: the page, then setup.sh); `docs/troubleshooting.md` (a satellite turned away after a new password).
- Test: `tests/test_setup_script.py` (new).

**Interfaces:**
- Produces:
  - `bash setup.sh --new-password`, on a satellite only, and alone: on the brain, or with no `.env` yet, it says to run `bash setup.sh` instead and exits 1.
  - `new_password()` asks `read -rsp "  The new password from the settings page (not shown as it's typed): "`, trims spaces and a carriage return from it (a phone's copy can bring them), refuses one shorter than 16 characters or with anything but `[A-Za-z0-9_-]` (`token_urlsafe`'s letters), replaces the `NOVA_BACKEND_TOKEN=` line of `$SECRETS_FILE` - adding one if it has none - keeping every other line, written with `umask 077` to a file beside it and moved into place, then `systemctl --user restart "$SERVICE_NAME"`, and says `done_ "Saved, and the satellite restarted"`. The password is never echoed.

- [ ] **Step 1: Write the failing tests**, running `bash -c 'source setup.sh; new_password'` with `NOVA_CONFIG` set to a temporary folder, `NOVA_SETUP_ROLE=satellite`, the password on stdin, and a `systemctl` stand-in first on `PATH` that writes its arguments to a file:
  - `test_new_password_replaces_only_the_token` - a `.env` with `NOVA_BACKEND_TOKEN=old` and another line: afterwards the token is the new one, the other line is kept, the file is mode 600, and `systemctl --user restart voice-assistant` ran;
  - `test_new_password_adds_a_token_line_if_there_isnt_one`;
  - `test_new_password_refuses_a_short_one` - the file is unchanged and nothing restarted; likewise one with a quote or a space inside;
  - `test_new_password_trims_a_pasted_carriage_return`;
  - `test_setup_sends_the_user_to_the_page_to_add_a_satellite` - `write_secrets`' text and the closing hint name the settings page;
  - `test_new_password_isnt_for_the_brain` - with `NOVA_SETUP_ROLE=brain`, exit 1;
  - `test_the_password_isnt_echoed` - it isn't in the output.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_setup_script.py`. Expected: they fail.
- [ ] **Step 3: Implement**, then the README and troubleshooting.
- [ ] **Step 4: Run them again** - expected: pass. Then `bash -n setup.sh update.sh uninstall.sh`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Give a set-up Pi a new password with setup.sh --new-password".

### Task 6: A routine's satellite

**Files:**
- Modify: `voice_assistant/settings_page.py` (`save_routine`); `voice_assistant/web/settings.html` and `settings.js` (the editor's "Runs on").
- Test: `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: `_satellites()`'s `default` from Task 1.
- Produces:
  - `save_routine` refuses a `satellite` that isn't in `brain.satellites()`, as `add_alarm` does: "There's no satellite called {name}.". An empty one is the default, as `Routines` already keeps it.
  - The editor's "Runs on" select, shown only with more than one satellite: first "The default (bedroom)", value "", then each satellite. Editing a routine starts it at its own satellite, and saving sends it, so saving on the page keeps it.
  - Run, on the page, runs it on its own satellite (`Brain.run_routine` already does - this pins it).

- [ ] **Step 1: Write the failing tests:**
  - `test_a_routine_keeps_its_satellite_when_saved_on_the_page`;
  - `test_a_routine_for_a_satellite_not_let_in_is_refused`;
  - `test_run_on_the_page_runs_a_routine_on_its_own_satellite` - two sessions; the kitchen's routine runs on the kitchen.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py -k "routine"`. Expected: the second fails. The first and third are pins - `Routines.save` already keeps a satellite, and the drop was in `settings.js`.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Let a routine be given a satellite on the page, and keep it".
