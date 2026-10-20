# Roadmap

Where the project stands, using the finding IDs from the audit of 19 October 2026 (kept outside the repository, as "Nova Assistant Audit.md"). When coming back after a break, start here, then read the newest records in [decisions/](decisions/).

## Branches

- `main` - the version from before the audit fixes.
- `phase-1` - Phase 1, tested on the Pi.
- `phase-2` - Phase 2, built on `phase-1`, tested on the Pi.
- `phase-3` - Phase 3, the Home Assistant and Navidrome work, and fixes from testing, built on `phase-2`, running on the Pi.

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
- [ ] 72-hour soak test, with `scripts/soak_monitor.py` running
- [ ] UX-04 physical stop button - needs a button and a GPIO pin
- [ ] Merge into `main`, and declare the Pi version complete - the audit's section 15 lists what that means

## Phase 3: code quality, speed, and the home - built on `phase-3`, running on the Pi

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
- [x] Tried by voice: a lone "stop" heard by Moonshine, and talking over a reply
- [ ] Go through the [release checklist](release-checklist.md) on the Pi with Phase 3 on it
- [ ] Streaming Claude's reply into speech - looked at, left for now: about a tenth of a second, for guessing whether a tool call is coming
- [x] SEC-05 downloads checked against pinned hashes · ARCH-05 one atomic JSON save · ARCH-06, ARCH-07 dead code and stale comments · ARCH-08 LF everywhere · BUG-29 mixed yes/no answers · BUG-31 `max_tokens` used as set · BUG-26 already fixed with BUG-14
- [ ] Shorter bursts on the shared input and output (`/etc/asound.conf` and `setup.sh`), currently 125ms - quicker wake word and end of speech, and stopping sounds sooner

## Phase 4 onwards

- Phase 4 - a backend on PVE2, and the Pi as a satellite, with standalone mode kept as a fallback. Speech-to-text there could be Parakeet, if PVE2 has a GPU - measured against Moonshine first.
- Phase 5 - more of Home Assistant than the lights and calendar, with confirmations for anything security-sensitive. Jellyfin through Home Assistant's media players, for the TV.
- Phase 6 - push-to-talk clients for Windows and macOS.

## Outside the code

- [ ] Sign out the Google sessions and old Tailscale machines whose cookies were in Git history
- [ ] Ask GitHub Support to purge the cached old commits
- [ ] Delete the old backups (`nova-backup.tgz` on the Mac, the `.bundle` on the Windows PC)
- [ ] Consider new Anthropic and Spotify keys, in case either was ever in `.bash_history`
