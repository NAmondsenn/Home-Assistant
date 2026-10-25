# The Pi as a Satellite - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova's Pi becomes a light satellite (microphone, wake word, speaker, stopping, a small fallback), and PVE2's `nova` container becomes the brain that does everything else, for any number of satellites.

**Architecture:**
- **The connection.** The existing connection in `protocol.py` grows into two-way messages, with both sides proving they know the satellite's own password.
- **The Pi.** It runs `satellite.py`, which plays what the brain sends and streams the microphone after the wake word.
- **The brain.** `backend.py` runs a `Brain` of shared services, with a `Session` per connected satellite. The session is `main.py`'s `SmartAssistant` moved across, driving the satellite through `RemoteSpeaker` and an `AudioFeed` in place of the local speaker and microphone.

**Tech Stack:**
- Python 3.13, numpy.
- Sockets over the tailnet, with HMAC-SHA256.
- On the satellite: sounddevice, openWakeWord and speexdsp.
- On the brain: sherpa-onnx (Nemotron), Piper, anthropic, spotipy, paho-mqtt and ffmpeg.

**Spec:** `docs/decisions/0006-the-pi-as-a-satellite.md` (agreed 22 October 2026). It replaces 0004's stage 2 and its standalone mode.

## Global Constraints

- **No new container.** The brain is container 107, `nova`, on PVE2.
- **Secrets.** Nothing secret is on the Pi but its own satellite password, `NOVA_BACKEND_TOKEN` in `~/.config/nova/.env`. Every other key lives on the brain.
- **The tailnet only.** Nothing new is reachable from outside it.
- **One codebase, and one writer for each thing.** The brain owns the timers and alarms, each satellite's volumes and its mute. The satellite keeps copies only to use while PVE2 can't be reached.
- **No standalone mode.** The Pi has no Moonshine, Whisper, Piper, Claude or everyday commands.
- **Uncompressed audio.**
  - Up: 16kHz, 16-bit, mono, as now.
  - Down: 16-bit little-endian at the rate and channels each stream names.
  - Each message carries at most `MAX_AUDIO` bytes (320,000).
- **Recording limit.** The satellite stops sending a recording after 10 seconds of audio, whatever the brain says.
- **Privacy.** Nothing the user says, and none of Nova's answers, is published to Home Assistant (0005's rule).
- **The Home Assistant device carries over.** A satellite's device keeps 0005's eleven controls and names. Its identifier still comes from `mqtt.name` plus the satellite's own hostname, so the Pi keeps its device, and with it the Bedroom.
- **No new dependencies** beyond what's already in `requirements.txt` and `requirements-backend.txt`.
- **House style.** Comments in full sentences, British spelling, spaced " - " dashes, *why* not *what*; Google docstrings; LF; one commit per task, titled in plain words, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Tests run on the Pi.**
  - Run `bash .superpowers/tools/pi_test.sh [args]` from the repo root, then again with `TZ_OVERRIDE=UTC`.
  - Ruff: `/tmp/nova-check-pytest/bin/ruff check .` in `/tmp/nova-check/work` on the Pi.
  - The Pi's venv still has every package the brain uses except sherpa-onnx, so tests fake `Nemotron`.

## Review Focus

1. **The connection drops mid-recording or mid-reply.**
   - The satellite stops sending and says the fallback phrase.
   - The brain abandons that turn without the session or the brain falling over.
   - The satellite reconnects by itself (Tasks 6 and 9: `test_a_dropped_connection_mid_recording_says_the_phrase` and `test_a_satellite_which_goes_mid_turn_leaves_the_brain_working`).
2. **The brain restarts with an alarm due.**
   - The satellite rings it from its copy.
   - On reconnecting, the brain is told it rang and was dismissed, so it isn't rung a second time (Tasks 3, 4 and 9: `test_what_was_dismissed_while_away_doesnt_ring_again`).
3. **The wrong password, or something posing as the brain.**
   - Refused both ways.
   - Logged once, not on every retry.
   - The satellite backs off to every 30 seconds (Tasks 1 and 5: `test_a_brain_which_cant_prove_the_password_is_refused`, `test_refusals_are_logged_once_and_backed_off`).
4. **Wake words close together.** A second wake while still listening, a wake during the listening sound, or a reply talked over during a follow-up all give one turn at a time, and Nova is never left stuck "listening" (Tasks 6 and 8: `test_a_wake_while_streaming_starts_nothing_new`, `test_talking_over_a_follow_up_starts_the_next_turn`).
5. **A satellite the brain doesn't know, or the same satellite connecting twice.**
   - An unknown satellite is refused with one clear line.
   - A second connection from the same name replaces the first, whose session ends cleanly (Task 9: `test_a_second_connection_replaces_the_first`).

---

### Task 1: Both sides prove the password, and the brain keeps one per satellite

