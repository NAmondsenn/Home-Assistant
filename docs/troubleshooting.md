# Troubleshooting

By symptom. The two places to look first are the log, which says what Nova heard and decided at each step, and the sound cards the Pi can see.

```bash
journalctl --user -u voice-assistant -n 100
```
```bash
aplay -l && arecord -l
```

## Nova doesn't make a sound

- **Check the speakers are on the right input.** Edifier speakers switch input with a press of the volume knob on the right-hand speaker, and the light changes colour with each one - this one cost an evening.
- **Check `/etc/asound.conf` names cards which exist.** It names the speaker card (`hw:CARD=...`), and if that speaker is swapped for another, nothing plays through the shared output any more. Compare the `CARD=` names with `aplay -l`, fix them, and restart the assistant. This happened when a HyperX speaker (`Sound`) was replaced by a USB-C adapter (`Audio`).
- **Play something directly**, to rule the assistant out:

  ```bash
  aplay -D default sounds/listening.wav
  ```

## Nova doesn't hear me

- **Is the wake word being heard at all?** Look for `Wake word detected` in the log. If it never appears, raise your voice or lower `thresholds.wake_word_confidence`.
- **Was the microphone unplugged?** Nova reopens it by itself - the log says `No audio from the microphone ... reopening it`, then `Microphone working again` - trying again every few seconds at first, then every 30. If it never comes back, check `arecord -l` and the capture card in `/etc/asound.conf`.
- **Listening for "Hey Jarvis" instead?** Then `models/hey_nova.onnx` is missing - the log says so at startup.

## Nova cuts me off, or keeps listening

- **Cut off mid-sentence** - raise `thresholds.silence_duration`, or lower `thresholds.vad_threshold`.
- **Keeps listening after I've finished** - usually because something in the room sounds like speech, such as a TV. Raise `thresholds.vad_threshold`.
- The log shows `Speech detected` and `Silence detected after ...s` for each recording, and `logs/latency.jsonl` has the timings.

## Nova answers something nobody said

Whisper sometimes finds words in noise. The obvious ones ("Thank you.", "Thanks for watching") are already thrown away, logged as `Ignoring '...'`. If a false wake word is the cause, raise `thresholds.wake_word_confidence`.

## Music doesn't play

- **"The music isn't available right now"** - the music player didn't start. The log says why, near the top: for Navidrome, `navidrome.url`, `navidrome.username` or `NAVIDROME_PASSWORD` is missing; for Spotify, its sign-in.
- **"I can't reach Navidrome right now"** - check Navidrome is running, and that its address in `config.local.yaml` opens from the Pi: `curl -I http://navidrome.local:4533`.
- **"Navidrome didn't accept my password"** - check `NAVIDROME_PASSWORD` in `.env` against the Navidrome user named in `navidrome.username`.
- **A song is skipped straight away** - the log says `Couldn't play '...'` with ffmpeg's reason. Usually a file Navidrome can't convert to MP3; check its transcoding settings.
- **"Spotify needs signing in again on the Pi"** - run the sign-in again: `venv/bin/python -m voice_assistant.spotify_controller`.
- **"I can't find the ... speaker"** - raspotify isn't running, or its name doesn't match `spotify.device_name`. Check `systemctl status raspotify`. It's restarted every night anyway (`raspotify-restart.timer`), since its connection can drop silently.

## The lights or the calendar don't work

- **"Home Assistant isn't set up yet"** - `home_assistant.url` isn't set in `config.local.yaml`, or `HA_TOKEN` isn't in `.env`. The log says which at startup.
- **"Home Assistant didn't accept my token"** - the token has been deleted or mistyped. Make a new one for Nova's Home Assistant user.
- **"Sorry, I am not aware of any area called ..."** - that's Home Assistant's own answer. Check the light is exposed to Assist (Settings > Voice assistants > Expose), and what it and its area are called there.
- **The calendar says nothing's on** - check the calendar shows events in Home Assistant itself, and, if `home_assistant.calendars` is set, that it's listed there.

## A timer or alarm didn't go off

- Timers and alarms are in `data/timers.json` and `data/alarms.json`. Anything which came due while Nova was off is announced late, as "While I was off: ...".
- Nothing goes off until the Pi's clock has been set from the internet, so a Pi with no network at boot holds them until it has.

## Claude's replies fail

The failure is said out loud: "took too long" (a slow connection), "can't reach the internet", an API key problem (check `.env`), or "too many requests" (wait a minute). The everyday commands are handled without Claude, and timers, alarms, Nova's own volume and the time keep working offline. With no network at all - the cable out, or the Wi-Fi down - Nova knows straight away from the Pi's routing table, and says so rather than waiting out its timeouts.

## The weather doesn't work

Set `weather.location` in `config.yaml` to your town or city, then restart.

## It's slow

```bash
venv/bin/python scripts/latency_report.py
```

That shows how long each stage takes, typically and on a bad day, so you can see which one is to blame.
