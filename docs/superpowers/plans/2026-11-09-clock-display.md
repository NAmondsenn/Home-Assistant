# Clock Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a flashed Mi Smart Clock show Nova's pages (home, coming up, now playing) with listening and speaking overlays, change pages by touch, make Nova listen on a held finger, and step Nova's volume with its buttons.

**Architecture:** The brain gains a display service: a store of clocks and their keys (`displays.py`), a feed that builds each satellite's display state and wakes waiting clocks when it changes (`display_feed.py`), and a small HTTP server on its own LAN port that serves the page and its API behind a per-clock key (`display_server.py`). The page (`web/display.*`) long-polls the state, draws it, and sends holds and volume presses. Sessions gain change hooks and a "listen now". The settings page gets a Clock displays section.

**Builder's calls made here:** pushing is a long-poll (`/display/api/state?since=`), not WebSocket or SSE, because the brain has no WebSocket server, the page must send its key in a header, and a held request is plain standard library. "What Nova heard" is the final text once the recording ends, since the speech-to-text gives no partial text today. The display server listens on its own port (`display.port`, off by default) on every address, because the settings page and satellite port listen on 127.0.0.1 only and the clock is on home Wi-Fi.

**Tech Stack:** Python 3 standard library (`http.server`, `threading`), the existing `requests`-based Home Assistant client and Google calendar; plain HTML, CSS and JavaScript for the page; pytest on the Pi; Node 22 (`~/tools/node`) only for the gesture test.

**Spec:** `docs/decisions/0013-the-clock-display.md` (mockup: `docs/decisions/0013-clock-display-mockup.html`). Read both before any task.

## Global Constraints

