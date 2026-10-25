# The calendar straight from Google - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova reads the user's calendars from Google itself, rather than through Home Assistant, adds events to them by voice, and takes each calendar but the user's own as an activity - "when's gym tomorrow?" works for a calendar called Gym as soon as it's made.

**Architecture:** A new `voice_assistant/google_calendar.py` holds Nova's own Google sign-in (`GoogleSignIn`, plain HTTPS with `requests`, a command on the brain to sign in once) and `GoogleCalendar`, which lists the calendars, reads and adds events, and says what's on. The calendar's words - `describe_agenda` and the rest - move there unchanged from `home_assistant.py`, which keeps nothing of the calendar. The choices decision 15 adds are kept in the data folder through plan 2's `ChoiceStore`, and the two settings that leave `config.yaml` go through plan 2's `REMOVED`.

**Tech stack:** Python 3.13 on the brain, Google's OAuth 2.0 (an authorisation code with PKCE) and Calendar API v3 over `requests`, pytest. No new dependency, and no Google library.

**Spec:** `docs/decisions/0008-home-assistant-first.md` - the user's answer 2, and decisions 10, 11 and 15. Read it before starting any task. It's built after `2026-10-24-assist-and-safety.md` and `2026-10-24-lists-devices-and-weather.md`, whose `ChoiceStore` and `config.REMOVED` it uses.

## Global constraints

- **Straight from Google** (the user's answer 2): the calendar is read from, and added to, Google itself, not through Home Assistant. It's the one exception to rule 3 here, and it's the user's.
- **Plain HTTPS** with `requests`. No Google library, and no new dependency.
- **The scopes are exactly these three**, the least that list the calendars, read every calendar's events, and add to the user's own:
  - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`;
  - `https://www.googleapis.com/auth/calendar.events.readonly`;
  - `https://www.googleapis.com/auth/calendar.events.owned`.

  Google has no scope that adds an event without being able to change one, so Nova's code is what keeps it to adding: it only ever lists calendars, reads events and inserts them (Task 4's test).
- **The sign-in is a secret on the brain only.** A Google Cloud OAuth client of the user's own, a "Desktop app" set to "In production" - in "Testing", Google ends the sign-in after a week. Its id and secret are `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `~/.config/nova/.env`, or on the page's Keys. The token is kept in `paths.DATA_DIR/google_token.json`, readable by Nova's user only, and never on a satellite.
- **Making or storing credentials is the user's, with the controller** - never an implementer's. Everything here is built and tested against fakes. The controller's steps come after Task 4.
- **Choices are kept in the data folder**, in `calendars.json` through `ChoiceStore`, with their defaults (decision 15): `{"activities": {}, "new_events": None}`. New events go to Google's primary calendar until another is chosen; every calendar but the primary is an activity until switched. 0009's page adds the controls later.
- **Adding:** an event lasts an hour unless an end is said, and is all day if no time is. Nova says back what it added, with the day and the time - "Added Dentist, Thursday the 29th at 3 PM." Nothing is changed or deleted by voice.
- **Offline**, or with Google not answering, there's no calendar, and Nova says "I can't reach Google's calendar right now." No copy is kept.
- **Removed settings warn**, through plan 2's `config.REMOVED`: `home_assistant.calendars` in Task 2 and `home_assistant.activities` in Task 3, each in the task that removes it. The brain's own `~/.config/nova/config.yaml` may still set them.
- **The repo is public.** The primary calendar's id is the user's email address, so it's never in code, tests, docs, commits or logs. Tests use made-up ids such as `calendar-work`. No client id, secret or token goes anywhere in the repo.
- **House style:** `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, LF, British English, spaced " - " dashes.
- **Tests:** on the Pi with `bash .superpowers/tools/pi_test.sh [pytest args]`, and in UTC with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes root@home-assistant 'cd /tmp/nova-check/work && /tmp/nova-check-pytest/bin/ruff check .'`.
- **Commits:** one per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Safety:** never push or deploy; touch no machine except through those two commands.

## Review focus

