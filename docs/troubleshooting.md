# Troubleshooting

By symptom. Nova is in two parts (see [0006](decisions/0006-the-pi-as-a-satellite.md)): each **satellite** - the Pi - hears the wake word and plays what it's sent, and the **brain** - the `nova` container on PVE2 - hears what's said, works out the answer, speaks it and rings the timers and alarms. Each section says which to look on.

On the brain, the commands below are run from its clone, `/home/nova/Home-Assistant`. Anything which reads or writes Nova's settings, keys or data is run as the brain's user, with `runuser -u nova -- ` in front, and its files are edited that way too, e.g. `runuser -u nova -- nano /home/nova/.config/nova/.env` - root's own folders are never looked in. Only `systemctl`, `journalctl` and `tailscale` are root's. On a satellite, everything is run as its own user.

The places to look first are the brain's log, which says what Nova heard and decided at each step, and the satellite's, which says what it heard of the wake word and whether it can reach the brain:

```bash
journalctl -u nova-backend -n 100                 # on the brain, as root
```
```bash
journalctl --user-unit voice-assistant -n 100     # on the satellite
```

The brain's log is also kept in `~/.local/share/nova/logs/backend.log` (`runuser -u nova -- tail -n 100 /home/nova/.local/share/nova/logs/backend.log`), and the satellite's in its own `~/.local/share/nova/logs/smart_assistant.log`. On the satellite, the sound cards it can see are:

```bash
aplay -l && arecord -l
```

## Nova says it can't reach its server

The satellite says "Sorry, I can't reach my server right now" when it can't reach the brain - for the wake word, or when the brain goes part way through a request. Until it can, it still rings the timers and alarms from its own copy, and the wake word stops them.

- **The satellite's log says `Can't connect to the brain at nova (...)`** - check the brain is running (`systemctl status nova-backend`, on the brain, as root), that Tailscale passes port 10400 on to it (`tailscale serve status`, on the brain, as root - see the README for the `tailscale serve` command), and the brain's address in the satellite's `~/.config/nova/config.yaml` (`backend.address`, `nova:10400`).
- **The brain's log says `Refused a satellite: No satellite called ...`** - the brain doesn't know it. It's known by the satellite's machine's name, unless `backend.satellite` in the satellite's settings gives another: on the brain, `runuser -u nova -- venv/bin/python backend.py add-satellite NAME`, and put the password it prints in the satellite's `~/.config/nova/.env`, as `NOVA_BACKEND_TOKEN=...`. Then restart the satellite: `systemctl --user restart voice-assistant`.
- **The brain's log says the satellite `hung up part way through the handshake`** - most likely its password isn't the one the brain has for it. Make it a new one on the brain, with `remove-satellite NAME` and then `add-satellite NAME`, run as above, and put that in the satellite's `.env`.
- **The satellite won't start at all, and its log says it `needs its brain's address ... and its password`** - setup was left without the password. Add it as above.
- **A satellite removed on the brain** is let go straight away, and turned away from then on - `remove-satellite` is how one is shut out.

## Nova doesn't make a sound

On the satellite:

- **Check the speakers are on the right input.** Edifier speakers switch input with a press of the volume knob on the right-hand speaker, and the light changes colour with each one - this one cost an evening.
- **Check `/etc/asound.conf` names cards which exist.** It names the speaker card (`hw:CARD=...`), and if that speaker is swapped for another, nothing plays through the shared output any more. Compare the `CARD=` names with `aplay -l`, fix them, and restart the satellite. This happened when a HyperX speaker (`Sound`) was replaced by a USB-C adapter (`Audio`).
- **Play something directly**, to rule Nova out:

  ```bash
  aplay -D default sounds/listening.wav
  ```

## Nova's voice or the music crackles

On the satellite, which plays everything:

- **Look for `The speaker ran dry` in its log** - each time the speaker wasn't given audio quickly enough, counted and mentioned at most once a minute. If it keeps appearing, raise `OUTPUT_LATENCY` in `voice_assistant/speaker.py` by 0.02 (one of the shared output's 21ms periods) - stopping takes that much longer.
- **An `/etc/asound.conf` without `period_size` in it** is from before the short bursts, and works in 125ms ones - `update.sh` says so. Run `bash setup.sh` and let it write the file again, then restart the satellite and raspotify.
- `The microphone overflowed` is the same thing the other way round - some of what was said was lost.