**Files:**
- Modify: `voice_assistant/protocol.py` (the message list in the docstring; `proof`, `greet`, `admit`, `Refused`)
- Create: `voice_assistant/satellite_passwords.py`
- Modify: `backend.py` (`add-satellite NAME` and `remove-satellite NAME`)
- Test: `tests/test_protocol.py`, `tests/test_satellite_passwords.py`, `tests/test_backend.py`

**Interfaces:**
- Produces:
  - `protocol.proof(password: str, role: str, first: str, second: str) -> str`: the hex HMAC-SHA256 of `f"{role}|{first}|{second}"`.
  - `protocol.greet(stream, name: str, hostname: str, password: str, sample_rate: int, unclean: bool) -> None`: the satellite's side. Raises `protocol.Refused`.
  - `protocol.admit(stream, password_for: Callable[[str], Optional[str]]) -> Dict`: the brain's side. Returns the hello's `name`, `hostname`, `sample_rate` and `unclean`, or raises `Refused`.
  - `SatellitePasswords(path=os.path.join(paths.CONFIG_DIR, "satellites.json"))`, with:
    - `.get(name) -> Optional[str]`;
    - `.add(name, password: Optional[str] = None) -> str` - makes a 32-byte `secrets.token_urlsafe` unless one is given. It refuses a name already there, and a password shorter than `backend.MIN_TOKEN` (16);
    - `.remove(name) -> bool`;
    - `.names() -> List[str]`, in the order they were added. The first is the default satellite.
  - `backend.main(["add-satellite", NAME])` prints a new password once. `backend.main(["add-satellite", NAME, "--password-stdin"])` reads one from standard input instead, so an existing password can be moved without it appearing in the process list. `backend.main(["remove-satellite", NAME])` removes one.
- **The handshake, with these exact messages.**
  1. The satellite sends `hello {name, hostname, sample_rate, unclean, nonce}`. The nonce is 32 hex characters.
  2. If the name is unknown, the brain sends `refused {}` and closes. Otherwise it sends `challenge {nonce, proof}`, where the proof is `proof(pw, "brain", satellite_nonce, brain_nonce)`.
  3. The satellite checks that proof with `hmac.compare_digest`, then sends `proof {proof}`, where the proof is `proof(pw, "satellite", brain_nonce, satellite_nonce)`.
  4. The brain checks it, and answers `ready {}` or `refused {}`.
- **The message list for the docstring.** Every later task uses these exact names.
  - **Satellite to brain:** `ping`, `wake {talked_over}`, `audio` (payload), `audio_end {reason: "limit"|"cancelled"}`, `played {id, stopped, position}`, `progress {id, position}`, `away {rang: [ids], dismissed: [ids]}`.
  - **Brain to satellite:**
    - `pong`, `listen`, `stop_listening`;
    - `play_sound {id, name, volume, kind, start, length}`;
    - `play_stream {id, rate, channels, volume, kind, paused}`, `stream_audio {id}` (payload), `stream_end {id}`;
    - `set_playback {id, paused?, volume?}`;
    - `stop {kinds|null}`, `silence {kind}`, `allow {kind}`;
    - `state {volumes: {general, alarm, music}, muted}`;
    - `alarms {entries: [{id, due, kind, sound}]}`;
    - `phrase {rate}` (payload);
    - `restart`.

- [ ] **Step 1: Write the failing tests.** Run each pair of sides over `socket.socketpair()`, each on its own thread.
  - `test_the_handshake_admits_a_satellite_which_knows_its_password`: `admit` returns `{"name": "bedroom", "hostname": "home-assistant", "sample_rate": 48000, "unclean": False}`.
  - `test_a_wrong_password_is_refused`: both sides raise or return `Refused`, and the brain sends `refused`.
  - `test_a_brain_which_cant_prove_the_password_is_refused`: a brain with a different password makes `greet` raise `Refused`, and the satellite never sends `proof`.
  - `test_an_unknown_satellite_is_refused`.
  - `test_passwords_are_kept_in_the_order_added_and_privately`: `names() == ["bedroom", "kitchen"]`, the file is mode `0o600`, and `add("bedroom")` again raises `ValueError`.
  - `test_add_satellite_prints_the_password_once`: run `backend.main(["add-satellite", "bedroom"])`.
  - `test_add_satellite_can_take_an_existing_password_from_stdin`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** Write the passwords with `storage.save_json`. Compare proofs only with `hmac.compare_digest`. Leave `Connection._serve`'s speech-to-text alone - Task 9 replaces it.
- [ ] **Step 4: Run the tests, then the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Have the Pi and the brain each prove the satellite's password".

---

### Task 2: A recording made from any source of audio

**Files:**
- Create: `voice_assistant/recording.py`
- Modify: `voice_assistant/microphone.py` (`record_until_silence` becomes a one-line call into `recording`)
- Test: `tests/test_recording.py`. The recording tests in `tests/test_microphone.py` must pass unchanged.

