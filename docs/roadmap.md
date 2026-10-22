# Roadmap

Where the project stands, using the finding IDs from the audit of 19 October 2026 (kept outside the repository, as "Nova Assistant Audit.md"). When coming back after a break, start here, then read the newest records in [decisions/](decisions/).

## Branches

- `main` - everything up to and including Phase 4 and the routines, merged on 21 October, and running on the Pi. New work branches off it.
- `phase-1` - Phase 1, tested on the Pi.
- `phase-2` - Phase 2, built on `phase-1`, tested on the Pi.
- `phase-3` - Phase 3, the Home Assistant and Navidrome work, and fixes from testing, built on `phase-2`. Merged into `main`.
- `phase-4` - Phase 4, built on `phase-3`. The plan is in [decisions/0004](decisions/0004-pve2-backend.md). Merged into `main`.
- `routines` - the routines, built on `phase-4`. Merged into `main`.

## Phase 0: housekeeping - done

- [x] SEC-01: the home directory and browser profiles purged from Git history, the old `spotify` branch deleted.

## Phase 1: critical fixes - done, tested on the Pi

- [x] PERF-08 latency timeline · TEST-01 real test suite · DEP-01 `requirements.lock`
- [x] BUG-01, BUG-11 Spotify token refresh, and starting without a network
- [x] BUG-12 Claude timeouts and specific failure messages · BUG-03 history records what was said
- [x] BUG-04, BUG-05 alarms survive restarts, repeat days read properly · BUG-06, BUG-15 dismissing and cancelling the right thing
- [x] BUG-02 Spotify pause off the critical path · BUG-08 Whisper warm-up · BUG-09, BUG-10 volume routing and no reset at startup
- [x] UX-01 listening sound
- [x] Found while testing: the time read out as "nineteen thirty-six", the listening sound silent unless music was playing

## Phase 2: Raspberry Pi completion - built, tested on the Pi

- [x] PERF-04 one microphone stream with pre-roll, which also fixed the microphone going deaf when replugged, and about a fifth of its audio being lost
- [x] BUG-25 Silero voice activity · BUG-24 no wrap-around on loud audio
- [x] BUG-07 one speaker owner, which stops sounds part way · BUG-18 timers ring on their own threads · BUG-36 · UX-03 music paused for announcements
- [x] UX-04 alarms in bursts, building up from quiet
- [x] BUG-32 everyday commands handled locally, and offline · BUG-20 Whisper hallucinations dropped · BUG-14 clock times worked out locally
- [x] BUG-13, 33, 41, 16, 17, 19, 21, 22, 23
- [x] BUG-40 weather · UX-02 clearer failures · SEC-03 log rotation and transcripts switch · SEC-04 private token cache · DEP-03 fresh install starts
- [x] TEST-02 clock above 80% coverage, Spotify matching tested · TEST-03 ruff and GitHub checks
- [x] README and these docs
- [x] Live test on the Pi - [release-checklist.md](release-checklist.md)
- [x] Set `weather.location`
- [x] Soak test, with `scripts/soak_monitor.py` running and all of Phase 3 on the Pi - from Tuesday 20 October at 17:09, stopped after 16 hours as plenty: 23 conversations, memory from 627 MB to 677 MB in the first few hours and then flat, 28 threads throughout, no restarts, and nothing logged as a warning or an error
- Deferred, agreed 20 October: UX-04 physical stop button - to be talked over with a remote with a microphone of its own, which could do the same and more
- [x] Merge into `main`, and declare the Pi version complete - the audit's section 15 lists what that means. Merged on 21 October, along with Phase 4 and the routines

## Phase 3: code quality, speed, and the home - built on `phase-3`, merged into `main`