- Work in the worktree `/home/nathaniel/work/Home-Assistant-clock`, branch `clock-display`. Never touch `/home/nathaniel/work/Home-Assistant` (another session's music build lives there). No `git stash`.
- Tests: `nice -n 19 timeout 900 bash ~/nova/tools/pi_test_clock.sh <pytest args>` (copies this worktree to the Pi and runs pytest there). Check `cat /proc/loadavg` first and wait if the first number is above 3. Run only the task's test file while working; one full run (`-x`) before the task's commit; never two runs at once; no repeat runs for flakiness. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/clock-work && /tmp/nova-check-pytest/bin/ruff check .'` after a test run has copied the tree.
- Node (gesture test only): `nice -n 19 timeout 120 ~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/`. No equality asserts on large objects or DOM nodes.
- Never `ps` on the Pi; never deploy, push or restart anything.
- Code style (the user's): full-sentence comments in British spelling with spaced " - " dashes, explaining why, with concrete quoted examples; Google-style `Args:`/`Returns:`/`Raises:` docstrings; module docstrings like the existing ones, pointing at `docs/decisions/0013`; no comments on obvious lines; no new dependencies; no hard-coded hosts or paths; keep each file's line endings.
- Commits: one per task, a plain sentence title, ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Each task adds tests that fail on the old code.
- Nothing in the repository names the user's hosts, holds a key, or shows his calendar.
- The name `clock` is taken (timers and alarms: `clock.py`, `clock_*.py`, the page's `"clock"` state); this feature is "display" in every name.
- Volume buttons never change the `alarm` volume. A tap or swipe never makes Nova listen. The clock's own speaker and microphones are never used.

## Review Focus

1. A wrong, removed or missing key: every `/display/api/*` route answers 401, repeated wrong keys from one address are slowed by `WrongTries`, and a clock removed on the settings page is refused at once, mid long-poll included. (Task 5 tests.)
2. The clock's satellite isn't connected, or the brain restarts: the state says `"connected": false`, a hold answers `{"listening": false}`, a volume press answers 409, and nothing raises. (Tasks 4 and 5 tests.)
3. A hold while Nova is already in a conversation: `listen_now()` returns False and makes no second feed; an "end" with no hold, a second "end", or an "end" after the conversation finished does nothing. (Task 2 tests.)
4. Home Assistant, Google or Spotify failing during a poll: the feed keeps the last good weather and events, shows no music rather than crashing, and the poll thread carries on. (Task 4 tests.)
5. The muted satellite: a hold still makes Nova listen (his answer), and the state carries `"muted": true` for the page's mark. (Tasks 2 and 4 tests.)

---

### Task 1: The clocks and their keys

**Files:**
- Create: `voice_assistant/displays.py`
- Test: `tests/test_displays.py`

**Interfaces:**
- Produces: `class Displays(path: str = os.path.join(paths.CONFIG_DIR, "displays.json"))` with
  - `listed() -> List[Dict]`: each `{"id": str, "name": str, "satellite": Optional[str], "brightness": int, "home_after": int}`, never the key or its hash.
  - `add(name: str, satellite: Optional[str]) -> Tuple[Dict, str]`: the new entry (as listed) and its key, `secrets.token_urlsafe(32)`, shown once.
  - `renew(display_id: str) -> str`: a new key; the old one stops working.
  - `change(display_id: str, changes: Dict) -> Dict`: only `satellite`, `brightness` (int 0-100), `home_after` (int seconds, 10-3600) may change; returns the entry.
  - `remove(display_id: str) -> None`.
  - `find(key: str) -> Optional[Dict]`: the entry whose key this is, else None.
  - Raises `ValueError` with a sentence for the page for a bad name, unknown id or bad value; `OSError` if it couldn't save.
  - Constants: `DEFAULT_BRIGHTNESS = 70`, `DEFAULT_HOME_AFTER = 60`, `MOST_DISPLAYS = 10`.

- [ ] **Step 1: Write the failing tests** in `tests/test_displays.py`:
  - `test_add_lists_the_clock_without_its_key`: `entry, key = displays.add("Desk", "pi")`; `listed()` has one entry with `name == "Desk"`, `satellite == "pi"`, `brightness == 70`, `home_after == 60`, and `key` appears nowhere in `json.dumps(displays.listed())` or in the saved file's text.
  - `test_find_knows_only_the_current_key`: `find(key)["id"] == entry["id"]`; `find("wrong")` is None; after `renew(id)` the old key finds None and the new one finds the entry.
  - `test_remove_stops_the_key`: after `remove(id)`, `find(key)` is None and `listed() == []`.
  - `test_change_keeps_to_its_limits`: `change(id, {"brightness": 40, "home_after": 120})` saves both; `{"brightness": 101}`, `{"home_after": 5}`, `{"name": "x"}` and an unknown id each raise ValueError.
  - `test_kept_across_a_restart`: a second `Displays(same path)` finds the same key.
  - `test_names_must_be_sensible`: empty or 40-character names raise ValueError; an 11th clock raises ValueError.
- [ ] **Step 2: Run** `bash ~/nova/tools/pi_test_clock.sh tests/test_displays.py` - expect import failure.
- [ ] **Step 3: Implement `Displays`** in `voice_assistant/displays.py`. Keys are stored as SHA-256 hex, compared with `hmac.compare_digest`; ids are a short slug of the name made unique; writes go through `storage.save_json` under a `threading.Lock`; the file is read once at start (`storage.load_json`). Follow `satellite_passwords.py` for shape and `page_password.Logins` for hashing.
- [ ] **Step 4: Run the task's tests** - expect pass; then the full suite with `-x`.
- [ ] **Step 5: Commit** "Keep the clock displays and their keys".

### Task 2: Sessions say what they're doing and listen when held

**Files:**
- Modify: `voice_assistant/session.py` (`activity` at `:148` and its setters; `_handle_interaction` around `:533-625`)
- Test: `tests/test_session.py`

**Interfaces:**
- Produces on `Session`:
  - `activity` becomes a property; setting it to a new value calls every watcher.
  - `heard: str` - what Nova last heard in this conversation (the text shown as `You: ...` at `:625`), `""` when a new listen starts; setting it calls the watchers.
  - `watch(watcher: Callable[[], None]) -> None` and `unwatch(watcher) -> None`. Watchers are called from the thread that made the change, sometimes with the announcement lock held, so they only take note (never block, announce, run a routine or listen_now); one raising is logged, never passed on.
  - `listen_now() -> bool`: if no conversation is running (`self._idle.is_set()`) and no hold is open, starts one exactly as a wake does - puts `{"talked_over": False, "pre_roll": 0, "feed": self.satellite.listen()}` on `self.satellite.wakes` and keeps the feed - and returns True; otherwise False. Muted or not makes no difference.
  - `stop_held() -> None`: if a held feed is open, `self.satellite.stop_listening(feed)` and forget it; otherwise nothing.

- [ ] **Step 1: Write the failing tests** in `tests/test_session.py`, using the existing `Pi`, `connect`, `make_session` and `running` helpers:
  - `test_a_hold_starts_a_conversation_like_a_wake`: with the session running and idle, `session.listen_now()` is True, the Pi receives a `listen` message, and once the Pi answers with speech the session reaches `heard` equal to the fake speech-to-text's text.
  - `test_a_hold_works_while_muted`: `session.set_muted(True)`; `listen_now()` is True and `listen` is sent.
  - `test_no_second_hold_during_a_conversation`: while a conversation is running, `listen_now()` is False and no second `listen` is sent.
  - `test_letting_go_ends_the_recording`: after `listen_now()`, `stop_held()` sends `stop_listening` for that recording; a second `stop_held()`, and one with no hold, send nothing and raise nothing.
  - `test_watchers_hear_each_change`: a watcher appending `session.activity` sees `"listening"` then later `"idle"` in a wake's conversation; a watcher that raises doesn't stop the conversation; after `unwatch` it's no longer called.
- [ ] **Step 2: Run** `bash ~/nova/tools/pi_test_clock.sh tests/test_session.py -k "hold or watcher or letting_go"` - expect failures.
- [ ] **Step 3: Implement** in `session.py`: rename the attribute to `_activity` behind a property with a setter that notifies only on a real change; set `heard` where the final text is known (`:625`) and clear it where listening starts; `listen_now` and `stop_held` with a lock around the held feed. Clear the held feed when the conversation it started ends.
- [ ] **Step 4: Run** the task's tests, then the full suite with `-x`.
- [ ] **Step 5: Commit** "Let a session be watched and made to listen by a held finger".

### Task 3: Today's weather and what Jellyfin is playing, as data

**Files:**
- Modify: `voice_assistant/weather.py`, `voice_assistant/home_assistant.py`
- Test: `tests/test_weather.py`, `tests/test_home_assistant.py`

**Interfaces:**
- Produces:
  - `Weather.today(now: Optional[datetime] = None) -> Optional[Dict]`: `{"temperature": float, "condition": str, "high": Optional[float], "low": Optional[float]}` from the chosen entity (`_entity`), its `temperature` attribute and today's daily forecast (`home.forecasts(entity, "daily")`, `_on(entries, "today")`); `condition` in words from `CONDITIONS` (e.g. `"partlycloudy"` -> `"partly cloudy"`), capitalised by the page not here. None when there's no weather entity. Raises what `forecast` raises.
  - `HomeAssistant.jellyfin_media() -> List[Dict]`: Jellyfin's media players that are playing or paused (recognised by `integration_entities('jellyfin')` in a new `JELLYFIN_MEDIA_TEMPLATE`), each `{"entity_id", "name", "area", "state" ("playing"|"paused"), "title", "artist", "album", "picture" (the `entity_picture` path or None), "position" (float or None), "duration" (float or None), "updated" (epoch seconds of `media_position_updated_at` or None)}`. Timeout `PLAYING_MEDIA_TIMEOUT`. Raises as `playing_media` does.
  - `HomeAssistant.picture(path: str) -> Tuple[bytes, str]`: GETs `path` from Home Assistant with Nova's token and returns the body and content type; `ValueError` unless `path` starts with `/api/media_player_proxy/`; at most 2 MB.

- [ ] **Step 1: Write the failing tests**:
  - `tests/test_weather.py::test_today_gives_numbers_and_words`: with the existing `FakeHome` giving a chosen entity at 9.4 degrees, condition `rainy`, and today's forecast high 11, low 5: `today() == {"temperature": 9.4, "condition": "rainy", "high": 11, "low": 5}`; with no forecast for today, high and low are None; with no entity, None.
  - `tests/test_home_assistant.py::test_jellyfin_media_reads_the_template`: with the existing `FakeSession` answering the template with one playing and one paused player, both come back with every field above; the template sent contains `integration_entities('jellyfin')`.
  - `tests/test_home_assistant.py::test_picture_only_fetches_media_player_pictures`: `picture("/api/media_player_proxy/media_player.tv?token=x")` returns the fake body and `image/jpeg`; `picture("/api/states")` raises ValueError without a request.
- [ ] **Step 2: Run** the three tests - expect failures.
- [ ] **Step 3: Implement** the three functions; reuse `_entity`, `_on`, `_is_number`, `CONDITIONS` and `_template`/`_request`.
- [ ] **Step 4: Run** those test files, then the full suite with `-x`.
- [ ] **Step 5: Commit** "Give today's weather and Jellyfin's playing as data for the clock".

### Task 4: The display feed

**Files:**
- Create: `voice_assistant/display_feed.py`
- Test: `tests/test_display_feed.py`

**Interfaces:**
- Consumes: Task 2's `watch`, `unwatch`, `activity`, `heard`; Task 3's `Weather.today`, `HomeAssistant.jellyfin_media`; existing `brain.session_for(name)`, `brain.weather`, `brain.calendar` (`events(first, days, leave_out=calendar.left_out())`), `brain.home`, `session.music_player.now_playing()`, `session.muted`, `session.home.area`, `session.volume`.
- Produces: `class DisplayFeed(brain, now: Callable[[], float] = time.time, every: float = 1.0)` with
  - `start()`, `stop()` - one daemon poll thread.
  - `state(satellite: str) -> Dict` - the current state for a satellite, built on demand if never polled.
  - `wait(satellite: str, since: int, timeout: float) -> Dict` - returns at once if the state's `version` differs from `since`, else waits up to `timeout` for a change, then returns the state.
  - `poke(satellite: str) -> None` - rebuild that satellite's state now (used after a volume press).
  - State shape - the contract with the page, exactly these keys:
    ```
    {"version": int, "connected": bool, "nova": "idle"|"listening"|"thinking"|"speaking",
     "heard": str, "muted": bool, "now": float (brain epoch seconds), "utc_offset": int (minutes),
     "weather": {"temperature","condition","high","low"} | None,
     "events": [{"title": str, "start": "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM", "all_day": bool}],  (next 5, none already over)
     "music": {"source": "Navidrome"|"Spotify"|"Jellyfin", "room": str|None, "title": str, "artist": str|None,
               "album": str|None, "playing": bool, "position": float, "duration": float|None,
               "at": float (epoch seconds the position was true), "cover": str|None (a /display/api/cover?... path)} | None,
     "volume": {"category": "music"|"general", "percent": int}}
    ```
  - Rules: Nova's own music (the satellite's `now_playing()`) comes first; only when it is None, a Jellyfin player whose state is `playing`, preferring one in the satellite's room (`session.home.area`), then by entity id. Calendar names and ids are never in the state. `version` changes only when something other than `now` and the music's `position`/`at` changes (the page moves the progress bar itself). Refresh: activity, heard and muted at once through `watch` (and every poll); Nova's music every 2 s (Navidrome) or 5 s (Spotify, `needs_internet`); Jellyfin every 5 s; weather every 10 minutes; events every 5 minutes. A failure keeps the last good weather and events, logs once per kind until it works again, and gives no music for that source.

- [ ] **Step 1: Write the failing tests** in `tests/test_display_feed.py`, with small fakes (a `FakeBrain` with `session_for`, `weather`, `calendar`, `home`; a `FakeSession` with `watch`/`unwatch`, `activity`, `heard`, `muted`, `music_player`, `home.area`, `volume`), `every` small and a fake `now`:
  - `test_state_has_the_contract_keys` - every key above, `connected` False for an unknown satellite.
  - `test_activity_change_wakes_a_waiter` - a `wait(sat, v, 5)` on another thread returns within 1 s after the fake session's watchers are called with `nova` now `"listening"`, and the version differs.
  - `test_novas_music_wins_over_jellyfin` and `test_jellyfin_in_the_room_first` - per the rules above; a paused Jellyfin player is never shown.
  - `test_position_alone_doesnt_change_the_version`.
  - `test_events_are_the_next_five_not_over` - from seven fake events, one already over, five come back in order, without `calendar` or `calendar_id`, all-day ones as dates.
  - `test_failures_keep_the_last_good` - weather then raising `requests.RequestException` keeps the earlier weather; Spotify's `now_playing` raising gives `music` None; the thread is still alive.
  - `test_muted_is_carried` - `muted` follows the session.
- [ ] **Step 2: Run** the file - expect import failure.
- [ ] **Step 3: Implement** `DisplayFeed` with one `threading.Condition` guarding a per-satellite dict of states; covers are paths `"/display/api/cover?kind=nova&id=<cover id>"` and `"/display/api/cover?kind=home&id=<entity_id>"`. Watch each connected session once (watch on first sight, unwatch when it goes).
- [ ] **Step 4: Run** the file, then the full suite with `-x`.
- [ ] **Step 5: Commit** "Build each satellite's display state and wake waiting clocks".

### Task 5: The display server

**Files:**
- Create: `voice_assistant/display_server.py`
- Modify: `voice_assistant/settings_page.py` (only to import `SAFETY_HEADERS`/`MOST_BODY` if not already importable; no behaviour change)
- Test: `tests/test_display_server.py`

**Interfaces:**
- Consumes: Task 1's `Displays.find`; Task 2's `listen_now`, `stop_held`; Task 4's `DisplayFeed.wait`, `state`, `poke`; existing `settings_page.volume_levels`, `settings_page.change_volume`, `session.boop`, `session.music_player.cover()`, Task 3's `HomeAssistant.picture`, `page_password.WrongTries`.
- Produces: `class DisplayServer(brain, displays, feed, port: int, address: str = "0.0.0.0")` with `start()`, `stop()`, `port` (the bound port, for tests with `port=0`), and `@classmethod from_config(cls, config, brain, displays, feed) -> Optional[DisplayServer]` (None when `display.port` is 0). Routes:
  - `GET /display`, `/display.js`, `/display-gestures.js`, `/display.css`, `/Outfit.woff2`: static from `voice_assistant/web/`, no key, with `SAFETY_HEADERS`.
  - `GET /display/api/state?since=<int>`: header `Authorization: Bearer <key>`; answers `feed.wait(display's satellite, since, 25)` merged with the display's `brightness` and `home_after`. A display with no satellite uses the brain's default satellite.
  - `POST /display/api/hold` `{"action": "start"|"end"}` -> `{"listening": bool}` (`listen_now()` / `stop_held()`; not connected -> `{"listening": false}`).
  - `POST /display/api/volume` `{"direction": "up"|"down"}` -> `{"category", "percent"}`; category `music` when the satellite's `now_playing()` says `"playing": True`, else `general`; one step is `session.volume.step` percentage points, clamped 0-100, applied with `change_volume`, then `session.boop(...)` as `SettingsPage.set_volume` does, then `feed.poke`. Never `alarm`. Not connected -> 409.
  - `GET /display/api/cover?kind=nova|home&id=...`: the satellite's `music_player.cover()` (only if its id matches the current cover) or `brain.home.picture(...)` for the Jellyfin player's current `picture`; 404 otherwise.
  - Any wrong or missing key -> 401 and `WrongTries.attempt` by client address; while that address is waiting -> 429. POST bodies capped at 1 KB and must be JSON objects (400 otherwise). Unknown paths -> 404.

- [ ] **Step 1: Write the failing tests** in `tests/test_display_server.py`, serving on `port=0` with fakes for the brain, feed and session and a real `Displays` in `tmp_path`, asking with `urllib.request`:
  - `test_static_page_needs_no_key` and `test_api_needs_the_key` (401 without, 200 with).
  - `test_removed_clock_is_refused_at_once`.
  - `test_wrong_keys_are_slowed` (after the `WrongTries` limit, 429).
  - `test_state_carries_brightness_and_home_after`.
  - `test_hold_start_and_end` (calls reach the fake session; not connected gives `{"listening": false}`).
  - `test_volume_steps_music_while_playing_else_voice` (and never `alarm`; not connected gives 409).
  - `test_bad_bodies_are_refused` (non-JSON, an array, over 1 KB, an unknown action or direction -> 400).
- [ ] **Step 2: Run** the file - expect import failure.
- [ ] **Step 3: Implement** `DisplayServer` on `ThreadingHTTPServer` with `daemon_threads`, following `SettingsPage._serve`/`stop` and its `_Handler` style. Look the key up on every request, so a removed clock fails mid-session.
- [ ] **Step 4: Run** the file, then the full suite with `-x`.
- [ ] **Step 5: Commit** "Serve the clock display and its API behind each clock's key".

### Task 6: Wiring, configuration and the settings section

**Files:**
- Modify: `voice_assistant/brain.py` (construct after `settings_page` at `:363`; start in `run()` `:1463`; stop in `_clean_up()` `:1542`), `config.yaml` (new `display:` block with `port: 0`), `voice_assistant/config.py` (`LIMITS[("display", "port")] = Limit("number", 0, 65535, True)` or as the file's pattern needs), `voice_assistant/settings_page.py` (`EDITABLE` entry `("display", "port", "Port the clock displays connect on", "restart")`, `KEYS`, state, actions), `voice_assistant/web/settings.html`, `voice_assistant/web/settings.js`
- Test: `tests/test_settings_page.py`, `tests/test_brain.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 5.
- Produces:
  - `Brain.displays: Displays`, `Brain.display_feed: DisplayFeed`, `Brain.display_server: Optional[DisplayServer]`.
  - On `SettingsPage`: `state()["displays"]` = `Displays.listed()` plus `"port"`; `add_display(name: str, satellite: Optional[str], who: str) -> Dict` returning `{"display": entry, "key": key, "path": "/display#key=" + key}`; `change_display(display_id: str, changes: Dict, who: str) -> Dict`; `new_display_key(display_id: str, who: str) -> Dict` (same shape as add); `remove_display(display_id: str, who: str) -> Dict`. A satellite not in `_satellite_names()` is refused with a sentence. Each logged with `who`, as the satellite actions are.
  - `ACTIONS` routes `/api/displays/add`, `/api/displays/change`, `/api/displays/key`, `/api/displays/remove`.
  - The section "Clock displays" in `settings.html`/`settings.js`: one row per clock with its name, a satellite select (pattern: `showRunsOn`, `settings.js:1889`), a brightness slider (0-100), "Back to Home after" seconds, "New key" and "Remove"; an add form (name, satellite). After add or new key, the key and the path are shown once with a note to open `http://<the brain's address>:<port>/display#key=...` in the kiosk app. No inline script or style (the page's CSP).

- [ ] **Step 1: Write the failing tests**:
  - `tests/test_settings_page.py`: `test_add_display_shows_the_key_once` (key in the add answer, not in `state()`); `test_display_satellite_must_be_known`; `test_change_display_saves_brightness`; `test_remove_display`; `test_display_routes_need_the_page_headers` (through `_ask` without `X-Nova: page` -> refused as other POSTs are).
  - `tests/test_brain.py`: `test_display_server_off_by_default` (port 0 -> `display_server` None, nothing listening) and `test_display_server_starts_and_stops` (a free port from the test, started in `run`, stopped in `_clean_up`).
  - `tests/test_config.py`: the `display.port` limit accepts 0 and 8766 and refuses 70000.
- [ ] **Step 2: Run** the three files - expect failures.
- [ ] **Step 3: Implement** the wiring, configuration, page methods and routes, then the section's HTML and JS. Check the settings page by hand in a desktop browser if possible; at least run `~/tools/node/bin/node --check voice_assistant/web/settings.js`.
- [ ] **Step 4: Run** the files, then the full suite with `-x`.
- [ ] **Step 5: Commit** "Wire the clock displays into the brain and the settings page".

### Task 7: The page

**Files:**
- Create: `voice_assistant/web/display.html`, `voice_assistant/web/display.css`, `voice_assistant/web/display.js`, `voice_assistant/web/display-gestures.js`
- Test: `tests/web/display_gestures.test.mjs` (Node), `tests/test_display_server.py` (that each static file is served)

**Interfaces:**
- Consumes: Task 5's routes and Task 4's state shape exactly.
- Produces:
  - `display-gestures.js` exports (as a plain script defining `window.NovaGestures`, and `module.exports` when present, so Node can test it) `classify(points) -> "tap"|"hold"|"swipe-left"|"swipe-right"|"none"` where `points` is `[{x, y, t}]` from touch start to end, and the constants `HOLD_MS = 450`, `TAP_MS = 300`, `STILL_PX = 15`, `SWIPE_PX = 60`, `SWIPE_MS = 600`. A hold is decided while the finger is still down: `isHold(points, nowMs) -> bool`.
  - `display.js`: reads the key from `location.hash` (`#key=...`) into `localStorage` and drops the hash; long-polls `/display/api/state?since=<version>` with the `Authorization` header; on failure shows the offline mark and retries after 2, 4, 8, 10, 10... seconds, while the time keeps ticking from the clock's own clock (using the last `now`/`utc_offset` difference when known).
  - Pages Home, Coming up, Now playing as in the mockup (800 x 480, dark, dots), 24-hour time, dates like "Sunday 8 November"; events show "Mon 10:00", "Wed" for all-day this week, "2 Dec" beyond it.
  - Swipe left or right changes page; a tap goes to the next page; a hold POSTs `hold` `start` once the hold is decided and `end` when the finger lifts (or the touch is cancelled). The Now playing bar moves locally from `position` + (now - `at`) while `playing`.
  - Music starting (`music` from None or not playing to playing, compared with the last state) shows Now playing; music stopping (to None or not playing) returns to Home after `home_after` seconds if still on Now playing.
  - Overlays: `nova` `listening` or `thinking` -> the orb with "Listening..." and `heard` under it when non-empty; `speaking` -> a CSS sound-wave animation; `idle` -> none. A small muted mark shows while `muted` and no overlay is up.
  - Volume: when `window.fully` exists, `fully.bind("volumeUp", "novaVolume('up')")` and the same for down, and `novaVolume` POSTs `volume` and shows a level bar for 1.5 s. In a desktop browser the ArrowUp/ArrowDown keys do the same, for testing. Brightness: when `brightness` changes and `window.fully` exists, `fully.setScreenBrightness(Math.round(brightness * 255 / 100))`.
  - Light on the clock (1 GB RAM): no framework, CSS animations only, the album art as an `<img>` of the cover path; reuse `Outfit.woff2`.

- [ ] **Step 1: Write the failing tests**: `tests/web/display_gestures.test.mjs` - a 100 ms still touch is `"tap"`; a 500 ms still touch is `"hold"` and `isHold` is false at 300 ms and true at 460 ms; 80 px left in 200 ms is `"swipe-left"`; 80 px right is `"swipe-right"`; 30 px in 200 ms is `"none"`; a 500 ms touch that moved 40 px is never `"hold"` (assert with `assert.equal` on the strings only). `tests/test_display_server.py::test_every_page_file_is_served` - each of the four files answers 200 with the right content type.
- [ ] **Step 2: Run** the Node test (command in Global Constraints) and the server test - expect failures.
- [ ] **Step 3: Implement** the four files.
- [ ] **Step 4: Run** both, `node --check` on each JS file, then the full suite with `-x`. If a desktop browser is available to the implementer, open the page against a fake server at 800 x 480 and look; otherwise say so in the report.
- [ ] **Step 5: Commit** "Draw the clock display: pages, touch, overlays and buttons".

### Task 8: Setting it up, written down

**Files:**
- Create: `docs/clock-display.md`
- Modify: `README.md` (one short paragraph pointing at it), `docs/roadmap.md` (the clock display as built, waiting for the flashed clock)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write `docs/clock-display.md`** for the user, short and plain: what it is; flashing (point at decision 0013's Flashing section, never repeat or name his hosts); Fully Kiosk PLUS - install over adb (`adb connect <clock>:5555`, `adb install`, `pm grant de.ozerov.fully android.permission.SYSTEM_ALERT_WINDOW` only if needed), settings to turn on (Enable JavaScript Interface, URL whitelist to the brain's display address only, Keep Screen On, start URL `http://<brain>:<port>/display#key=<key>`); setting `display.port` on the settings page and restarting Nova; opening that port on the brain's firewall if it has one; adding the clock in the Clock displays section; what to check by hand on the clock (each page, swipe, tap, hold, the volume buttons, brightness, offline mark).
- [ ] **Step 2: Check** every command and setting name against the code and decision 0013; no host names, addresses or keys.
- [ ] **Step 3: Commit** "Write down how to set up the clock display".

### Final: whole-branch review

- [ ] One Opus reviewer reads `git diff 0d5830e..HEAD` against decision 0013 and this plan's Review Focus, runs nothing, and reports findings; fixes go in as their own commits; then one full suite run (London) and one with `TZ_OVERRIDE=UTC`, and ruff.
