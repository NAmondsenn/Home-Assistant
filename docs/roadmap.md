# Roadmap

Where the project stands, using the finding IDs from the audit of 19 October 2026 (kept outside the repository, as "Nova Assistant Audit.md"). When coming back after a break, start here, then read the newest records in [decisions/](decisions/).

## Branches

- `main` - the version from before the audit fixes.
- `phase-1` - Phase 1, tested on the Pi.
- `phase-2` - Phase 2, built on `phase-1`.

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

## Phase 2: Raspberry Pi completion - built, being tested on the Pi

- [x] PERF-04 one microphone stream with pre-roll, which also fixed the microphone going deaf when replugged, and about a fifth of its audio being lost
- [x] BUG-25 Silero voice activity · BUG-24 no wrap-around on loud audio
- [x] BUG-07 one speaker owner, which stops sounds part way · BUG-18 timers ring on their own threads · BUG-36 · UX-03 music paused for announcements
- [x] UX-04 alarms in bursts, building up from quiet
- [x] BUG-32 everyday commands handled locally, and offline · BUG-20 Whisper hallucinations dropped · BUG-14 clock times worked out locally
- [x] BUG-13, 33, 41, 16, 17, 19, 21, 22, 23
- [x] BUG-40 weather · UX-02 clearer failures · SEC-03 log rotation and transcripts switch · SEC-04 private token cache · DEP-03 fresh install starts
- [x] TEST-02 clock above 80% coverage, Spotify matching tested · TEST-03 ruff and GitHub checks
- [x] README and these docs
- [ ] Live test on the Pi - [release-checklist.md](release-checklist.md)
- [ ] 72-hour soak test, with `scripts/soak_monitor.py` running
- [ ] UX-04 physical stop button - needs a button and a GPIO pin
- [ ] Set `weather.location`
- [ ] Merge into `main`, and declare the Pi version complete - the audit's section 15 lists what that means

## Phase 3: code quality and performance

- [ ] PERF-01, PERF-02 Piper kept running, Claude's reply streamed into speech a sentence at a time, prompt caching
- [ ] ARCH-01 a `Conversation` class out of `main.py` · ARCH-04 action registry · ARCH-09 action results
- [ ] ARCH-02, ARCH-03 one paths module, validated config kept outside the repository · DEP-04 install, update and uninstall scripts
- [ ] PERF-03 Whisper settings A/B · PERF-05 drop librosa · PERF-07 fewer Spotify calls · DEP-05 watchdog
- [ ] SEC-05, SEC-07, ARCH-05 to ARCH-08, BUG-26, BUG-29, BUG-31
- [ ] Shorter bursts on the shared input and output (`/etc/asound.conf` and `setup.sh`), currently 125ms - quicker wake word and end of speech, and stopping sounds sooner

## Phase 4 onwards

- Phase 4 - a backend on PVE2, and the Pi as a satellite, with standalone mode kept as a fallback.
- Phase 5 - Home Assistant for the lights and devices, with confirmations for anything security-sensitive.
- Phase 6 - push-to-talk clients for Windows and macOS.

## Outside the code

- [ ] Sign out the Google sessions and old Tailscale machines whose cookies were in Git history
- [ ] Ask GitHub Support to purge the cached old commits
- [ ] Delete the old backups (`nova-backup.tgz` on the Mac, the `.bundle` on the Windows PC)
- [ ] Consider new Anthropic and Spotify keys, in case either was ever in `.bash_history`