**Interfaces:**
- Produces:
  - `recording.record_until_silence(source, detector, listen_from=None, keep_before=0.0, timeout=10.0, silence_duration=0.6, speech_timeout=3.0, min_speech=0.12, timeline=None, heard=None) -> np.ndarray`.
    - This is Microphone's algorithm, moved unchanged.
    - `source` needs `position`, `listen(start)` (a reader with `read(count)` and `position`, as `MicrophoneReader` has), and `time_at(position)`.
  - `recording.AudioFeed`: a source fed from elsewhere, with:
    - `.feed(samples: np.ndarray)` - int16 at 16kHz, noting when each piece arrived for `time_at`;
    - `.end()` - nothing more is coming. A reader waiting then gets what's left, and the recording stops;
    - `.position`, `.listen(start)`, `.time_at(position)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_fed_recording_stops_once_the_user_goes_quiet`: feed 0.5s of speech and then 1s of silence, with a fake detector. It returns about 0.5s plus `silence_duration`.
  - `test_a_feed_which_ends_part_way_gives_what_was_heard`.
  - `test_a_feed_with_no_speech_gives_nothing`: an empty array after `speech_timeout`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** Move the body; don't copy it.
- [ ] **Step 4: Run `tests/test_recording.py`, `tests/test_microphone.py`, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Record until the user stops from any source of audio, not just the microphone".

---

### Task 3: The clock knows which satellite each timer and alarm rings on

**Files:**
- Modify: `voice_assistant/clock.py`
- Test: `tests/test_clock.py`

**Interfaces:**
- Produces:
  - `set_timer(..., satellite: Optional[str] = None)` and `set_alarm(..., satellite: Optional[str] = None)` store `entry["satellite"]`.
  - `on_timer_finished(message, sound, boost, still_wanted=..., satellite=..., **playback)` and `on_unanswered(timer, kind, satellite=...)` are given the entry's satellite.
  - `Clock(..., on_change: Optional[Callable[[], None]] = None)` is called after anything is set, cancelled, removed, dismissed or rung.
  - `Clock.assign_unowned(satellite: str) -> int` gives every entry with no satellite to this one, and returns how many it gave.
  - `Clock.overview(satellite: Optional[str] = None)` shows only that satellite's entries when one is given.
  - `Clock.copy_for(satellite: str, within: float = 7 * 86400) -> List[Dict]`:
    - returns `{"id", "due", "kind", "sound"}`;
    - `due` is epoch seconds and `kind` is `"timer"` or `"alarm"`;
    - covers every occurrence in that window, with repeats expanded;
    - `id` is stable for each occurrence: `f"{entry id}@{int(due)}"`.
  - `Clock.apply_away(satellite: str, rang: List[str], dismissed: List[str])`. What was dismissed while away is dealt with as if dismissed here: a timer goes, and a repeating alarm moves on. What only rang is left to ring here.

- [ ] **Step 1: Write the failing tests.**
  - `test_a_timer_rings_on_the_satellite_it_was_set_from`: the callback gets `satellite="bedroom"`.
  - `test_the_copy_holds_each_occurrence_within_a_week`: a weekday alarm gives 5 entries.
  - `test_what_was_dismissed_while_away_doesnt_ring_again`.
  - `test_changes_are_told_to_on_change`.
  - `test_entries_from_before_satellites_are_given_to_the_default`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** Give entries a stable id if they lack one.
- [ ] **Step 4: Run `tests/test_clock.py`, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Ring each timer and alarm on the satellite it was set from".

---

### Task 4: What the satellite does while PVE2 can't be reached

**Files:**
- Create: `voice_assistant/fallback.py`
- Create: `sounds/cant_reach_server.wav`. Nova's voice saying "Sorry, I can't reach my server right now.", made with the Pi's Piper (`TextToSpeech.synthesise`) at 22,050Hz, mono, 16-bit.
- Test: `tests/test_fallback.py`

**Interfaces:**
- Produces:
  - `AlarmCopy(path=os.path.join(paths.DATA_DIR, "alarm_copy.json"))`, with:
    - `.replace(entries)`;
    - `.due(now) -> List[Dict]` - entries due by `now`, not rung yet, and no more than `GRACE = 600` seconds late;
    - `.rang(id)` and `.dismissed(id)`;
    - `.away_report() -> {"rang": [...], "dismissed": [...]}`;
    - `.clear_report()`.
  - `StateCopy(path=os.path.join(paths.DATA_DIR, "satellite_state.json"))`, with `.update(state: Dict)`, `.volume(category) -> float` (0-1) and `.muted -> bool`.
  - `FallbackRinger(speaker, sounds_folder, copy: AlarmCopy, state: StateCopy)`, with:
    - `.check(now)` - rings what's due: the entry's sound, looped, as kind `"announcement"`, at the alarm volume;
    - `.ringing -> bool`;
    - `.dismiss()`.
    - It rings until dismissed, or for `RING_LIMIT = 600` seconds.
  - `Phrase(path=os.path.join(paths.DATA_DIR, "fallback_phrase.wav"), default=<sounds>/cant_reach_server.wav)`, with:
    - `.replace(audio: bytes, rate: int)` - keeps the brain's;
    - `.play(speaker, volume) -> Optional[Playback]`.
    - It plays the brain's copy if it has one, and the default if not.

