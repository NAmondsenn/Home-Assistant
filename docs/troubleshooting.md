# Troubleshooting

By symptom. The two places to look first are the log, which says what Nova heard and decided at each step, and the sound cards the Pi can see.

```bash
journalctl --user-unit voice-assistant -n 100
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

## Nova's voice or the music crackles

- **Look for `The speaker ran dry` in the log** - each time the speaker wasn't given audio quickly enough, counted and mentioned at most once a minute. If it keeps appearing, raise `OUTPUT_LATENCY` in `voice_assistant/speaker.py` by 0.02 (one of the shared output's 21ms periods) - stopping takes that much longer.
- **An `/etc/asound.conf` without `period_size` in it** is from before the short bursts, and works in 125ms ones - `update.sh` says so. Run `bash setup.sh` and let it write the file again, then restart Nova and raspotify.
- `The microphone overflowed` is the same thing the other way round - some of what was said was lost.

## Nova doesn't hear me

- **Is the wake word being heard at all?** Look for `Wake word detected` in the log. If it never appears, raise your voice or lower `thresholds.wake_word_confidence`.
- **Was the microphone unplugged?** Nova reopens it by itself - the log says `No audio from the microphone ... reopening it`, then `Microphone working again` - trying again every few seconds at first, then every 30. If it never comes back, check `arecord -l` and the capture card in `/etc/asound.conf`.
- **Listening for "Hey Jarvis" instead?** Then `models/hey_nova.onnx` is missing - the log says so at startup.
- **From across the room, quietly** - "Hey Nova" may still be heard, then nothing (`No speech within 3.0s`), or the wrong words. A small USB microphone only picks up so much; closer, or louder, is the fix.

## Nova doesn't hear "Hey Nova" over music

- **Is echo cancelling on?** The log says `Echo cancelling on` at startup, and, once music has played for 10 to 20 seconds, `Echo cancelling: what's played is heard ...ms later`. Until that second line, the music isn't being taken out yet. Without the first, speexdsp is missing (`apt install libspeexdsp1`) or `audio.echo_cancellation` is off.
- **It only takes out what Nova plays itself** - Navidrome, its voice and timers. Spotify plays through raspotify, which Nova doesn't hear, so it can't be taken out.
- **It takes the music down, not away** - about 5 to 14dB on the Pi, more the longer it plays. Loud music right next to the microphone can still drown you out; turning it down a little helps a lot.

## Nova cuts me off, or keeps listening

- **Cut off mid-sentence** - raise `thresholds.silence_duration`, or lower `thresholds.vad_threshold`.
- **Keeps listening after I've finished** - usually because something in the room sounds like speech, such as a TV. Raise `thresholds.vad_threshold`.
- The log shows `Speech detected` and `Silence detected after ...s` for each recording, and `latency.jsonl` in `~/.local/share/nova/logs` has the timings.

## Nova mishears, or answers something nobody said

- **The log shows what was heard**, as `Transcription complete (...): '...'`. Moonshine's known slips are allowed for - a lone "stop" heard as "soap", "sop" or "so" still stops things, and "skip" heard as "scap" still skips. Names it keeps getting wrong can go in `moonshine.keyterms`.
- **Words found in noise** - the obvious ones ("Thank you.", "Thanks for watching") are thrown away, logged as `Ignoring '...'`. If a false wake word is the cause, raise `thresholds.wake_word_confidence`.

## Music doesn't play

- **"The music isn't available right now"** - the music player didn't start. The log says why, near the top: for Navidrome, `navidrome.url`, `navidrome.username` or `NAVIDROME_PASSWORD` is missing; for Spotify, its sign-in.
- **"I can't reach Navidrome right now"** - check Navidrome is running, and that its address in `~/.config/nova/config.yaml` opens from the Pi: `curl -I http://navidrome.local:4533`.
- **"Navidrome didn't accept my password"** - check `NAVIDROME_PASSWORD` in `~/.config/nova/.env` against the Navidrome user named in `navidrome.username`.
- **A song is skipped straight away** - the log says `Couldn't play '...'` with ffmpeg's reason. Usually a file Navidrome can't convert to MP3; check its transcoding settings.
- **"Spotify needs signing in again on the Pi"** - run the sign-in again: `venv/bin/python -m voice_assistant.spotify_controller`.
- **"I can't find the ... speaker"** - raspotify isn't running, or its name doesn't match `spotify.device_name`. Check `systemctl status raspotify`. It's restarted every night anyway (`raspotify-restart.timer`), since its connection can drop silently.

## The lights or the calendar don't work

- **"Home Assistant isn't set up yet"** - `home_assistant.url` isn't set in `~/.config/nova/config.yaml`, or `HA_TOKEN` isn't in `~/.config/nova/.env`. The log says which at startup.
- **"I can't reach Home Assistant right now"** - check it's running and its address opens from the Pi. With no network at all, it's said straight away.
- **"Home Assistant didn't accept my token"** - the token has been deleted or mistyped. Make a new one for Nova's Home Assistant user.
- **"Sorry, I am not aware of any area called ..."** - that's Home Assistant's own answer. Check the light is exposed to Assist (Settings > Voice assistants > Expose), and what it and its area are called there.
- **The calendar says nothing's on** - check the calendar shows events in Home Assistant itself, and, if `home_assistant.calendars` is set, that it's listed there.

## A timer or alarm didn't go off

- Timers and alarms are in `timers.json` and `alarms.json` in `~/.local/share/nova`. Anything which came due while Nova was off is announced late, as "While I was off: ...".
- Nothing goes off until the Pi's clock has been set from the internet, so a Pi with no network at boot holds them until it has.

## Claude's replies fail

The failure is said out loud: "took too long" (a slow connection), "can't reach the internet", an API key problem (check `~/.config/nova/.env`), or "too many requests" (wait a minute). The everyday commands are handled without Claude, and timers, alarms, Nova's own volume and the time keep working offline. With no network at all - the cable out, or the Wi-Fi down - Nova knows straight away from the Pi's routing table, and says so rather than waiting out its timeouts.

## The weather doesn't work

Set `weather.location` in `~/.config/nova/config.yaml` to your town or city, then restart.

## Nova won't start after a settings change

The log ends with what's wrong and where - e.g. `'thresholds.silence_duraton' isn't a setting. Did you mean 'silence_duration'?`. Fix it in `~/.config/nova/config.yaml` and restart.

## Nova restarted by itself

If the main loop stops moving for two and a half minutes - stuck on something which should have timed out - systemd restarts Nova, rather than leaving it deaf. The log says `The main loop hasn't moved for ...s` just before, and systemd `Watchdog timeout` - or only the second, if the whole of Nova froze. Whatever it was doing then is the thing to look at.

## It's slow

```bash
venv/bin/python scripts/latency_report.py
```

That shows how long each stage takes, typically and on a bad day, so you can see which one is to blame.
