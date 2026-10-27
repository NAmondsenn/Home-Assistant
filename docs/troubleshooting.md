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
- **The brain's log says `Refused a satellite: No satellite called ...`** - the brain doesn't know it. It's known by the satellite's machine's name, unless `backend.satellite` in the satellite's settings gives another: add it by that name on the settings page, under Satellites (or on the brain, `runuser -u nova -- venv/bin/python backend.py add-satellite NAME`). Then, on the satellite, run `bash setup.sh --new-password` from its clone and type in the password it was given - it saves it and restarts the satellite.
- **The brain's log says the satellite `hung up part way through the handshake`** - most likely its password isn't the one the brain has for it. Give it a new one on the settings page, under Satellites > New password, then run `bash setup.sh --new-password` on the satellite and type it in. Don't use `remove-satellite` and then `add-satellite` for this: removing it moves its timers, alarms and routines to the default satellite, and empties its copy if it's connected, and adding it again doesn't bring them back.
- **A satellite is turned away after New password** - that's expected until it has the new one: the brain lets it go as the password changes, and its log says it `hung up part way through the handshake` from then on. Run `bash setup.sh --new-password` on it, as above. If it's still turned away, the password was cut short or mistyped as it was copied - `setup.sh` refuses one which is too short or has anything in it the page's passwords never have, but can't tell one wrong letter. Press New password again and type in the one it shows. On a Pi whose clone is from before `--new-password`, update it first (`bash update.sh`), or put the password after `NOVA_BACKEND_TOKEN=` in its `~/.config/nova/.env` and run `systemctl --user restart voice-assistant`.
- **The satellite won't start at all, and its log says it `needs its brain's address ... and its password`** - setup was left without the password. Give it one as above, with `bash setup.sh --new-password`.
- **A satellite removed on the brain** is let go straight away, and turned away from then on - Remove on the settings page, or `remove-satellite`, is how one is shut out. Its timers, alarms and routines go to the default satellite.

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
- The brain's log shows `Speech detected` and `Silence detected after ...s` for each recording, and its `latency.jsonl`, in `~/.local/share/nova/logs`, has the timings. Whatever happens, the satellite stops sending after the longest a request may be: `thresholds.recording_timeout`, Longest request on the settings page, 15 seconds unless changed, from 5 to 30. A satellite never sends more than 30 seconds, and one not yet updated stops at 10.

## Nova mishears, or answers something nobody said

- **The brain's log shows what was heard**, as `Transcription complete (...): '...'`. The known slips are allowed for - a lone "stop" heard as "soap", "sop" or "so" still stops things, and "skip" heard as "scap" still skips.
- **Words found in noise** - the obvious ones ("Thank you.", "Thanks for watching") are thrown away, logged as `Ignoring '...'`. If a false wake word is the cause, raise `thresholds.wake_word_confidence` in the satellite's settings.

## Music doesn't play

The brain plays Navidrome's music to the satellite, and drives Spotify, so these are all on the brain unless they say otherwise:

- **"The music isn't available right now"** - the music player didn't start. The brain's log says why, near the top: for Navidrome, `navidrome.url`, `navidrome.username` or `NAVIDROME_PASSWORD` is missing; for Spotify, its sign-in.
- **"I can't reach Navidrome right now"** - check Navidrome is running, and that its address in the brain's `~/.config/nova/config.yaml` opens from the brain: `curl -I http://navidrome.local:4533`.
- **"Navidrome didn't accept my password"** - check `NAVIDROME_PASSWORD` in the brain's `~/.config/nova/.env` against the Navidrome user named in `navidrome.username`.
- **A song is skipped straight away** - the brain's log says `Couldn't play '...'` with ffmpeg's reason. Usually a file Navidrome can't convert to MP3; check its transcoding settings.
- **"Spotify needs signing in again on the settings page"** - sign in again on the settings page, under Music - or, without the page, on the brain: `runuser -u nova -- venv/bin/python -m voice_assistant.spotify_controller`.
- **"I can't find the ... speaker"** - raspotify, on the satellite, isn't running, or its name doesn't match `spotify.device_name` in the brain's settings. Check `systemctl status raspotify` on the satellite. It's restarted every night anyway (`raspotify-restart.timer`), since its connection can drop silently.

## The lights don't work

On the brain, which talks to Home Assistant for every satellite:

- **"Home Assistant isn't set up yet"** - `home_assistant.url` isn't set in the brain's `~/.config/nova/config.yaml`, or `HA_TOKEN` isn't in its `~/.config/nova/.env`. Its log says which at startup.
- **"I can't reach Home Assistant right now"** - check it's running and its address opens from the brain. With no network at all, it's said straight away.
- **"Home Assistant didn't accept my token"** - the token has been deleted or mistyped. Make a new one for Nova's Home Assistant user.
- **"Sorry, I am not aware of any area called ..."** - that's Home Assistant's own answer. Check the light is exposed to Assist (Settings > Voice assistants > Expose), and what it and its area are called there.
- **"The lights" means the wrong room** - each satellite's lights are those of the area its device is in, in Home Assistant. Check the satellite's Nova device is in the right one.
### A switch, media player, script or scene doesn't work

Nova's own commands come first, so a request using one of their words - timer, alarm, music, volume, notifications, microphone and the like - goes to Claude, not to Assist. So does one with a word that points back ("it", "that", "the other one"), a delay ("in five minutes", "at six"), or two things joined with "and", "then" or "plus". Two things with only a comma between them do reach Assist, where what it would act on is still checked. Otherwise, the brain's log says what happened to it:

- **`Not sent to Assist, which is only given light sentences just now`** - Home Assistant's registry or Assist's sentence debugging isn't answering, so only light sentences go to Assist until both do. The log said `The wider rule is off, as ...` with the reason, and says `The wider rule is back on` once it is.
- **`Not sent to Assist, which reads it as ...`** - Assist matched nothing, or would act on something the check can't see, such as a script that isn't exposed. Check it's exposed (Settings > Voice assistants > Expose) - scripts aren't by default - and say it as it's called there.
- **`Refused a Home Assistant request ...`** or **`Refused a sensitive Home Assistant request`** - its words are for locking, unlocking or opening something, or it names, or would work, a lock, door, gate, window, valve, alarm panel, anything of Proxmox or one of the PC's HASS.Agent commands, or a script, scene or group that does - or something which acts that isn't in Home Assistant's registry. A new one is refused until the registry's next read, up to five minutes after it's added; one with no unique id - a YAML template or command_line switch, a scene with no id - is refused until it has one (`unique_id` in its YAML, or made in the UI). An old-style `group.*` can't take a unique id, so make it a group helper in the UI instead (Settings > Devices & services > Helpers > Group). The log line names the target as `not in Home Assistant's registry - give it a unique id to work it by voice` in both cases, so for one added in the last five minutes, wait for the registry's next read first. That's on purpose. A question about one, such as "is the garage door open?", goes to Claude instead.
- **`Can't see what ... does`** - Home Assistant can't show that script's contents, usually because it's written in YAML outside `scripts.yaml`, so it isn't run. Move it into `scripts.yaml`, or make it in the script editor. If it says the registry wouldn't say which script it is now, Home Assistant's WebSocket didn't answer in time, and it's tried again next time.