- [ ] **Step 1: Write the failing tests.**
  - `test_an_alarm_due_while_away_rings_until_dismissed`.
  - `test_an_alarm_long_missed_isnt_rung`: more than `GRACE` late.
  - `test_the_away_report_says_what_rang_and_what_was_dismissed`.
  - `test_the_copy_lasts_through_a_restart`.
  - `test_the_phrase_is_the_default_until_the_brain_sends_one`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** Use a fake speaker that records `play_file` calls.
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Ring alarms and say why when the satellite can't reach its brain".

---

### Task 5: The satellite's connection to the brain

**Files:**
- Create: `voice_assistant/satellite_link.py`
- Test: `tests/test_satellite_link.py`

**Interfaces:**
- Consumes: Task 1's `greet`, `Refused`, and `send`/`receive`.
- Produces `SatelliteLink(address: str, name: str, hostname: str, password: str, sample_rate: int, unclean: bool, on_message: Callable[[Dict, bytes], None], on_connected: Callable[[bool], None])`, with:
  - `.start()` and `.stop()`;
  - `.send(kind, payload=b"", **fields) -> bool` - False when not connected. It's thread-safe, and a send never blocks for more than the 2s socket timeout;
  - `.connected -> bool`.
- **Retrying:** it reconnects after 2s, doubling to at most 30s.
- **Staying in touch:** it pings every 5s. 15s with nothing heard means the connection has dropped.
- **Refusals:** the same refusal is logged once until it changes or a connection succeeds.
- `on_message` runs on the link's own reading thread.

- [ ] **Step 1: Write the failing tests**, against a fake brain on a listening socket.
  - `test_the_link_connects_and_reports_it`.
  - `test_the_link_reconnects_after_the_brain_goes`.
  - `test_refusals_are_logged_once_and_backed_off`.
  - `test_nothing_heard_for_15_seconds_counts_as_dropped`: patch the times.
  - `test_send_says_false_while_not_connected`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Keep the satellite connected to its brain".

---

### Task 6: The satellite program