- [x] PERF-03 one Whisper guess, measured · a 0.6s end-of-speech pause
- [x] PERF-01 Piper kept loaded, speech played a sentence at a time from memory (PERF-06) · PERF-02 Claude's instructions cached
- [x] ARCH-04, ARCH-09 each action declared once, with one way of confirming it · ARCH-01 `Conversation` out of `main.py`
- [x] Home Assistant lights and calendar, through Assist and a user of Nova's own (brought forward from Phase 5)
- [x] Navidrome music through Nova's own speaker, Spotify kept as an option
- [x] From testing: "...stop" while ringing, "Hey Nova" straight after a reply
- [x] PERF-05 librosa and PyAudio gone · PERF-07 fewer Spotify calls · DEP-05 watchdog · SEC-07 service hardening
- [x] ARCH-02, ARCH-03 settings, keys and data outside the repository, checked as they're read · DEP-04 install, update and uninstall
- [x] On the Pi: `HA_TOKEN` and `NAVIDROME_PASSWORD` in `~/.config/nova/.env`, the addresses in `~/.config/nova/config.yaml`
- [x] Claude Haiku 5.5, with thinking off
- [x] Moonshine for speech-to-text - 0.83s and 1.5% of words wrong, against Whisper's 2.16s and 4.5%, measured on the Pi
- [x] Echo cancelling, so "Hey Nova" is heard over music, and over Nova's own replies, which stop when it's said
- [x] Work and uni times from the calendar, and the next shift or the next few · one thing looked for, "when's my MOT?" · calendar answers kept short
- [x] Memory: facts picked up as they come up, with "I'll remember that", heard back and forgotten by voice
- [x] From testing: liked songs, stopped music staying stopped, clarifying questions asked as questions, times on the hour read as "2 PM"
- [x] "Midnight" and "noon" rather than "12 AM" and "12 PM" · three days or more of the calendar as an overview, a week in 14 seconds rather than 52
- [x] Tried by voice: a lone "stop" heard by Moonshine, and talking over a reply
- [x] Went through the [release checklist](release-checklist.md) on the Pi with Phase 3 on it, 20 October - all passed, once a lone "so" counted as "stop", Home Assistant and Navidrome gave up at once with no network, and "set of one minute timer" was understood
- Deferred to Phase 4, agreed 20 October: streaming the reply into speech. With Haiku it would save about a tenth of a second - the whole reply takes a median 1.1s, and is usually one sentence - but with a slower or local model it would save seconds. To be built behind `llm.py`, for every model, with tool calls told apart as they arrive
- [x] SEC-05 downloads checked against pinned hashes · ARCH-05 one atomic JSON save · ARCH-06, ARCH-07 dead code and stale comments · ARCH-08 LF everywhere · BUG-29 mixed yes/no answers · BUG-31 `max_tokens` used as set · BUG-26 already fixed with BUG-14
- [x] Shorter bursts on the shared input and output - 21ms rather than 125ms, measured on the Pi: the microphone's audio reaches the wake word in about 50ms rather than 130ms, and a stopped sound goes quiet in under 200ms rather than 240ms. The times the speaker runs dry, or the microphone overflows, are logged

## Phase 4 onwards

