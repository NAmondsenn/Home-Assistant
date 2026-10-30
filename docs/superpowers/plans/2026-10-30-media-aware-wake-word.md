# A stronger wake word while something plays in the room - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a media player in a satellite's room is playing, that satellite needs a surer wake word (0.75 by default), and Claude may answer "not for me" to speech from the film or music, which Nova then ignores silently.

**Architecture:** a `MediaWatcher` on the brain asks Home Assistant every 3 s which media players are playing (with area, device and source) and tells the brain when that list changes; each satellite's session keeps those in its own room that aren't Nova's own. The brain sends each satellite a `wake_confidence` in the state message; the satellite listens by it. Claude's per-request situation names what's playing, and a `not_for_me` tool ends the turn silently.

**Tech Stack:** Python 3.13, requests, pytest. No new dependency.

**Spec:** `docs/decisions/0010-media-aware-wake-word.md`, from the user's brief of 30 Oct ("any media player Home Assistant puts in the satellite's room that's PLAYING (not paused) means a stronger wake word (about 0.75, set on the settings page under Listening) and Claude being told so it can answer 'not for me' to TV speech, which Nova then ignores silently. Local commands still work. Nova's own music doesn't count.").

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address or token.
- What counts, for each satellite: a `media_player` whose state is exactly `playing`, whose area (its own, else its device's) is that satellite's room (its session's `HomeAssistant.area`), not on any connected satellite's own HA device (each session's `HomeAssistant.device_id`), and not a player whose `source` attribute matches (trimmed, case-insensitive) any connected satellite's Spotify speaker name (`satellite.speaker_name`, else the brain's `spotify.device_name`). A satellite whose room is unknown -> nothing counts for it. Each satellite gets its own wake confidence and its own requests' situation line.
- The template asks for every playing media player with its area name, device id, source and friendly name, as JSON, and Python filters - no value is ever written into the template's text.
- Settings, both under `thresholds` (so the page shows them under Listening), both used straight away: `wake_word_confidence` (0.5, "How sure Nova must be it heard its wake word") and `media_wake_word_confidence` (0.75, "How sure Nova must be it heard its wake word while something's playing in the room"). The page accepts 0.3 to 0.95 for each. While something plays, the satellite gets `max(wake_word_confidence, media_wake_word_confidence)`.
- Satellite: `wake_confidence` in a state message is used only if it's an int or float (not bool), finite, from 0.05 to 0.99; anything else is ignored and the current sensitivity kept. A message without it changes nothing.
- HA unreadable -> nothing counts; logged once as a warning when it starts failing, once at INFO when it answers again.
- `not_for_me`: honoured only if something was playing when the request was built; then Nova says nothing, plays no sound, doesn't listen again, doesn't add the turn to the conversation's thread, and runs no other tool from that same response. Called while nothing plays: "Sorry, I didn't catch that." Logged at INFO "Taken as speech from what's playing, so ignored", the words only through the existing transcript logging rule.
- Local commands and Assist are unchanged: only requests that reach Claude can be ignored.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. Tests on the Pi via `.superpowers/tools/pi_test.sh`, in London and UTC; ruff.

## Review Focus

1. **TV speech can't make Nova act.** A `not_for_me` response runs nothing, and nothing from it reaches the thread.
2. **Nothing makes Nova easier to wake or impossible to wake.** No path sends a sensitivity below the normal setting while something plays; a bad value never reaches openWakeWord.
3. **HA slow or down** never delays a request or stops the watcher thread.
4. **Nova's own music** never counts.

---

### Task 1: The media watcher

**Files:** create `voice_assistant/media_watch.py`; modify `voice_assistant/home_assistant.py` (one method), `voice_assistant/brain.py` (make it, start it with the other watchers, stop it), `voice_assistant/session.py` (`playing_here`). Tests: `tests/test_media_watch.py`, `tests/test_home_assistant.py`, `tests/test_brain.py`, `tests/test_session.py`.

- `HomeAssistant.playing_media() -> List[Dict]`: one `_template` call (timeout 2 s) rendering, as JSON, every `states.media_player` with state `playing`: `{"entity_id", "name" (friendly_name or entity id), "area" (area_name(entity_id) or null), "device" (device_id(entity_id) or null), "source" (attribute or null)}`. Raises `requests.RequestException` or `ValueError` as `_template`/`json` do.
- `class MediaWatcher(home, on_change: Callable[[], None], every: float = 3.0)`:
  - `playing() -> List[Dict]`: every playing player as last read (empty after a failed read).
  - `playing_in(area, devices, speakers) -> List[str]`: the names of what counts for one satellite (the Global Constraints' rules), sorted. `area` None -> [].
  - `check()`: one read; calls `on_change()` only when the list (entity ids with their area, device and source) changed. Failure: an empty list (so a change to empty calls `on_change()`), warning once, INFO once when back.
  - `start()` / `stop()`: a daemon thread, as `BatteryWatcher`'s, that survives any exception from `check` or `on_change` (logged with `logger.exception`, at most once per kind of failure in a row).
- Brain: `self.media` made only when Home Assistant is set up (else `None`); `on_change` calls `_send_states()`. A session's view - `Session.playing_here() -> List[str]` - passes its own `home.area`, every connected session's `home.device_id`, and every connected satellite's Spotify speaker (`satellite.speaker_name`, else the brain's `spotify.device_name`). [] without a watcher or a home.
- Tests: in the room and playing counts; paused/idle doesn't; another room doesn't; no area doesn't; the satellite's area unknown -> nothing; on any connected satellite's device doesn't; two satellites in two rooms each see only their own; Spotify source "Voice-Assistant" vs speaker "voice-assistant " doesn't; source unrelated counts; a change calls on_change once, no call when unchanged (a title change alone is no change); HA raising -> nothing counts, one warning across three failures, INFO on recovery; `on_change` raising doesn't kill the thread; template text has no Nova value written into it.
- Commit: "Watch for anything playing in each satellite's room".

### Task 2: The wake confidence each satellite is sent

**Files:** modify `config.yaml` (thresholds: the two settings, with comments), `voice_assistant/config.py` (limits 0.3-0.95 for both), `voice_assistant/settings_page.py` (`EDITABLE`: both, "now", labels as in the Global Constraints), `voice_assistant/brain.py` (`apply_settings`: a change to either sends states again), `voice_assistant/session.py` (`send_state`), `voice_assistant/remote.py` (`set_state(..., wake_confidence: Optional[float] = None)`), `voice_assistant/protocol.py` (docstring: `"wake_confidence"?: 0-1`). Tests: `tests/test_session.py`, `tests/test_remote.py`, `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_brain.py`.

- `Session.send_state` sends `wake_confidence`: `wake_word_confidence`, or the higher of the two while anything counts for this session's satellite (its own room, per the Global Constraints); read from config at each send, as `recording_timeout` is.
- Tests: nothing playing -> 0.5; playing -> 0.75; media setting 0.4 with normal 0.6 while playing -> 0.6; no media watcher (no HA) -> normal; saving either setting on the page sends states to every connected satellite; the page refuses 0.2 and 0.97; an older satellite's `set_state` path is unaffected (field is just extra).
- Commit: "Send each satellite how sure to be of its wake word".

### Task 3: The satellite listens by what it's sent

**Files:** modify `satellite.py` (`_take_state`). Tests: `tests/test_satellite.py`.

- Applies the Global Constraints' check, then `self.wake.sensitivity = value`. Logged at INFO when it changes ("Listening for the wake word at 0.75 now").
- Tests: 0.75 applied; absent leaves it; True, "0.75", NaN, inf, 0, -0.1, 1.5, None each ignored; 1 accepted.
- Commit: "Listen for the wake word as surely as the brain asks".

### Task 4: Claude's "not for me"

**Files:** modify `voice_assistant/llm.py` (situation line, system prompt paragraph, the tool and its handling), `voice_assistant/conversation.py` (what's playing in; the silent reply), `voice_assistant/session.py` or `voice_assistant/brain.py` only as wiring needs, `voice_assistant/actions.py` only if tools must be declared there. Tests: `tests/test_llm.py`, `tests/test_conversation.py`, `tests/test_session.py`.

- The situation line, only while something plays here (`Session.playing_here()`, from Task 1): "Playing in this room: Firefox, Windows PC." The system prompt (cached, unchanged per request) says: when the situation says something's playing, what was heard may be speech from it - a film, a programme, a song, a presenter - picked up as if said to Nova; if it plainly is that and not a request to Nova, call `not_for_me` and nothing else; when in doubt, answer as normal.
- The tool takes no input. When a response has it: no other tool use in that response runs, the loop ends, and the result says "not for me".
- Conversation: honoured only if what was playing when the request was built was non-empty; the reply says nothing, no sounds, `listen_again` False, nothing remembered in the thread, `timeline.note(outcome="not_for_me")`, the INFO log line. Music paused or lowered for the turn comes back as after any reply. Not honoured: "Sorry, I didn't catch that."
- Tests: the situation line present only while playing; the prompt paragraph present always (cache); a response with `not_for_me` plus `set_lights` runs neither; silent reply fields; thread unchanged; not playing -> the apology; follow-up requests get the same check; local intents ("pause", "stop") never reach Claude while playing.
- Commit: "Let Claude say what it heard was the film, not for Nova".