## The calendar doesn't work

On the brain, which reads the calendars from Google itself for every satellite - not through Home Assistant. See the README's "Google Calendar".

- **"The calendar isn't set up yet"** - `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` isn't in the brain's `~/.config/nova/.env` (or under Keys on the settings page), or the brain hasn't restarted since. Its log says `The calendar is off until ...` at startup, and the settings page's Calendars and lists says the keys are needed.
- **"I need signing in to Google again"**, or the settings page's Calendars and lists says Nova needs signing in to Google again - Nova hasn't signed in yet, or the sign-in was removed in your Google account, or the OAuth client was deleted or its secret made anew in Google Cloud. Sign in again on the settings page, under Calendars and lists. If the client changed, put its new id and secret under Keys first, and restart the brain. A client left in Testing rather than In production ends the sign-in after a week.
- **"I can't reach Google's calendar right now"** - the brain can't reach Google, or Google answered with an error. With no network at all, it's said straight away. The brain's log says `Couldn't read the calendar:` and the kind of failure, with no calendar's id.
- **The sign-in says Google didn't let Nova do something** - a box on Google's page was left unticked. Sign in again, with every box ticked.
- **The sign-in says Google refused it, with a code such as `redirect_uri_mismatch`** - the OAuth client in Google Cloud isn't set up as the README says: it needs to be of type Desktop app, with the Google Calendar API switched on. Fix it there, then start the sign-in again.
- **"I haven't got a calendar called ..."**, or an activity is said wrongly - each calendar but your own, Google's own (holidays, birthdays) and one named by an email address (a person's own, shared with you) is an activity, by its name: "when's gym tomorrow?" needs a calendar called Gym. Switch one on or off on the settings page, under Calendars and lists - it's used from the next question, with no restart. A calendar named like one of Nova's own things - Timer, Music - is Claude's to answer, not a local command.
- **The calendar says nothing's on** - check the event shows in Google Calendar itself, on a calendar in your account's list, and that its calendar is "In what's on" on the settings page. A calendar added there turns up within five minutes. `home_assistant.calendars` isn't used any more - the brain's log says so at startup, and it can be deleted.

## A battery warning didn't come

On the brain, which watches the batteries for every satellite:

- **Nothing is watched without Home Assistant** - "Battery levels need Home Assistant" means `home_assistant.url` or `HA_TOKEN` isn't set up on the brain - see "The lights don't work" above. Its log says `Home Assistant didn't answer, so batteries aren't being watched` once while it can't be reached, and `Home Assistant answers again` when it can.
- **Nova says it can't reach Home Assistant, but it's running** - batteries need an administrator's `HA_TOKEN`, as Home Assistant only answers templates for one. The brain's log says `Home Assistant won't tell Nova what's in which room`. Make Nova's user an administrator.
- **The device isn't there** - ask "battery levels". A device is only found if Home Assistant has a battery sensor for it which reads in percent, and it isn't unavailable.
- **It's only on the phone, or only said out loud** - a warning is said out loud only while you're home (Home Assistant's person) and outside `ntfy.quiet_from` to `ntfy.quiet_until`; otherwise it goes to `ntfy.phone`. The phone's own warning isn't sent to itself. With no ntfy, or a failed send, it's said instead - and held until the quiet hours end if it came in them, then said only if it's still true.
- **The device's warnings are off** - on the settings page's Batteries section, each device has a "Warn me" switch. Turned off, it warns about nothing, though "battery levels" still tells its level.
- **It wasn't sent to the phone** - each device also has "Send to my phone". It's off for the phone itself, which warns you on its own, and on for everything else. The settings page also has one switch for whether a warning that was said out loud is sent as well.
- **A mark already warned** - each mark warns once as the level falls. It starts again once the device has charged, or its level is 10 points above its highest mark. What has warned is kept in `batteries.json` in the brain's `~/.local/share/nova`.
- **Nothing is said out loud** - the default satellite (the first added, unless another is made the default on the settings page) has to be connected. The brain's log says `A battery warning wasn't said, as the default satellite isn't connected`.

## A timer or alarm didn't go off

- The brain keeps them, in `timers.json` and `alarms.json` in its `~/.local/share/nova`, and rings each on the satellite it was set from. Anything which came due while the brain was off is announced late, as "While I was off: ...", once its satellite is back - unless the satellite rang it itself meanwhile. It's kept in `timers.json` until then, however often the brain restarts, and said only once - though anything over an hour old as the brain starts is dropped.
- **While the brain can't be reached**, the satellite rings them from its copy (`alarm_copy.json` in its own `~/.local/share/nova`), which the brain sends it whenever anything changes, and every hour. A timer or alarm still ringing on the satellite when the brain is back carries on ringing from the brain, even if the brain went while it rang. The satellite rings it until the brain says it has it, so for a moment both may ring it. If the brain goes once more while it rings, the satellite rings it again - but only within ten minutes of when the brain began ringing it. After that, a brain which restarted says it once it's back, as "While I was off: ...". One which stayed up and only lost touch with the satellite lets it ring out, and keeps it for when you ask what you missed. A satellite which restarts while ringing one from its copy carries on ringing it. One dismissed on the satellite meanwhile isn't rung again. The satellite's log says `Ringing ... from the satellite's copy`.
- Nothing goes off on the brain until its clock has been set from the internet, so a brain with no network at boot holds them until it has - its log says `System clock not set yet, holding timers`.

