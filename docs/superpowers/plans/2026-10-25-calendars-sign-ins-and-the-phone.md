# Calendars and lists, the sign-ins, and the phone - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova is signed in to Spotify and to Google from the page, with no shell. The Music section says why there's no player when there isn't one. A new Calendars and lists section holds 0008's choices - which calendars are read and which are activities, where new events go, the main list, the weather entity and which button wakes which PC - each found in Home Assistant or Google as Nova runs, and used straight away. The page works one-handed on a phone.

**Architecture:** `SpotifySignIn` joins `spotify_controller.py`, shaped like 0008's `GoogleSignIn`: a sign-in started on the brain, one use of its state, and the pasted address checked against it. A session's Spotify looks for the sign-in again whenever it hasn't one, so a sign-in made later is used at the next command. The new section's choices are 0008's `ChoiceStore` files, and each feature gains a small read for the page and a way to choose; nothing names an entity in `config.yaml`. The phone's changes are the page's own.

**Tech stack:** Python 3.13 on the brain, spotipy 2.26.0 (already pinned), Google's OAuth and Calendar API through `requests`, Home Assistant's REST API and 0008's registry snapshot, the page's HTML, CSS and JavaScript, pytest. No new dependency.

**Spec:** `docs/decisions/0009-the-settings-page.md` - decisions 8, 13 and 14. Read it before starting any task, and 0008's decision 15, which hands 0009 these choices. This is the last of 0009's four plans (the split is said at the top of `2026-10-25-settings-straight-away.md`): it's built after 0008's three plans, whose `ChoiceStore` files, `GoogleSignIn` and `Snapshot` it uses, and after this design's plans 2 and 3, whose Undo, Stop and Copy buttons Task 6 sizes for a thumb.

## Global constraints

- **A sign-in is only finished from the address of the sign-in the page started:** the state is made on the brain with `secrets.token_urlsafe()`, kept in memory, compared with `secrets.compare_digest`, and used once whether it works or not. A restart forgets it. Each token is kept where it is now, mode 600, readable by Nova's user only, and never shown, logged or sent to the page.
- **Keys still wait for a restart.** The sign-ins are offered only when their client ID and secret are set and Nova has restarted since.
- **What's offered is found as Nova runs** - calendars from Google, lists, weather entities, Wake-on-LAN buttons and the PCs from Home Assistant and its registry - so one added later turns up by itself. One that goes drops off the page, and its choice is kept in case it comes back. Renaming is done in Home Assistant, or in Google.
- **Each choice is kept in the data folder** through `ChoiceStore`, and works straight away, as 0007's do: `calendars.json` (`{"activities": {}, "new_events": None, "whats_on": {}}`), `lists.json`, `weather.json` and `pcs.json` as 0008 made them.
- **Not on the page:** the PC's power, the things on a list, switches and media players (rule 3 - Home Assistant's dashboard has them).
- **The page's security stays as it is, never weaker:** 127.0.0.1 only, behind `tailscale serve`; only `settings_page.allow`'s logins; every POST needs `X-Nova: page` and a same-origin `Sec-Fetch-Site`; `SAFETY_HEADERS` on every answer, its content policy unchanged; `MOST_BODY`. The sign-in pages open in a new tab from a link, which the policy allows; nothing from Spotify or Google runs in the page.
- **The repo is public.** No email address, calendar id, hardware address, hostname, IP address or token in code, tests, docs or commits. A calendar's id is often an email address, and a Wake-on-LAN button's entity id carries the PC's hardware address: tests use made-up ones, such as `work@group.calendar.google.com` and `button.wake_desk_pc`.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'`. `node --check voice_assistant/web/settings.js`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **The satellite's 10 s recording cap** becomes the brain's "Longest request" (decision 11) in plan 3, `2026-10-25-satellites-on-the-page.md`, the only one of 0009's plans that needs the Pi updated - which is the user's to do. Nothing in this plan changes the satellite.
- **Safety:** never push or deploy; touch no machine except through those commands.

## Review focus

1. **A pasted address from another sign-in** - an old tab, a second Start, someone else's - is refused, and the one under way can't then be finished with it either. (Tasks 1 and 3, `test_an_address_from_another_sign_in_is_refused` and `test_a_google_address_from_another_sign_in_is_refused`.)
2. **A session that started before the sign-in** plays at its next Spotify command, with no restart. (Task 1, `test_a_session_uses_a_sign_in_made_after_it_started`.)
3. **A calendar taken out of "what's on" that's still an activity** - "what have I got on Thursday?" leaves it out, and "when's work tomorrow?" still reads it. (Task 4, `test_an_activity_out_of_whats_on_is_still_read_for_itself`.)
4. **A chosen thing that's gone** - the main list deleted in Home Assistant - drops off the page, todo is used, and the choice comes back with the list. (Task 5, `test_a_choice_for_something_gone_is_kept_for_its_return`.)
5. **Choosing something the page didn't offer** - a list id that isn't a to-do list, a calendar Nova can't add to as where new events go - is refused. (Tasks 4 and 5, `test_new_events_only_go_to_a_calendar_nova_can_add_to` and `test_only_a_list_home_assistant_has_can_be_the_main_one`.)

## Live checks before Task 1

For the controller, read-only, with the user's OK.

1. **spotipy 2.26.0's own signatures**, in the brain's venv: `SpotifyOAuth.get_authorize_url(state=None, show_dialog=False)`, `SpotifyOAuth.parse_auth_response_url(url)` giving `(state, code)`, and `get_access_token(code=None, as_dict=True, check_cache=True)`. Task 1's fakes follow what's found.
2. **The brain's `.env` has `SPOTIFY_REDIRECT_URI`** - yes or no, and whether it's setup.sh's `http://127.0.0.1:8888/callback` - which the user's Spotify app has to list.
3. **Google's sign-in on the brain**: whether Nova is signed in yet (`GoogleSignIn.signed_in()`), since plan 3 of 0008 isn't deployed.