1. **The clocks going back** on Sunday 25 October 2026: an event on either side is said at its own local time. (Task 2, `test_an_event_after_the_clocks_go_back_is_said_at_its_own_time`.)
2. **Google giving its answer in pages** - calendars or events with a `nextPageToken` - loses none of them. (Task 2, `test_every_page_of_events_is_read`.)
3. **A sign-in Google has ended** (revoked, or a client left in Testing) says it needs doing again, never that Google can't be reached. (Task 1, `test_a_revoked_sign_in_says_it_needs_doing_again`.)
4. **An event early next month** says which month, so a misheard date is caught. (Task 4, `test_an_event_next_month_says_which_month`.)
5. **A calendar whose name is two words** - "Uni timetable" - is still heard by its first word, as long as no other calendar's name starts with it. (Task 3, `test_a_calendar_of_two_words_is_heard_by_its_first`.)

## Live checks before Task 1

None in Home Assistant. Google's answers can only be seen once Nova's signed in, so they're checked in the controller's steps after Task 4. Until then the fakes follow Google's Calendar API reference: a `calendarList` entry's `id`, `summary`, `summaryOverride`, `primary` and `accessRole`; an event's `status`, `summary`, `start` and `end` (each with `dateTime` or `date`) and `recurringEventId`; and `nextPageToken` for both.

---

### Task 1: Signing in to Google

**Files:**
- Create: `voice_assistant/google_calendar.py`, with a module docstring giving the user's answer 2 and decision 10 in brief.
- Modify: `voice_assistant/paths.py` (`GOOGLE_TOKEN_FILE`); `voice_assistant/settings_page.py` (`KEYS`); `README.md` (a "Google Calendar" part, replacing step 6 of the Home Assistant part).
- Test: `tests/test_google_calendar.py` (new), `tests/test_settings_page.py`.

**Interfaces:**
- Produces:
  - `SCOPES` (the three above), `AUTHORISE = "https://accounts.google.com/o/oauth2/v2/auth"`, `TOKEN = "https://oauth2.googleapis.com/token"` and `REDIRECT = "http://127.0.0.1:8085/"`. Nothing listens there, so the page doesn't load and the user pastes its address back, as with Spotify.
  - `class SignInNeeded(Exception)`.
  - `GoogleSignIn(client_id: str, client_secret: str, path: str = paths.GOOGLE_TOKEN_FILE, session: Optional[requests.Session] = None)`:
    - `start() -> str` - the address to open: `AUTHORISE` with `client_id`, `redirect_uri=REDIRECT`, `response_type=code`, `scope` (the three, space-separated), `access_type=offline`, `prompt=consent`, a `state` from `secrets.token_urlsafe`, and a `code_challenge` (the S256 of a `secrets.token_urlsafe(64)` verifier) with `code_challenge_method=S256`. The state and verifier are kept in memory for `finish`;
    - `finish(pasted: str)` - reads `code` and `state` from the address pasted. Another sign-in's state, or none started, raises `ValueError("That address isn't from this sign-in.")`, and no code raises `ValueError("That address hasn't got Google's code in it.")`, with nothing asked or saved. Otherwise it swaps the code, with the verifier, at `TOKEN`, and saves `{"refresh_token", "scope"}` with `storage.save_json`, which keeps it readable by Nova's user only. It raises `requests.RequestException` if Google can't be reached;
    - `signed_in() -> bool` - whether there's a saved refresh token;
    - `access_token() -> str` - kept in memory until a minute before it runs out, then refreshed at `TOKEN`. It raises `SignInNeeded` with no saved sign-in, or when Google answers `invalid_grant`, and `requests.RequestException` when Google can't be reached.
  - `paths.GOOGLE_TOKEN_FILE = os.path.join(DATA_DIR, "google_token.json")`.
  - `main(argv: Optional[List[str]] = None)` - the sign-in, run once on the brain as Nova's user: `venv/bin/python -m voice_assistant.google_calendar`, the way Spotify's is. It loads the keys with `paths.load_secrets()` and needs `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. It prints the address, then "The page won't load - that's expected. Copy its address from the address bar and paste it here:", reads it, finishes, and prints "Signed in to Google.". Already signed in, it says so, unless given `--again`. It never prints a token or a calendar's id.
  - `KEYS` gains `("GOOGLE_CLIENT_ID", "Google client ID")` and `("GOOGLE_CLIENT_SECRET", "Google client secret")`, hidden as they're typed, like Spotify's.

- [ ] **Step 1: Write the failing tests** with a `FakeGoogle` session answering by address and noting what was asked:
  - `test_the_sign_in_asks_for_only_what_nova_uses` - the address's `scope` is exactly the three, with `access_type=offline`, `code_challenge_method=S256` and a `state`;
  - `test_a_sign_in_finished_from_another_address_is_refused` - another state, or none started: the error, nothing asked, nothing saved;
  - `test_finishing_saves_the_sign_in_for_novas_user_only` - the code and verifier are sent; the file's mode is `0o600`, and it has the refresh token;
  - `test_an_access_token_is_kept_until_it_nearly_runs_out` - one refresh for two calls, then another once it's within a minute of running out;
  - `test_a_revoked_sign_in_says_it_needs_doing_again` - `invalid_grant` raises `SignInNeeded`; a connection error raises `requests.RequestException`;
  - `test_without_a_sign_in_it_needs_doing`;
  - `test_the_sign_in_command_never_prints_a_secret` - with `input` and the session faked, and capsys: "Signed in to Google." is printed, the token isn't;
  - `test_the_page_takes_googles_keys` (test_settings_page.py).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py`. Expected: they fail, with `ModuleNotFoundError: No module named 'voice_assistant.google_calendar'`.