- Phase 4 - a backend on PVE2, and the Pi as a satellite, with standalone mode kept as a fallback. Streaming the model's reply into speech, deferred from Phase 3, comes with any slower or local model. ntfy, both ways: homelab alerts announced on the speakers, with "tell me more" for the details (only urgent ones at night), and "send that to my phone" for an answer, a reminder nobody answered, or Nova having to restart itself. A settings page served by Nova - settings, keys (never shown back), Spotify sign-in, volumes, alarms, what it remembers, and how it's doing - its settings API in the backend, so standalone Pis have it too. Speech-to-text there could be Parakeet, on PVE2's processor since it has no graphics card - measured against Moonshine first.
  - [x] The plan: what runs on PVE2, what stays on the Pi, and how standalone mode works - [decisions/0004](decisions/0004-pve2-backend.md)
  - [ ] Moonshine working out the words while they're still being said - built on `phase-4`. Measured on the Pi with 38 recordings of the user's voice: the same words as before, and 0.24s to wait once the recording stopped rather than 0.74s (0.78s at worst rather than 2.27s), for 2.3 seconds of processing for each second of recording rather than 1.0 - with no microphone overflows while Nova ran alongside. On the Pi since 21 October, for when the backend can't be reached
  - [x] Recordings of the user's own voice - 38, made with `scripts/record_samples.py` through Nova's own microphone, alongside the soak. Measured with `scripts/compare_speech_to_text.py`, Parakeet TDT 0.6B v2 got 6.8% of words wrong where Moonshine small got 14-16%; leaving out two recordings whose words weren't certain, 2.4% against 6.5%
  - [x] Parakeet's speed on PVE2's processor: 0.58s typically once the recording stopped, 1.35s at worst, with 4.5% of words wrong - and a third of a millisecond to the Pi and back. More accurate than Moonshine, but slower than Moonshine working the words out as they're said
  - [x] NVIDIA's streaming Nemotron Speech on PVE2, which works as it hears: 0.29s once the recording stopped, 0.75s at worst, with 5.1% of words wrong and 23 of 25 everyday commands still handled - nearly Parakeet's accuracy at nearly live Moonshine's speed. It needs half a second of silence before each recording and 1.2s after
  - [x] Stage 1's model chosen, 20 October: Nemotron - see [decisions/0004](decisions/0004-pve2-backend.md)
  - [x] Stage 1's backend: `backend.py`, running in its own container on PVE2 (`nova`, reached over the tailnet only). Measured from the Pi with the user's 38 recordings, before they were deleted: 0.33s to wait once the recording stopped, 0.53s at worst, with 5.1% of words wrong - and next to no work for the Pi
  - [x] The Pi hearing through the backend, with Moonshine to fall back on - on the Pi since 21 October: `backend.address` set to `nova:10400`, and the password in the Pi's `~/.config/nova/.env`
  - [x] ntfy, both ways - on the Pi since 21 October: `homelab` alerts read out as they arrive (only priority 5 between 23:00 and 07:00, the rest for "any notifications?"), "tell me more", and "send that to my phone" to `clipboard`, a reminder nobody answered to `reminders`, and Nova restarting after a problem to `homelab`
  - [x] The settings page - on the Pi since 21 October, served by Nova, over the tailnet only and for the user's own Tailscale login: how it's doing, volumes, timers and alarms, what it remembers, keys (only ever saved, never shown back), the main settings, and a restart - laid out as a dashboard beside Home Assistant's and Homepage's, with what's playing, the lights to switch and dim, Home Assistant's scripts and scenes to run, a star showing what Nova's doing, and the volume beeping on Nova's speaker as it's dragged. Checked in Chrome at a laptop's, a tablet's and a phone's width. Signing in to Spotify from it later
  - [x] Routines - on the Pi since 21 October: a few words or a set time starting steps said as if by the user, so a routine can do anything Nova can; made, changed and run on the settings page, beside Home Assistant's scripts and scenes. Checked in Chrome at a laptop's and a phone's width
- Phase 5 - what the user asked for on 21 October, then a 72-hour soak test of everything since Phase 3:
  - [x] The lights first - including short commands like "lights on", which Home Assistant doesn't understand by itself: Assist is told which device is speaking and its room, and what it doesn't understand goes to Claude, which works the lights by name, room, brightness, colour and warmth. Nova is also a device in Home Assistant over MQTT, with its room taken from the area it's put in, and a microphone mute
  - [x] The Pi as a satellite - [decisions/0006](decisions/0006-the-pi-as-a-satellite.md). The Pi runs `satellite.py` alone - microphone, echo cancelling, wake word, end of speech, the speaker, and a small fallback which rings timers and alarms when the brain can't be reached - and PVE2's `nova` container runs `backend.py` as the brain for every satellite: speech-to-text, Claude, the voice, music, Home Assistant, timers, memory, the settings page and ntfy. No standalone mode. `setup.sh` and `update.sh` install either role, and `backend.py add-satellite` lets a satellite in with a password of its own. Built on `satellite`; the Pi and PVE2 are switched over after
  - [ ] The PC on and off by voice - on through Wake-on-LAN, off through something on the PC itself
  - [ ] The battery left on the phone, the Mac and the tablet
  - [ ] Fewer false wakes - about one an hour from the TV, some sent on as if the user had said them - with a check that it's the user's own voice saying "Hey Nova". The wake word's confidence alone can't tell them apart: the TV's run from 0.53 to 0.96, the user's from 0.52 to 0.99
  - Later, if wanted: Jellyfin through Home Assistant's media players, for the TV, and confirmations for anything security-sensitive
- Phase 6 - push-to-talk clients for Windows and macOS.

## Outside the code

- [ ] Sign out the Google sessions and old Tailscale machines whose cookies were in Git history
- [ ] Ask GitHub Support to purge the cached old commits
- [ ] Delete the old backups (`nova-backup.tgz` on the Mac, the `.bundle` on the Windows PC)
- [ ] Consider new Anthropic and Spotify keys, in case either was ever in `.bash_history`