---

### Task 1: Sign in to Spotify on the page

**Files:**
- Modify: `voice_assistant/spotify_controller.py` (`spotify_oauth`, `SpotifySignIn`, `_signed_in`, `NOT_SIGNED_IN`, `_RefreshingAuth`'s message, `authenticate_first_time` uses `SpotifySignIn`); `voice_assistant/settings_page.py` (`music_state`, two actions); `voice_assistant/web/settings.html`, `settings.js` and `settings.css` (the Music section's sign-in); `setup.sh` (the closing hint); `README.md` (Spotify's steps).
- Test: `tests/test_spotify.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `spotify_oauth() -> SpotifyOAuth` - built from `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and `SPOTIFY_REDIRECT_URI`, with today's scope, cache path and `requests_timeout`. `ValueError` without them. `SpotifyController.__init__` uses it.
  - `SpotifySignIn(oauth: SpotifyOAuth)`:
    - `start() -> str` - a new state, and `oauth.get_authorize_url(state=state)`;
    - `finish(pasted: str)` - takes and clears the state first, as `GoogleSignIn.finish` does, so it's used once whatever follows; then `parse_auth_response_url`, the state compared, `oauth.get_access_token(code, as_dict=False, check_cache=False)` and `os.chmod(cache, 0o600)`. A `SpotifyOauthError` from the parse (an address with `error=`, from Cancel) or from `get_access_token` raises `ValueError("Spotify didn't accept that sign-in - start again.")`, so the page always answers. `ValueError("That address isn't from the sign-in this page started - start again.")` for a state that's missing or another's, and `ValueError("That address hasn't got Spotify's code in it - copy all of it.")` for no code;
    - `signed_in() -> bool` - whether the cache has a token.
  - `SpotifyController._signed_in() -> bool` - `self.sp`, or, without one, `_authenticate()` again (it only reads the cache). Every `if not self.sp:` becomes `if not self._signed_in():`.
  - `NOT_SIGNED_IN = "Spotify needs signing in again on the settings page."`
  - `SettingsPage._spotify: Optional[SpotifySignIn]`, made when the player is Spotify and its keys were set at start. `music_state()` gains `"spotify": {"signed_in": bool}` then - in its own branch, before the check for a connected session, so the sign-in is offered with every satellite away. Without a session it's `{"available": False, "why": "No satellite is connected just now.", "spotify": ...}`.
  - `POST /api/spotify/start` gives `{"success": True, "url": ...}`; `POST /api/spotify/finish` `{"address": ...}` gives "Signed in to Spotify." or the `ValueError`'s sentence, and "Spotify didn't accept that sign-in - start again." for a refusal. Logged without the address.
  - The page shows "Signed in to Spotify" with "Sign in again", or "Sign in to Spotify". Signing in opens the tab in the click handler itself - `window.open("", "_blank")`, its `location` set once the URL is back - since a phone's Safari blocks a tab opened after an `await`; if that's refused, the URL is shown as a link to tap. Then it shows a field: "Spotify sends you to an address that doesn't load. Paste that address here."

- [ ] **Step 1: Write the failing tests** with a fake `SpotifyOAuth`:
  - `test_a_sign_in_gets_and_keeps_the_token_privately` - start, then finish with an address carrying that state and a code: `get_access_token` is called with the code and `check_cache=False`, and the cache is mode 600;
  - `test_an_address_from_another_sign_in_is_refused` - another state, no state, and the right state used twice: each raises, and no token is asked for;
  - `test_a_cancelled_spotify_sign_in_is_answered` - an address with `error=access_denied` gives "Spotify didn't accept that sign-in - start again.", and the state is used up;
  - `test_the_spotify_sign_in_is_offered_with_no_satellite_connected`;
  - `test_a_session_uses_a_sign_in_made_after_it_started` - a controller made with no cache plays once the cache is written;
  - `test_nova_says_spotify_is_signed_in_to_on_the_settings_page` - `NOT_SIGNED_IN`;
  - `test_the_page_signs_in_to_spotify` - start gives a URL, finish says "Signed in to Spotify.", and neither the code nor the token is in caplog or `music_state()`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_spotify.py tests/test_settings_page.py -k "sign_in or signed_in or another_sign_in or cancelled"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the page, setup.sh's hint ("For Spotify, sign in on the settings page, under Music.") and the README.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, `bash -n setup.sh`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Sign in to Spotify on the settings page, used from the next command".

### Task 2: The Music section says why there's no player

**Files:**
- Modify: `voice_assistant/music.py` (`why_no_player`); `voice_assistant/settings_page.py` (`music_state`, `play`); `voice_assistant/web/settings.html` and `settings.js` (`music-missing` shows the reason).
- Test: `tests/test_music.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `why_no_player(config) -> str`, the first that's true:
    - Spotify chosen, its client ID or secret missing: "Spotify's client ID and secret aren't set. Add them under Keys, then restart Nova.";
    - Navidrome chosen, no address: "Navidrome's address isn't set. Add it in Settings, under Music.";
    - Navidrome chosen, no `NAVIDROME_PASSWORD`: "Navidrome's password isn't set. Add it under Keys, then restart Nova.";
    - else: "Nova couldn't set up {Spotify or Navidrome} - Recent problems, in Status, says why.".
  - `music_state()` with a connected session and no player gives `{"available": False, "why": why_no_player(...)}`, and `play()` answers with it in place of "Nova has no music player set up.".
  - Spotify not being signed in isn't "no player" - Task 1's sign-in shows instead. Nor is no satellite connected, which Task 1 says as itself, never blaming the player.

- [ ] **Step 1: Write the failing tests:** `test_why_theres_no_music_is_said` - each of the four, with `monkeypatch.delenv`/`setenv`; and `test_the_page_says_why_theres_no_music`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_music.py tests/test_settings_page.py -k "why"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Say on the page why there's no music player".

### Task 3: Sign in to Google on the page

**Files:**
- Modify: `voice_assistant/google_calendar.py` (`NEEDS_SIGN_IN`; `main` and its command go); `voice_assistant/settings_page.py` (two actions, and a `calendars_state` for Task 4 to grow); `voice_assistant/web/settings.html`, `settings.js` and `settings.css` (the Calendars and lists section, after Batteries, with the sign-in); `README.md` (Google Calendar's steps); `setup.sh` (any hint that names the command).
- Test: `tests/test_google_calendar.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: 0008's `GoogleSignIn.start()`, `finish(pasted)`, `signed_in()`, `ClientRefused` and `SignInNeeded`, and `Brain.calendar.sign_in`.
- Produces:
  - `NEEDS_SIGN_IN = "I need signing in to Google again. That's done on the settings page."`
  - The command on the brain goes, as decision 8 says, and Spotify's (`python -m voice_assistant.spotify_controller`) stays as the README's fallback - **pending the user's answer** on whether that's the right way round.
  - `SettingsPage.calendars_state() -> Dict` and `GET /api/calendars`: without `brain.calendar`, `{"available": False, "message": "Google's client ID and secret go under Keys, then restart Nova."}`; else `{"available": True, "signed_in": bool}`.
  - `POST /api/google/start` gives `{"success": True, "url": ...}`; `POST /api/google/finish` `{"address": ...}` gives "Signed in to Google.", or the `ValueError`'s sentence; for `ClientRefused`, "Google didn't accept the client ID or secret - check both under Keys."; for `SignInNeeded`, "Google didn't accept that sign-in - start again."; for `requests.RequestException`, "Couldn't reach Google - try again.". Logged without the address.
  - The section shows "Signed in to Google" with "Sign in again", or "Sign in to Google", working as Task 1's does, the tab opened in the click handler.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_page_signs_in_to_google` - with a fake `GoogleSignIn`: start, then finish; nothing of the address in caplog;
  - `test_a_google_address_from_another_sign_in_is_refused` - the page passes on `GoogleSignIn.finish`'s refusal, and no token is asked for;
  - `test_each_google_failure_is_said_plainly` - the three exceptions give their sentences;
  - `test_nova_says_google_is_signed_in_to_on_the_settings_page` - `NEEDS_SIGN_IN`;
  - `test_the_command_on_the_brain_is_gone` - `voice_assistant.google_calendar` has no `main`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py tests/test_settings_page.py -k "google or command"`. Expected: they fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Sign in to Google on the settings page, in place of the command on the brain".

### Task 4: Calendars - what's on, activities, and where new events go

**Files:**
- Modify: `voice_assistant/google_calendar.py` (`whats_on_ids`, `agenda`, `find`, `choose`, `set_new_events`, `from_env`'s defaults); `voice_assistant/settings_page.py` (`calendars_state`, two actions); `voice_assistant/web/settings.js` (the calendars' rows).
- Test: `tests/test_google_calendar.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: Task 3's `calendars_state`, and 0008's `calendars()`, `known()`, `activity_calendars()` and `_calendar_to_add_to`.
- Produces:
  - "An activity" starts as 0008 built it - every calendar but the primary, Google's own and one named by an email address - **pending the user's answer** on whether that's right, as the spec's words say "all but Google's primary".
  - `calendars.json`'s defaults gain `"whats_on": {}` - a calendar's id to False when it's out. All are in, to begin with.
  - `GoogleCalendar.left_out() -> Set[str]` - the ids chosen out of what's on. `events(first, days, calendar_ids=None, leave_out: Set[str] = frozenset())` works out the calendars to read after its own `calendars()` call, as now, then drops those in `leave_out` - so a calendar that's gone is still skipped as today, and the first read after a restart leaves out the right ones. `agenda()` and `find()` pass `leave_out=self.left_out()`. `activity()` reads its own calendar as now, in or out.
  - `GoogleCalendar.choose(calendar_id: str, whats_on: Optional[bool] = None, activity: Optional[bool] = None)` - for a calendar in `known()`; `ValueError("Google isn't showing that calendar just now.")` otherwise.
  - `GoogleCalendar.set_new_events(calendar_id: str)` - only a `writable` one: `ValueError("Nova can only add to your own calendars.")` otherwise.
  - `calendars_state()` signed in gains `"calendars": [{"id", "name", "primary", "writable", "whats_on", "activity"}]` from `calendars()` (read afresh once it's five minutes old), and `"new_events"` - the chosen one if it's still writable, else the primary's id. A Google failure gives `{"available": True, "signed_in": True, "message": "Nova can't reach Google just now."}`.
  - `POST /api/calendars` `{"id", "whats_on"?, "activity"?}` and `POST /api/calendars/new-events` `{"id"}` - each "Saved. It's used straight away.", under `SettingsPage._saving`. Types are checked as `set_battery` checks them.
  - Each row: the name, "In what's on" and "An activity" switches, and new events' calendar as a select of the writable ones.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_calendar_out_of_whats_on_isnt_read_for_it` - two calendars, one out: only the other's events are asked for, on the first read after a restart too (nothing known yet);
  - `test_a_calendar_gone_doesnt_stop_whats_on_with_one_left_out` - one out and another gone: the agenda still answers;
  - `test_an_activity_out_of_whats_on_is_still_read_for_itself`;
  - `test_an_activity_chosen_on_the_page_is_heard_at_the_next_request` - `activity_words()` has it straight after `choose(..., activity=True)`;
  - `test_new_events_only_go_to_a_calendar_nova_can_add_to`;
  - `test_new_events_go_to_the_primary_until_another_is_chosen` - and to the chosen one after, with no restart;
  - `test_a_calendar_google_no_longer_lists_keeps_its_choice` - it drops off `calendars_state()`, and is out of what's on again when it's back;
  - `test_the_page_shows_each_calendar_with_its_choices`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py tests/test_settings_page.py -k "whats_on or activity or new_events or calendar or gone"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Choose on the page which calendars are read, which are activities, and where new events go".

### Task 5: Lists, sending to, the weather and the PCs

**Files:**
- Modify: `voice_assistant/lists.py` (`overview`, `choose_main`); `voice_assistant/weather.py` (`entities`, `choose`); `voice_assistant/pc.py` (`PCPower.overview`, `choose_wake`); `voice_assistant/settings_page.py` (`home_choices_state`, three actions); `voice_assistant/web/settings.js` and `settings.css` (the section's other parts).
- Test: `tests/test_lists.py`, `tests/test_weather.py`, `tests/test_pc.py`, `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: 0008's `HomeAssistant.todo_lists()`, `weather_entities()` and `app_devices()`, `AppDevice.from_home`, `batteries.is_phone`, `find_pcs`, `Snapshot.wake_buttons()`, and each feature's `ChoiceStore`.
- Produces:
  - `Lists.overview() -> List[Dict]` - each of `todo_lists()` with `"main": bool` from `_main`. `Lists.choose_main(list_id: str)` - `ValueError("Home Assistant isn't showing that list just now.")` unless it's in `todo_lists()`.
  - `Weather.entities() -> List[Dict]` - `{"id", "name", "chosen": bool}`, the chosen one as `forecast` would use it. `Weather.choose(entity_id: str)`, a weather entity only.
  - `PCPower.overview() -> Dict` - `{"pcs": [{"device", "name", "wake"}], "buttons": [{"id", "name"}]}` from `find_pcs` and `wake_buttons()`, named by their friendly names. `PCPower.choose_wake(device: str, button: str)` - a HASS.Agent device and a Wake-on-LAN button only.
  - `SettingsPage.home_choices_state() -> Dict` and `GET /api/home-choices`: `{"available": True, "lists": [...], "sending": [...], "weather": [...] or None, "pcs": {...} or None}` - weather only with more than one entity, PCs only with more than one PC or more than one button. `"sending"` is each device Nova can send to, `{"name", "how"}`: the phone "Through ntfy, to the topic under Notifications" (or "Through ntfy, once a topic for your phone is set under Notifications"), and every other app device "Through Home Assistant". Without Home Assistant, `{"available": False, "message": ...}` as `batteries_state` says it; unreachable, "Nova can't reach Home Assistant just now.".
  - `POST /api/lists/main` `{"id"}`, `POST /api/weather` `{"id"}`, `POST /api/pcs/wake` `{"device", "button"}` - each "Saved. It's used straight away.", under `_saving`.
  - The page: each list with its count ("3 things to do") and its other names ("Also: groceries"), the main one marked, with "Make main"; sending as plain rows, nothing to choose; the weather and PCs as selects, only when there's a choice.

- [ ] **Step 1: Write the failing tests:**
  - `test_each_list_comes_with_its_count_other_names_and_whether_its_main`;
  - `test_only_a_list_home_assistant_has_can_be_the_main_one`;
  - `test_a_choice_for_something_gone_is_kept_for_its_return` - the chosen list goes: `overview()` marks todo, `choices.get("main")` is unchanged, and it's main again when it's back;
  - `test_the_weather_choice_is_only_offered_with_more_than_one` - and choosing one is used at the next forecast;
  - `test_the_pcs_choice_is_only_offered_with_more_than_one_of_either` - and choosing is used at the next "turn on the desk PC";
  - `test_sending_says_how_each_device_is_reached`;
  - `test_home_choices_without_home_assistant_say_so`.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_lists.py tests/test_weather.py tests/test_pc.py tests/test_settings_page.py -k "overview or main or choice or sending or home_choices"`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then `node --check`, the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Choose the main list, the weather and which button wakes which PC on the page".

### Task 6: On a phone

**Files:**
- Modify: `voice_assistant/web/settings.html`, `settings.js` and `settings.css`.
- Test: none of its own - `node --check voice_assistant/web/settings.js`, and `tests/test_settings_page.py` for the page's files still being served.

**Interfaces:**
- Consumes: `state()`'s `satellites` and `satellite`, and `status.problems` (still up to `PROBLEMS_SHOWN = 8` from the brain).
- Produces:
  - With more than one satellite, the masthead names the one looked after - "Nova · bedroom" - and so do the headings of Sound, Timers and alarms, and Devices: "Sound · bedroom".
  - Recent problems shows the latest three, each cut to two lines (`-webkit-line-clamp: 2`), and a "Show all" button that shows the rest whole.
  - Undo, Stop, a password's Copy, "Make main", and the sign-ins' buttons are at least 44 px each way.
  - Everything new in 0009 fits at 390 px wide with no sideways scroll.

- [ ] **Step 1: Make the changes**, in the page's existing style. There's no RED: the page has no JavaScript tests, so the commit says so.
- [ ] **Step 2: Run** `node --check voice_assistant/web/settings.js` and `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py`. Expected: clean, and pass.
- [ ] **Step 3: Commit** - "Make the page work one-handed on a phone".

The look at 390 and 1440 wide is the user's to check, on their phone and a laptop, once it's deployed.