## Nova doesn't hear me

- **Is the wake word being heard at all?** Look for `Wake word detected` in the satellite's log. If it never appears, raise your voice or lower `thresholds.wake_word_confidence` in the satellite's settings.
- **Was the microphone unplugged?** The satellite reopens it by itself - its log says `No audio from the microphone ... reopening it`, then `Microphone working again` - trying again every few seconds at first, then every 30. If it never comes back, check `arecord -l` and the capture card in `/etc/asound.conf`.
- **Listening for "Hey Jarvis" instead?** Then `models/hey_nova.onnx` is missing on the satellite - its log says so at startup.
- **From across the room, quietly** - "Hey Nova" may still be heard, then nothing (`No speech within 3.0s`, in the brain's log), or the wrong words. A small USB microphone only picks up so much; closer, or louder, is the fix.
- **Muted?** A satellite muted from Home Assistant or the settings page doesn't hear the wake word at all, and stays muted through restarts at either end.

## Nova doesn't hear "Hey Nova" over music

On the satellite, which takes what it plays back out of what it hears:

- **Is echo cancelling on?** Its log says `Echo cancelling on` at startup, and, once music has played for 10 to 20 seconds, `Echo cancelling: what's played is heard ...ms later`. Until that second line, the music isn't being taken out yet. Without the first, speexdsp is missing (`apt install libspeexdsp1`) or `audio.echo_cancellation` is off in its settings.
- **It only takes out what Nova plays itself** - Navidrome, its voice and timers. Spotify plays through raspotify, which Nova doesn't hear, so it can't be taken out.
- **It takes the music down, not away** - about 5 to 14dB on the Pi, more the longer it plays. Loud music right next to the microphone can still drown you out; turning it down a little helps a lot.

## Nova cuts me off, or keeps listening

The brain decides when you've stopped, so these are its settings, in its `~/.config/nova/config.yaml`:

- **Cut off mid-sentence** - raise `thresholds.silence_duration`, or lower `thresholds.vad_threshold`.
- **Keeps listening after I've finished** - usually because something in the room sounds like speech, such as a TV. Raise `thresholds.vad_threshold`.
- The brain's log shows `Speech detected` and `Silence detected after ...s` for each recording, and its `latency.jsonl`, in `~/.local/share/nova/logs`, has the timings. Whatever happens, the satellite stops sending after 10 seconds.

## Nova mishears, or answers something nobody said

- **The brain's log shows what was heard**, as `Transcription complete (...): '...'`. The known slips are allowed for - a lone "stop" heard as "soap", "sop" or "so" still stops things, and "skip" heard as "scap" still skips.
- **Words found in noise** - the obvious ones ("Thank you.", "Thanks for watching") are thrown away, logged as `Ignoring '...'`. If a false wake word is the cause, raise `thresholds.wake_word_confidence` in the satellite's settings.

## Music doesn't play

The brain plays Navidrome's music to the satellite, and drives Spotify, so these are all on the brain unless they say otherwise:

- **"The music isn't available right now"** - the music player didn't start. The brain's log says why, near the top: for Navidrome, `navidrome.url`, `navidrome.username` or `NAVIDROME_PASSWORD` is missing; for Spotify, its sign-in.
- **"I can't reach Navidrome right now"** - check Navidrome is running, and that its address in the brain's `~/.config/nova/config.yaml` opens from the brain: `curl -I http://navidrome.local:4533`.
- **"Navidrome didn't accept my password"** - check `NAVIDROME_PASSWORD` in the brain's `~/.config/nova/.env` against the Navidrome user named in `navidrome.username`.
- **A song is skipped straight away** - the brain's log says `Couldn't play '...'` with ffmpeg's reason. Usually a file Navidrome can't convert to MP3; check its transcoding settings.
- **"Spotify needs signing in again on the server"** - run the sign-in again, on the brain: `runuser -u nova -- venv/bin/python -m voice_assistant.spotify_controller`.
- **"I can't find the ... speaker"** - raspotify, on the satellite, isn't running, or its name doesn't match `spotify.device_name` in the brain's settings. Check `systemctl status raspotify` on the satellite. It's restarted every night anyway (`raspotify-restart.timer`), since its connection can drop silently.

## The lights or the calendar don't work

On the brain, which talks to Home Assistant for every satellite:

- **"Home Assistant isn't set up yet"** - `home_assistant.url` isn't set in the brain's `~/.config/nova/config.yaml`, or `HA_TOKEN` isn't in its `~/.config/nova/.env`. Its log says which at startup.
- **"I can't reach Home Assistant right now"** - check it's running and its address opens from the brain. With no network at all, it's said straight away.
- **"Home Assistant didn't accept my token"** - the token has been deleted or mistyped. Make a new one for Nova's Home Assistant user.
- **"Sorry, I am not aware of any area called ..."** - that's Home Assistant's own answer. Check the light is exposed to Assist (Settings > Voice assistants > Expose), and what it and its area are called there.
- **"The lights" means the wrong room** - each satellite's lights are those of the area its device is in, in Home Assistant. Check the satellite's Nova device is in the right one.
- **The calendar says nothing's on** - check the calendar shows events in Home Assistant itself, and, if `home_assistant.calendars` is set, that it's listed there.

## A timer or alarm didn't go off

- The brain keeps them, in `timers.json` and `alarms.json` in its `~/.local/share/nova`, and rings each on the satellite it was set from. Anything which came due while the brain was off is announced late, as "While I was off: ...", once its satellite is back - unless the satellite rang it itself meanwhile. It's kept in `timers.json` until then, however often the brain restarts, and said only once - though anything over an hour old as the brain starts is dropped.
- **While the brain can't be reached**, the satellite rings them from its copy (`alarm_copy.json` in its own `~/.local/share/nova`), which the brain sends it whenever anything changes, and every hour. A timer or alarm still ringing on the satellite when the brain is back carries on ringing from the brain, even if the brain went while it rang. The satellite rings it until the brain says it has it, so for a moment both may ring it - and rings it again if the brain goes once more while it rings. A satellite which restarts while ringing one from its copy carries on ringing it. One dismissed on the satellite meanwhile isn't rung again. The satellite's log says `Ringing ... from the satellite's copy`.
- Nothing goes off on the brain until its clock has been set from the internet, so a brain with no network at boot holds them until it has - its log says `System clock not set yet, holding timers`.

## Claude's replies fail

On the brain. The failure is said out loud: "took too long" (a slow connection), "can't reach the internet", an API key problem (check the brain's `~/.config/nova/.env`), or "too many requests" (wait a minute). The everyday commands are handled without Claude, and timers, alarms, Nova's own volume and the time keep working while the internet is down - as long as the satellite can reach the brain. With no network at all, the brain knows straight away from its routing table, and says so rather than waiting out its timeouts.

## The weather doesn't work

Set `weather.location` in the brain's `~/.config/nova/config.yaml` to your town or city, then restart it (`systemctl restart nova-backend`, as root).

## Nova won't start after a settings change

The log of whichever it was - the brain or the satellite - ends with what's wrong and where, e.g. `'thresholds.silence_duraton' isn't a setting. Did you mean 'silence_duration'?`. Fix it in that one's `~/.config/nova/config.yaml` and restart it. A file from before the brain heard for the satellites may still have `speech_to_text`, `moonshine` or `whisper` sections - they're no longer used, and only warned about, so they can be deleted.

## Nova restarted by itself

Both have a watchdog: if one stops getting on for two and a half minutes - stuck on something which should have timed out - systemd restarts it, rather than leaving it deaf. The log says `The main loop hasn't moved for ...s` just before, and systemd `Watchdog timeout` - or only the second, if the whole of it froze. Whatever it was doing then is the thing to look at.

- **On the brain**, that's most often one satellite's request, stuck part way - the lines before show how far it got. Most waits there give up by themselves instead: a recording Nemotron `hadn't finished` is said as "Sorry, something went wrong there", and a satellite which `never said a ... sound had finished` has that sound stopped and logged, with nothing said. While the brain restarts, which takes about 20 seconds, its satellites ring from their copies, and say they can't reach it.
- **On the satellite**, it's the microphone's loop.

## It's slow

On the brain, as its user:

```bash
runuser -u nova -- venv/bin/python scripts/latency_report.py
```

That shows how long each stage takes, typically and on a bad day, so you can see which one is to blame. Each satellite's own `latency.jsonl` has the whole wait, as the user hears it: for each wake word, how long until the first of the reply reached it.