## Claude's replies fail

On the brain. The failure is said out loud: "took too long" (a slow connection), "can't reach the internet", an API key problem (check the brain's `~/.config/nova/.env`), or "too many requests" (wait a minute). The everyday commands are handled without Claude, and timers, alarms, Nova's own volume and the time keep working while the internet is down - as long as the satellite can reach the brain. With no network at all, the brain knows straight away from its routing table, and says so rather than waiting out its timeouts.

## The weather doesn't work

The weather is Home Assistant's own - Met's forecast, for its home location - so it needs Home Assistant set up on the brain (`home_assistant.url`, and `HA_TOKEN` in its `.env`). Without it, Nova says "The weather needs Home Assistant."

- **"Home Assistant hasn't got a weather forecast."** It has no weather entity. Add the Met.no integration in Home Assistant (Settings > Devices & services), which forecasts for the home location set in Settings > System > General.
- **"Home Assistant's forecast isn't available right now."** The weather entity is unavailable - most often because Home Assistant can't reach Met, so check its internet. Home Assistant's log says why.
- **"That's Home Assistant's forecast from 5 hours ago."** Home Assistant last heard from Met over three hours ago. Its log says why it hasn't since.
- **The forecast is for somewhere else.** With more than one weather entity, Nova uses the first by entity id until you choose one: choose it on the settings page, under Calendars and lists ("Weather from").
- **The log warns `'weather.location' is no longer used`.** The weather no longer needs a place in Nova's settings: delete it from the brain's `~/.config/nova/config.yaml` - and the `weather:` line above it, though a `weather:` left on its own is only warned about too.

## The settings page at NPM's address doesn't open

See the README's "At an address of your own, through Nginx Proxy Manager". On the brain, the log says why a request was turned away (`journalctl -u nova-backend | grep "Settings page"`).

- **"Set a page password at the page's Tailscale address first."** No page password is set yet. Open the page at its tailnet address and set one under Keys, Page password (for NPM), then reload the NPM address.
- **"Too many wrong passwords - try again in a minute."** That device got the password wrong 5 times in a row, or the whole network 30 times in a minute, so tries are refused for a minute. If it says this on a device which hasn't been getting it wrong, and on every device at once, NPM may not be passing on `X-Real-IP` - it does by default, unless the proxy host's custom configuration takes it out - and every device is counted as NPM itself, sharing one count. A forgotten password is replaced at the tailnet address.
- **A bare 403 from the brain's port**, and the brain's log says `Settings page: turned away ..., which isn't NPM's` - the request didn't come from an address in `settings_page.proxy_from`. Put NPM's address there, as the brain sees it, in the brain's `~/.config/nova/config.yaml`, and restart Nova. Anything else on the network is meant to get this.
- **NPM says 502 Bad Gateway** - the brain isn't listening on that port: `settings_page.proxy_port` isn't set, `proxy_from` is empty (the log warns so at startup), or Nova hasn't restarted since they were set. Check NPM forwards to the brain's address and that port, with scheme `http`.
- **The login form keeps coming back** - the login ended: 30 days after it was made, Log out everywhere on any device, or a new page password. Log in again. A browser which blocks cookies can't stay logged in, and the page has to be opened over `https://`, as the login's cookie is only ever sent that way.

## Nova won't start after a settings change

The log of whichever it was - the brain or the satellite - ends with what's wrong and where, e.g. `'thresholds.silence_duraton' isn't a setting. Did you mean 'silence_duration'?`. Fix it in that one's `~/.config/nova/config.yaml` and restart it. A file from before the brain heard for the satellites may still have `speech_to_text`, `moonshine` or `whisper` sections - they're no longer used, and only warned about, so they can be deleted. So is a setting Nova no longer has, such as `weather.location`, with where it's set now.

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