- [ ] **Step 3: Implement** as the Interfaces say, then the README: making the Desktop-app client, In production, with the Calendar API switched on; its keys on the page; and the command, run as Nova's user (`runuser -u nova -- venv/bin/python -m voice_assistant.google_calendar`).
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Sign Nova in to Google itself, with a command on the brain".

### Task 2: Reading Google's calendars

**Files:**
- Modify: `voice_assistant/google_calendar.py` (`GoogleCalendar`, and the calendar's words moved in); `voice_assistant/home_assistant.py` (everything of the calendar goes, and its docstring says so); `voice_assistant/actions.py` (`ActionExecutor(calendar=...)`, `get_calendar`, `get_activity`, `UNAVAILABLE["calendar"]`); `voice_assistant/brain.py` (`self.calendar`); `voice_assistant/session.py`; `voice_assistant/config.py` (`REMOVED`); `config.yaml` (`home_assistant.calendars` goes); `README.md`; `docs/troubleshooting.md` ("The lights or the calendar don't work" splits, with a calendar part for Google).
- Test: `tests/test_google_calendar.py`; `tests/test_home_assistant.py` (its calendar tests move to the new file); `tests/test_actions.py`, `tests/test_config.py`, `tests/test_brain.py`.

**Interfaces:**
- Consumes: `GoogleSignIn` and `SignInNeeded` from Task 1, and plan 2's `config.REMOVED`.
- Produces:
  - Moved from `home_assistant.py` unchanged, with their constants: `describe_agenda`, `describe_activity`, `describe_next`, `describe_found`, `spoken_day` and their helpers (`_called`, `_as_activities`, `_by_day`, `_stretches`, `_span`, `_read_event`, `_sort_key`, `_on`, `_nothing_on`, `_day_by_day`, `_overview`, `_event_said`).
  - `CANT_REACH_GOOGLE = "I can't reach Google's calendar right now."` and `NEEDS_SIGN_IN = "I need signing in to Google again. That's done with a command on the brain."`.
  - `GoogleCalendar(sign_in: GoogleSignIn, activities: Optional[Dict[str, List[str]]] = None, session: Optional[requests.Session] = None)`. `activities` is `home_assistant.activities`, as `HomeAssistant` took it until now; Task 3 replaces it.
    - `GoogleCalendar.from_env(config) -> Optional[GoogleCalendar]` - None without `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, logged once.
    - `calendars(max_age: float = 300) -> List[Dict]` - `GET https://www.googleapis.com/calendar/v3/users/me/calendarList`, every page, each as `{"id", "name", "primary", "writable"}`. `name` is `summaryOverride`, else `summary`; `primary` is Google's own; `writable` is `accessRole == "owner"`, as `calendar.events.owned` adds to the user's own calendars only. The list is kept for `max_age` seconds, and `known() -> List[Dict]` gives the last one without asking.
    - `events(first: date, days: int, calendar_ids: Optional[List[str]] = None) -> List[Dict]` - for every calendar in the list, or those given: `GET .../calendars/{id}/events`, the id quoted, with `timeMin` and `timeMax` (the days' local start and end, in RFC 3339), `singleEvents=true` (so a repeating event comes once on each of its days, as Home Assistant gives them today), `orderBy=startTime` and `maxResults=250`, every page. A cancelled event is left out. Each is read by `_read_event` - Google's `start` and `end` are the shape Home Assistant passed on - with `"calendar"`, its calendar's name, added. Earliest first.
    - `agenda(first, days, today=None)`, `find(wanted, first, today=None)` and `activity(name, first, days, today=None, count=None, now=None)` - moved from `HomeAssistant`, saying the same as now. `SignInNeeded` says `NEEDS_SIGN_IN`. Google unreachable, a 5xx, or an answer of the wrong shape says `CANT_REACH_GOOGLE`, and with no network at all it says so without trying.
    - Every request sends `Authorization: Bearer <access_token()>`. Nothing logs a calendar's id.
  - `Brain.calendar: Optional[GoogleCalendar]`, given to each session's `ActionExecutor(calendar=...)`. `get_calendar` and `get_activity` are `needs="calendar"`, and `UNAVAILABLE["calendar"] = "The calendar isn't set up yet."`.
  - `config.REMOVED` gains `("home_assistant", "calendars"): "every calendar in Google's own list is read now"`.

- [ ] **Step 1: Write the failing tests**, with Google's answers as its API reference gives them, ids made up:
  - `test_every_calendar_in_googles_list_is_read` - three calendars, over two pages of the list; each events request has `singleEvents=true` and the day's window; the events come back from all three, earliest first;
  - `test_every_page_of_events_is_read`;
  - `test_a_repeating_event_is_on_each_of_its_days` - two instances of a weekly lecture, with `recurringEventId` and `originalStartTime`;
  - `test_an_all_day_event_is_said_on_its_day` - `{"date": "2026-10-30"}`, ending the next day;
  - `test_a_cancelled_event_is_left_out`;
  - `test_an_event_after_the_clocks_go_back_is_said_at_its_own_time` - `2026-10-24T09:00:00+01:00` and `2026-10-26T09:00:00Z` are both said "9 AM", on UK time;
  - `test_without_the_internet_it_says_it_cant_reach_google` - and with no network, nothing is asked;
  - `test_a_sign_in_that_needs_doing_again_says_so` - `NEEDS_SIGN_IN`;
  - the calendar's existing tests from `tests/test_home_assistant.py`, from `test_the_calendar_is_read_for_the_days_asked_about` to `test_nothing_coming_up_says_so`, moved over against a `FakeGoogle`, with their sentences unchanged. `test_only_the_calendars_named_are_read` goes;
  - `test_the_calendar_is_only_offered_once_google_is_set_up` (test_actions.py);
  - `test_home_assistant_calendars_is_let_through_with_where_its_set_now` (test_config.py).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py tests/test_actions.py tests/test_config.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say. Move the calendar's words by cutting and pasting them unchanged, so the review's diff shows only the move and the new client.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Read the calendars from Google itself rather than through Home Assistant".

### Task 3: Activities from the calendars

**Files:**
- Modify: `voice_assistant/google_calendar.py` (activities, and `GoogleCalendar`'s arguments); `voice_assistant/intents.py` (the activity rules; `ACTIVITIES` goes); `voice_assistant/actions.py` (`get_activity`'s description); `voice_assistant/conversation.py` (`_ask_claude`); `voice_assistant/session.py` (`LocalIntents`); `voice_assistant/brain.py` (the choices, and reading the calendars at `run()`); `voice_assistant/config.py` (`REMOVED`); `config.yaml` (`home_assistant.activities` goes); `README.md`.
- Test: `tests/test_google_calendar.py`, `tests/test_intents.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_config.py`.

**Interfaces:**
- Consumes: Task 2's `GoogleCalendar`, and plan 2's `ChoiceStore`.
- Produces:
  - `GoogleCalendar(sign_in: GoogleSignIn, choices: ChoiceStore, session: Optional[requests.Session] = None)` - `activities` goes. `choices` is `ChoiceStore(<data folder>/calendars.json, {"activities": {}, "new_events": None})`.
  - `SAME_AS = {"work": ("working", "shift", "shifts"), "uni": ("university", "lecture", "lectures", "class", "classes")}` - the few English words for the same things, kept in code. They're words, not devices.
  - `GoogleCalendar.activity_calendars() -> Dict[str, Dict]` - from `known()`, with no request: each activity's word (its calendar's name, in lower case) to its calendar. A calendar is an activity if `choices.get("activities")` says so for its id, else if it isn't the primary.
  - `GoogleCalendar.activity_words() -> Dict[str, str]` - every word heard locally, to its activity: the name, its plural (an "s" added, unless it ends in one), and `SAME_AS`'s words for it. A name of two or more words is heard by its first word too, as long as no other activity's name starts with that word.
  - `activity(name, ...)` finds the calendar through `activity_words`, and reads that calendar alone. Every event on it counts, whatever it's called. One it hasn't got: "I haven't got a calendar called gym." - Nova no longer tells the user to edit a file.
  - `agenda` gives each activity calendar's events as that activity, with `_as_activities` given each event's `"calendar"` and the activity calendars, rather than words in the events' names. Other events stay as they are.
  - `LocalIntents(clock=None, ntfy=None, activities: Optional[Callable[[], Dict[str, str]]] = None)`. The activity rules' ACTIVITY pattern is built from `activities()` at each match, so a calendar added later is heard from then on. Without it, they don't match, and Claude has them.
  - `get_activity`'s description ends "The user's activities: Uni and Work.", named as Google has them, and its `activity` parameter no longer names work and uni itself. `Conversation._ask_claude` sets `self.llm.tools = self.actions.tool_schemas()` before asking, so a new calendar is named from the next request; the API's cache only changes when the tools do.
  - `Brain.run()` reads the calendars once, in the background, so their names are known before anything's asked. A failure is only logged.
  - `config.REMOVED` gains `("home_assistant", "activities"): "activities are your calendars in Google now"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_every_event_on_the_work_calendar_is_work` - events called "Late" and "Cover" give "Tomorrow, you've got work from ...";
  - `test_a_calendar_added_later_is_an_activity_by_itself` - "Gym" turns up in the list: "When's gym tomorrow?" is handled locally, and reads only that calendar;
  - `test_the_primary_calendar_isnt_an_activity` - until it's switched on in `calendars.json`; and Work, switched off there, isn't one;
  - `test_an_activity_nova_hasnt_got_says_so` - "I haven't got a calendar called gym.";
  - `test_the_same_words_still_mean_work_and_uni` - "Am I working on Saturday?", "When's my next lecture?" and "What are my next three shifts?";
  - `test_a_calendar_of_two_words_is_heard_by_its_first` - "Uni timetable" is heard as "uni", but not once "Uni clubs" is there too;
  - `test_claude_is_told_the_activities` - the description ends "The user's activities: Uni and Work.", and a new calendar is named at the next request (test_conversation.py);
  - `test_whats_on_says_a_day_of_uni_as_uni` - the Uni calendar's sessions are "uni from 9 AM until 1 PM";
  - `test_home_assistant_activities_is_let_through_with_where_its_set_now` (test_config.py).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py tests/test_intents.py tests/test_actions.py tests/test_conversation.py tests/test_config.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Take each calendar but the user's own as an activity, by its name".

### Task 4: Adding to the calendar

**Files:**
- Modify: `voice_assistant/google_calendar.py` (`add`); `voice_assistant/actions.py` (an `add_to_calendar` tool); `README.md`.
- Test: `tests/test_google_calendar.py`, `tests/test_actions.py`.

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces:
  - `GoogleCalendar.add(summary: str, start: str, end: Optional[str] = None, all_day: Optional[bool] = None, calendar: Optional[str] = None, today: Optional[date] = None) -> Dict`:
    - `start` and `end` are local, "YYYY-MM-DD HH:MM", or "YYYY-MM-DD" for a day. A day alone, or `all_day`, is all day, and an end day is included. A time with no end lasts `DEFAULT_LENGTH`, an hour. An end before the start says "That ends before it starts.", and a start that can't be read "I didn't catch when that is.";
    - the calendar is the one named, matched by its name or its `activity_words` ("work" is Work), among those Google lets Nova add to. One it can't add to: "I can't add to Uni - Google only lets me add to your own calendars."; one it hasn't got: "I haven't got a calendar called that.". With none named, it's `choices.get("new_events")` if that's still one Nova can add to, else the primary;
    - it inserts with `POST .../calendars/{id}/events`, `{"summary", "start": {"dateTime": <local RFC 3339>} or {"date"}, "end": ...}`;
    - once Google has taken it, it says what it added: "Added Dentist, Thursday the 29th at 3 PM." - the weekday and the day of the month always, with " of November" when it isn't this month, and ", all day" for a day. A calendar other than the one new events go to is named: "Added Shift to Work, Saturday the 31st at 9 AM.";
    - Google refusing says "Google wouldn't add that.", and `NEEDS_SIGN_IN` and `CANT_REACH_GOOGLE` are said as for reading.
  - The tool `add_to_calendar(summary, start, end, all_day, calendar)`, `needs="calendar"`, `required=["summary", "start"]`, described: "Add an event to the user's Google calendar, for 'put the dentist on Thursday at 3'. start and end are local 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for all day; leave end out for an hour. Give calendar only when the user names one, e.g. 'work'. What was added is said for you, so just reply 'Okay.'"
  - `google_calendar.py` makes no other kind of request: the token, the calendar list, reading events, and inserting one. Nothing changes or deletes an event.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_new_event_goes_to_the_primary_calendar_and_lasts_an_hour` - asked on Saturday the 24th of October 2026, 3 PM to 4 PM on the 29th, and "Added Dentist, Thursday the 29th at 3 PM.";
  - `test_with_no_time_its_all_day` - `{"date": "2026-10-30"}` to `{"date": "2026-10-31"}`;
  - `test_a_calendar_named_is_used` - "work" goes to Work, and is named in what's said;
  - `test_the_chosen_calendar_is_used_until_its_gone`;
  - `test_a_calendar_nova_cant_add_to_says_so`;
  - `test_an_event_next_month_says_which_month` - from Friday the 30th of October, the 2nd of November is "Monday the 2nd of November";
  - `test_nothing_is_said_added_unless_google_took_it` - a 403 and a 500;
  - `test_nova_never_changes_or_deletes_an_event` - every request in this file's tests is a GET, or a POST to `TOKEN` or to an events list; and the module's source has no `PATCH`, `PUT` or `DELETE`, checked as `test_every_service_call_goes_through_the_one_allowlist` checks `home_assistant.py`;
  - `test_add_to_calendar_is_only_offered_once_google_is_set_up` (test_actions.py).
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_google_calendar.py tests/test_actions.py`. Expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say, then the README: adding by voice, and that it goes to the primary calendar unless another is named.
- [ ] **Step 4: Run** the changed files, then the full suite in both time zones, `tests/test_end_to_end.py` five times, and ruff. Expected: all pass, and ruff is clean.
- [ ] **Step 5: Commit** - "Add events to Google's calendar by voice, and say back what was added".

---

## The controller's steps, after Task 4

These make and use a credential, so each needs the user's OK, and none is an implementer's.

1. **The OAuth client.** With the user, in Google Cloud: make an OAuth client of type "Desktop app", switch on the Google Calendar API, and set the consent screen to "In production", with the three scopes. Home Assistant's Google integration's client is of type "TV and Limited Input", which can't take a pasted address, so it's a new one unless the controller rules otherwise.
2. **Its keys.** The user puts the client's id and secret on the brain, as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` - on the page's Keys, or in `~/.config/nova/.env` - never in the repo.
3. **Signing in.** Once the user has OK'd deploying it, on the brain: `runuser -u nova -- venv/bin/python -m voice_assistant.google_calendar`. The user opens the address, approves Nova, and pastes back the address the browser ends up at.
4. **Read-only checks.** "What have I got on Thursday?" against Google's own view of Thursday; that "Nathaniel" is the primary and Uni and Work are activities; that "Nathaniel" and Work can be added to; and that the brain's log warns about any `home_assistant.calendars` or `home_assistant.activities` still in its own settings. No calendar id is written down.