**Files:**
- Create: `satellite.py` (the Pi's entry point)
- Modify: `config.yaml` - add `backend.satellite: ""`, this satellite's name. Empty means the hostname.
- Test: `tests/test_satellite.py`

**Interfaces:**
- Consumes:
  - Task 4's `AlarmCopy`, `StateCopy`, `FallbackRinger` and `Phrase`.
  - Task 5's `SatelliteLink`.
  - `Microphone`, `make_echo_canceller`, `Speaker`, `WakeWordDetector`, `Timeline` and `watchdog`, all as they are.
- Produces `Satellite(config)`, with `.run()`.
- **When the wake word is heard** (`WakeWordDetector.listen_once` returns), in this order:
  1. Stop every playback of kind `"reply"`, and `silence("announcement")`.
  2. Pause every brain playback of kind `"music"`.
  3. If the fallback is ringing, `dismiss()` it, and that's all.
  4. If connected, send `wake {talked_over}`, where `talked_over` means a `"reply"` was playing. Then send `audio`: first the 0.3s (`audio.pre_roll`) before the wake word, then 100ms pieces from the microphone. Stop sending on `stop_listening`, after 10s of audio (`audio_end {reason: "limit"}`), or when the connection drops.
  5. If not connected, play the `Phrase`.
- **When the connection drops mid-recording,** stop sending and play the `Phrase`.
- **While already sending a recording,** a wake word starts nothing new.
- **Doing what the brain asks** (Task 1's list):
  - `play_sound` plays `sounds/<name>` through `Speaker.play_file`.
  - `play_stream`, `stream_audio` and `stream_end` go into `Speaker.play_stream`, as int16 converted to float at its rate and channels.
  - `set_playback` sets `paused` and `volume`.
  - `stop`, `silence` and `allow` act on the speaker.
  - `state` updates `StateCopy`, and sets the wake word's mute.
  - `alarms` replaces `AlarmCopy`; `phrase` replaces `Phrase`.
  - `listen` starts sending from now.
  - `restart` exits with code 1, so systemd starts it again.
- **What it tells the brain:**
  - `played {id, stopped, position}` when each of the brain's playbacks ends;
  - `progress {id, position}` each second for `"music"`;
  - `away {...}` straight after each connect. Clear the report once that's sent.
- **The fallback ringer** checks the copy every second while not connected. On connecting, it stops ringing.
- **Timing:** a `Timeline` per wake, with marks `"wake"` and `"reply_audio"` (when the first audio of a `"reply"` plays), saved to `paths.LOGS_DIR/latency.jsonl`.
- **The rest:**
  - `hello.unclean` comes from a running mark left by the last run, kept the way `main.py`'s `_note_start` keeps its mark.
  - The name is `backend.satellite`, or the hostname if that's empty.
  - It keeps the watchdog fed, as `main.py` does.

- [ ] **Step 1: Write the failing tests.** Use a fake brain over `socketpair`, and fake microphone, speaker and wake word.
  - `test_the_wake_word_sends_the_pre_roll_then_the_recording`.
  - `test_the_recording_stops_when_the_brain_says`.
  - `test_the_recording_stops_after_10_seconds_whatever_the_brain_says`.
  - `test_a_dropped_connection_mid_recording_says_the_phrase`.
  - `test_the_wake_word_over_a_reply_stops_it_and_says_so`.
  - `test_a_wake_while_streaming_starts_nothing_new`.
  - `test_a_stream_from_the_brain_plays_and_is_reported_done`.
  - `test_with_no_brain_the_wake_word_gets_the_phrase`.
  - `test_with_no_brain_due_alarms_ring_and_the_wake_word_dismisses_them`.
  - `test_the_away_report_is_sent_on_connecting`.
  - `test_mute_from_the_brain_stops_the_wake_word`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Add the satellite: hearing, playing and stopping, for a brain elsewhere".

---

### Task 7: The brain's view of one satellite

**Files:**
- Create: `voice_assistant/remote.py`
- Test: `tests/test_remote.py`

**Interfaces:**
- Consumes: Task 2's `AudioFeed`, and Task 1's messages.
- Produces `RemoteSatellite(stream, hello: Dict)`, with:
  - `.name`, `.hostname`, `.sample_rate`;
  - `.speaker: RemoteSpeaker`;
  - `.wakes: queue.Queue` of `{"talked_over": bool, "feed": AudioFeed}`;
  - `.connected -> bool`;
  - `.serve()` - reads until the satellite goes. Then every feed is ended, every playback is done, and `.connected` is False;
  - `.send(kind, payload=b"", **fields) -> bool`;
  - `.listen() -> AudioFeed`;
  - `.stop_listening()`;
  - `.set_state(volumes: Dict[str, float], muted: bool)`;
  - `.send_alarms(entries)`;
  - `.send_phrase(audio: bytes, rate: int)`;
  - `.restart()`;
  - `.close()`.
- `RemoteSpeaker` mirrors what `Speaker` offers its callers today:
  - `.sample_rate` (the satellite's);
  - `.play(audio, sample_rate=None, volume=1.0, kind="reply")`;
  - `.play_stream(pieces, sample_rate=None, volume=1.0, kind="reply", paused=False, most_ahead=None)` - sends on a thread of its own, in pieces of at most 100ms. For music it stays at most `most_ahead` seconds ahead of the satellite's `progress`;
  - `.play_file(filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None)` - sends `play_sound` with the file's basename;
  - `.stop(kinds=None)`, `.silence(kind)`, `.allow(kind)`, `.close()`.
  - Each `play*` returns a `RemotePlayback`, or None when the satellite isn't connected.
- `RemotePlayback`:
  - `.wait(timeout=None) -> bool`, `.stop()`, `.done() -> bool`;
  - `.paused` and `.volume` - setting either sends `set_playback`;
  - `.position: int` - samples at `.sample_rate`, from `progress` and `played`.
  - When the satellite goes, every playback is done.

- [ ] **Step 1: Write the failing tests**, against a fake satellite over `socketpair`.
  - `test_a_stream_reaches_the_satellite_as_16_bit_pieces`.
  - `test_waiting_ends_when_the_satellite_says_played`.
  - `test_a_wake_brings_a_feed_of_the_recording`.
  - `test_pausing_music_tells_the_satellite`.
  - `test_when_the_satellite_goes_every_playback_and_feed_ends`.
  - `test_nothing_plays_on_a_satellite_which_isnt_connected`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Let the brain play, stop and hear through a satellite".

---

### Task 8: One satellite's assistant, on the brain

**Files:**
- Create: `voice_assistant/session.py`, moving `SmartAssistant`'s conversation logic out of `main.py`.
- Delete: `main.py`
- Modify: `voice_assistant/ha_device.py` (`NovaDevice.from_config(..., hostname: Optional[str] = None)`, used for the identifier in place of this machine's)
- Modify: `voice_assistant/speech_to_text.py` (add `NemotronSpeechToText`)
- Test: `tests/test_session.py`, moved and adapted from `tests/test_main.py` (delete that). Also `tests/test_ha_device.py` and `tests/test_speech_to_text.py`.

**Interfaces:**
- Consumes:
  - Task 2's `record_until_silence` and `AudioFeed`.
  - Task 3's clock.
  - Task 7's `RemoteSatellite` and `RemoteSpeaker`.
- Produces:
  - `NemotronSpeechToText(nemotron)`. Its `.listen()` returns an object with `.add(audio)`, `.finish(audio) -> {"text": str}` and `.cancel()`, as `RemoteTranscription` does today.
  - `Session(brain, satellite: RemoteSatellite)`, with:
    - `.run()` - answers each wake from `satellite.wakes` until it disconnects;
    - `.close()`;
    - `.announce(message, ...)` - `_announce` made public, for the brain's clock and alerts;
    - `.run_routine(routine)`;
    - `.activity: str`;
    - `.muted: bool` and `.set_muted(muted)`;
    - `.volume: VolumeControl`, `.music_player`, `.actions`, `.home`;
    - `.device: Optional[NovaDevice]`;
    - `.boop(level)`.
- **The brain object a session uses** (Task 9 builds it): `brain.config`, `brain.clock`, `brain.memory`, `brain.routines`, `brain.ntfy`, `brain.weather`, `brain.tts` (shared), `brain.stt` (a `NemotronSpeechToText`), `brain.sounds_folder`.
- **What each session has of its own:**
  - `make_detector(config)` - the speech detector that decides when the user has stopped;
  - `VolumeControl`, saved per satellite in `paths.DATA_DIR/volume-<name>.json`;
  - the mute, in `paths.DATA_DIR/microphone-<name>.json`;
  - `make_player(config, satellite.speaker, volume)` and `MusicHold`;
  - `HomeAssistant.from_config` - its own, for its own room;
  - its own `ActionExecutor`, `LLMHandler` and `Conversation`;
  - `NovaDevice.from_config(..., hostname=satellite.hostname)`.
- **What changes from `main.py`:**
  - The wake comes from `satellite.wakes`.
  - The recording is `record_until_silence(feed, ...)`, with `keep_before=0`, since the satellite sent the pre-roll. Then `satellite.stop_listening()`.
  - A follow-up calls `satellite.listen()` before the listening sound.
  - `_say` counts as talked over when a wake with `talked_over` arrives while the reply plays.
- **Changes to the Home Assistant device's controls:**
  - Restart calls `satellite.restart()`.
  - Backend reports whether the brain's speech-to-text is loaded.
  - Mute and volume act on this session, and send `set_state` to the satellite.
  - The device is stopped when the session closes, so Home Assistant shows it unavailable.
- Everything else behaves as `main.py` did.

- [ ] **Step 1: Write the failing tests.** Move `tests/test_main.py`'s tests across, and add:
  - `test_a_wake_is_recorded_heard_and_answered_through_the_satellite`;
  - `test_talking_over_a_follow_up_starts_the_next_turn`;
  - `test_mute_is_kept_per_satellite_and_sent_to_it`;
  - `test_the_device_is_named_from_the_satellites_hostname`;
  - `test_a_turn_whose_satellite_goes_ends_quietly`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** Move the code; don't copy it.
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Move Nova's conversation to the brain, one session per satellite".

---

### Task 9: The brain

**Files:**
- Modify: `backend.py` - `main()` runs the `Brain`, and keeps Task 1's commands.
- Create: `voice_assistant/brain.py`
- Modify: `voice_assistant/routines.py` - a routine may carry `"satellite"`; none means the default one.
- Modify: `nova-backend.service` - `Type=notify` and `WatchdogSec=60`.
- Test: `tests/test_brain.py`, and `tests/test_backend.py` adapted.

**Interfaces:**
- Consumes:
  - Task 1's `admit` and `SatellitePasswords`.
  - Task 3's clock (`on_change`, `copy_for`, `apply_away`, `assign_unowned`).
  - Task 7's `RemoteSatellite`.
  - Task 8's `Session` and `NemotronSpeechToText`.
- Produces `Brain(config)`, with:
  - `.serve()` - listens on `backend.port` (10400), on 127.0.0.1 as now;
  - `.sessions: Dict[str, Session]`;
  - `.default_satellite() -> Optional[str]` - the first in `SatellitePasswords.names()`;
  - `.session_for(satellite: Optional[str]) -> Optional[Session]` - that one, or the default, if connected;
  - `.stop()`.
- **On each connection:**
  1. `admit`.
  2. Replace any session of the same name.
  3. Send `state`, `alarms` (`clock.copy_for`) and `phrase`. The phrase is the fallback phrase made by `brain.tts`.
  4. Apply the `away` report, if one comes.
  5. Run the session until it goes.
- **When the last run didn't stop properly** (`hello.unclean`), send the ntfy restart notice the way `main.py` did.
- **The clock:**
  - `on_timer_finished` goes to `session_for(satellite).announce(...)`, or nowhere if that satellite isn't connected, since it rings from its copy.
  - `on_change` re-sends `alarms` to each connected satellite.
  - At start, `clock.assign_unowned(default)` runs once a default exists.
- **ntfy alerts, and routines at their time,** go to `session_for(routine.get("satellite"))`.
- **The brain's own running mark and restart notice,** and the watchdog, work as `main.py`'s did.
- **One shared Piper.** `brain.tts`, the one `TextToSpeech`, is shared by every session. So `TextToSpeech.stream` must be safe for two sessions at once, with a lock around taking a Piper process. Add `test_two_sessions_can_speak_at_once` to `tests/test_text_to_speech.py`, and `voice_assistant/text_to_speech.py` to this task's files.

- [ ] **Step 1: Write the failing tests.** Use fake sessions and satellites.
  - `test_a_satellite_which_connects_gets_its_state_alarms_and_phrase`.
  - `test_a_second_connection_replaces_the_first`.
  - `test_a_timer_rings_on_its_satellite_or_waits_for_its_copy`.
  - `test_what_was_dismissed_while_away_doesnt_ring_again`, end to end with Task 3.
  - `test_a_satellite_which_goes_mid_turn_leaves_the_brain_working`.
  - `test_alerts_go_to_the_default_satellite`.
  - `test_an_unclean_satellite_is_told_to_the_phone`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests, the whole suite in both time zones, and ruff.**
- [ ] **Step 5: Commit** - "Make the backend Nova's brain, for every satellite".

---

### Task 10: The settings page on the brain, for every satellite

**Files:**
- Modify: `voice_assistant/settings_page.py` and `voice_assistant/web/settings.{html,js,css}`
- Modify: `voice_assistant/brain.py` - build and start the page.
- Test: `tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 9's `Brain`, which goes in place of the page's per-satellite collaborators.
- Produces:
  - `SettingsPage.from_config(config, brain, ...)`.
  - `state()` gains `"satellites": [{"name", "connected", "activity", "muted"}]`.
  - The volumes, music, lights, mute and beep act on the satellite a request names (`"satellite"`), or the default.
  - A satellite that isn't connected gives `{"success": False, "message": "<name> isn't connected."}`.
  - The alarms, routines, memory and keys work with no satellite connected.
  - Restart restarts `nova-backend`.
- **The page:**
  - The status panel lists each satellite.
  - A choice of satellite appears only when there's more than one.
  - Check the JS with `node --check`. The visual check is the controller's, in Task 14.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_page_lists_each_satellite`.
  - `test_a_volume_change_goes_to_the_satellite_named`.
  - `test_a_satellite_which_isnt_connected_is_said_so`.
  - `test_alarms_and_routines_work_with_no_satellite`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests, the whole suite in both time zones, ruff and `node --check`.**
- [ ] **Step 5: Commit** - "Look after every satellite from the brain's settings page".

---

### Task 11: Standalone mode goes

**Files:**
- Modify: `voice_assistant/speech_to_text.py`. Remove `SpeechToText`, `MoonshineSpeechToText`, `LiveTranscription`, `BackendSpeechToText`, `RemoteTranscription` and `make_speech_to_text`. Keep `looks_like_hallucination`, `_heard` and `NemotronSpeechToText`.
- Modify: `voice_assistant/microphone.py` - remove the `record_until_silence` call-through.
- Modify: `config.yaml` and `voice_assistant/config.py` - remove `speech_to_text`, `moonshine` and `whisper`.
- Delete: `scripts/compare_speech_to_text.py` and `tests/test_compare_speech_to_text.py`. Their job, deciding 0004, is done.
- Delete: `voice_assistant/network.py`, if nothing uses it once the rest has gone.
- Test: the whole suite.

- [ ] **Step 1: Remove them, and every test of only them.**
- [ ] **Step 2: Run the whole suite in both time zones, and ruff.** Expected: all pass, with nothing importing what's gone (`grep -rn "moonshine\|whisper\|BackendSpeechToText" --include=*.py .` finds nothing outside comments that explain history).
- [ ] **Step 3: Commit** - "Take standalone mode off the Pi: no Moonshine, Whisper or local Claude".

---

### Task 12: Installing each half, and the docs

**Files:**
- Modify: `requirements.txt` and `requirements.lock`. These become the satellite's: drop `moonshine-voice`, `faster-whisper`, `anthropic`, `spotipy`, `paho-mqtt` and `requests`, and keep the rest.
- Modify: `requirements-backend.txt`. Add, pinned at the versions in today's `requirements.lock`:
  - `anthropic`, `spotipy`, `paho-mqtt`, `requests`, `soundfile`, `soxr`;
  - `openwakeword` (for Silero), and whatever else the brain's imports need.
- Modify: `setup.sh` and `update.sh`, which take a role.
  - `bash setup.sh` installs a satellite, and `bash setup.sh --brain` the brain. The role is kept in `~/.config/nova/role`, so `update.sh` knows it.
  - **The satellite:** no Piper, Moonshine, Whisper or Nemotron downloads. Its audio setup and raspotify stay. `voice-assistant.service` runs `satellite.py`.
  - **The brain:** Piper, the voice and Nemotron go into `models/` (which may be a link to elsewhere - follow it). It needs `ffmpeg`. It installs `nova-backend.service`, with no audio setup.
- Modify: `voice-assistant.service` - `ExecStart` is `satellite.py`.
- Modify: `README.md`. Cover setting up the brain, setting up a satellite, `add-satellite`, and where the settings page is now. Keep 0005's Home Assistant section, and say it's set up on the brain.
- Modify: `docs/roadmap.md` - Phase 5 gains "The Pi as a satellite".
- Modify: `docs/decisions/0004-pve2-backend.md` - its header says stage 2 and standalone mode are replaced by 0006.
- Test: `bash -n setup.sh update.sh`, and the whole suite.

- [ ] **Step 1: Make the changes.**
- [ ] **Step 2: Check the scripts and the requirements.**
  - Run `bash -n setup.sh update.sh`.
  - On the Pi, `pip install --dry-run --no-deps -r requirements.lock` into a scratch venv must resolve, and so must the backend's.
  - Then run the whole suite in both time zones, and ruff.
- [ ] **Step 3: Commit** - "Install the Pi as a satellite and PVE2's container as the brain".

---

### Task 13: PVE2 ready for the brain (controller, with the user's OK)

Done by the controller, not an implementer. Ask the user before changing the container.

1. **Measure today, before anything changes.** Record these in the ledger:
   - From the Pi's `~/.local/share/nova/logs/latency.jsonl`, wake to `tts_done` (p50 and p90) over the last 7 days.
   - Nova's memory on the Pi (RSS of the service), and its processor use when idle.
2. **Ask the user for the container's changes,** then make them:
   - memory from 2GB to 4GB;
   - cores from 4 to 6, if step 5's check shows the brain needs them;
   - a mount from `/mnt/library/nova` to `/mnt/nova-files`, owned for the unprivileged container's `nova` user.
3. **Install the brain in container 107:**
   - Install `git`, and clone the `satellite` branch into `/home/nova/Home-Assistant`, in place of the copied files. Keep the old copy until step 5 passes.
   - Link `models` to `/mnt/nova-files/models`, and move the Nemotron model there.
   - Run `bash setup.sh --brain`.
4. **Copy across, not move,** from the Pi to the brain:
   - Into `~/.config/nova/.env`: `ANTHROPIC_API_KEY`, `HA_TOKEN`, `MQTT_PASSWORD`, `NAVIDROME_PASSWORD`, `NTFY_TOKEN` and the Spotify keys. Copy through pipes, never printed, and compare by sha256.
   - The settings in `config.yaml`.
   - The data: memory, routines, timers and alarms, volumes into `volume-home-assistant.json`, the mute, and the Spotify sign-in.
   - Then pipe the Pi's current `NOVA_BACKEND_TOKEN` into `python backend.py add-satellite home-assistant --password-stdin`, so the Pi keeps its password.
5. **Check the brain alone, with no satellite yet:**
   - The service is active and its watchdog is fed.
   - The settings page answers on 127.0.0.1:8765, with the routines, alarms and memory there.
   - Memory and processor use with Nemotron and Piper loaded. If PVE2's processor would hold Nova back, tell the user - it's a hardware limit.
6. **Settings page over HTTPS:** `tailscale serve --bg --https=443 http://127.0.0.1:8765` in the container. Tell the user the new address. Never put it in the repository.

### Task 14: Switching the Pi over (controller, with the user's OK)

1. Ask the user before pushing `satellite`, then push it.
2. **On the Pi:**
   - `git fetch && git switch satellite && bash update.sh`, so it's a satellite now.
   - Remove the Pi's old `tailscale serve` for the settings page.
   - Keep the Pi's old `.env`, settings and data, untouched, for a week - Nova's way back.
3. **Live checks:**
   - The Pi connects, and the brain's log names it.
   - The Home Assistant device is still "Nova", in the Bedroom, with eleven controls.
   - A question gets an answer in Nova's voice, and talking over a reply stops it.
   - "Lights on", a timer, an alarm and music (Navidrome) all work.
   - With the brain service stopped: a due timer rings, the wake word stops it, and asking anything gets the phrase. The brain is told once it's back.
   - The settings page from the user's phone, at 1440 and 390 wide.
4. **Measure again,** as in Task 13 step 1. Compare wake to `reply_audio` with the old wake to `tts_done`, and the Pi's memory. Tell the user which smaller Pi the numbers allow.
5. **After a week of it working,** with the user's OK, delete the Pi's old keys and data (Task 13's list), and the container's old copied files.

## Decisions made in writing this plan (not in the spec)

- **Ringing:** while connected, the brain rings timers and alarms through the satellite's speaker, as `main.py` did through its own. The satellite rings from its copy only while it can't reach the brain. Clock's bursts, build-up and "...stop" all stay in one place.
- **Restart:** Home Assistant's Restart restarts the satellite program. The settings page's Restart restarts the brain.
- **Backend sensor:** Home Assistant's Backend shows whether the brain's speech-to-text is loaded.
- **Defaults:** ntfy alerts, Home Assistant's Speak from automations, and routines with no satellite go to the default satellite - the first one added.
- **Music on the wake word:** the satellite pauses music itself when it hears the wake word, so it goes quiet at once. The brain's own pause and resume follow, as now.
- **Big files:** the brain's models reach `library` through a link from `models/`, so no code needs a path setting.
- **Requirements:** `requirements.txt` and `.lock` become the satellite's, and `requirements-backend.txt` the brain's, so the Pi's update path doesn't change.
- **The comparison script:** `compare_speech_to_text.py` is removed, its decision made.
