# Nova

A self-hosted voice assistant, with a Raspberry Pi in each room to talk to. Say "Hey Nova", ask for something, and it answers out loud - timers, reminders, alarms, music from your own Navidrome server or Spotify, the lights and your calendar through Home Assistant, the weather, and general questions through Claude. Everything except the questions themselves, the weather and Spotify stays on your own network.

It's built in two halves - see [docs/decisions/0006](docs/decisions/0006-the-pi-as-a-satellite.md) - and in stages, in [docs/roadmap.md](docs/roadmap.md). The **brain** runs on a home server (a container on PVE2) and does everything which thinks: speech-to-text, Claude, the voice, music, Home Assistant, timers, memory and the settings page. A **satellite** is a Raspberry Pi in a room, which only hears and speaks: its microphone, speaker and wake word. One brain looks after any number of satellites.

## What it can do

- **Timers and reminders** - "set a timer for ten minutes", "remind me to take the pizza out in 20 minutes", "remind me at 4pm to call mum". Timers ring until they're answered, for up to ten minutes; reminders come back every half a minute for five. Both survive a restart.
- **Alarms** - "wake me up at 7 on weekdays". They ring in bursts, building up from quiet, with gaps for "Hey Nova, stop" to be heard in.
- **Music** - from your own [Navidrome](https://www.navidrome.org/) server, or from Spotify: play a song, artist, album, genre or playlist (or your liked songs), pause, skip, go back, shuffle, repeat, "what's this song?". The music pauses while Nova is talking, and comes back when it's done. Navidrome's plays through Nova itself, so it pauses instantly and keeps working with the internet down.
- **Volume** - for Nova's voice, for timers and alarms, and for the music, each kept separately.
- **The lights** - "turn off the kitchen lights", "set the living room lights to 30 percent", "make the lamp blue", "make the lights warmer", through Home Assistant. Short commands like "lights on" mean the lights in the room Nova is in.
- **In Home Assistant** - Nova is a device there, with its volumes, next alarm, timers, stop, restart, a box to make it say something, and a switch to mute its microphone. See "Nova in Home Assistant" below.
- **Your calendar** - "what have I got on Thursday?", "am I busy this weekend?", from whichever calendars Home Assistant has, such as Google Calendar. "What have I got on this week?" is an overview - which days there's work or uni, and anything else by day - rather than every time of every day. "When's my MOT?" looks for one thing, up to three months ahead. And "what time do I have work tomorrow?", "when's my next shift?" or "when do I finish uni today?" - say what each is called on the calendar in `home_assistant.activities`.
- **The weather** - now, today or tomorrow, from Open-Meteo.
- **Anything else** - answered by Claude, which can search the web when you ask it to ("search for...", "look up...").
- **Follow-ups** - when Nova asks you something, just answer; there's no need to say "Hey Nova" again.
- **Talking over it** - "Hey Nova" is heard over the music Nova plays and over its own replies, which stop the moment it's said. "Hey Nova, stop" stops whatever it's saying or playing.
- **Remembering you** - mention something about yourself ("I've started a new job at Aldi", "my sister's called Amy") and Nova keeps it, says "I'll remember that", and uses it from then on. "What do you know about me?" reads it back; "forget that..." or "forget everything you know about me" takes it away. It's kept in `~/.local/share/nova/memory.json`, and goes to Claude with each request so it can use it.
- **Notifications** - with your own [ntfy](https://ntfy.sh/) server, alerts posted to the topics you choose - your homelab's, say - are read out as they arrive, and "tell me more" reads all of one. At night only the most urgent are; "any notifications?" has the rest. "Send that to my phone" sends Nova's last answer there, as do a reminder nobody answered and Nova restarting after a problem.
- **Routines** - several things at once from a few words of your choosing ("Hey Nova, good morning"), or by themselves at a set time: each step is something you'd say to Nova - "what's the weather", "turn on the kitchen lights", "play my morning playlist" - done just as if you had, so a routine can do anything Nova can. Made and run on the settings page, and kept in `~/.local/share/nova/routines.json`.
- **A settings page** - how Nova's doing, what's playing, your lights and Home Assistant's scripts and scenes, its volumes, timers and alarms, what it remembers, its keys and its main settings, from your phone or laptop - over your tailnet, and only for you.

The everyday commands - timers, pause, skip, volume, the time, the weather, the lights and the calendar - are recognised on the brain without asking Claude, so they're quicker. With the internet down, the brain still handles timers, alarms, the time, the lights and Navidrome; anything which needs Spotify, the forecast or Claude says straight away that it can't reach the internet, rather than going quiet. With the brain unreachable, a satellite can't understand anything, and says so - but its timers and alarms, which it keeps a copy of, still ring, and "stop" still stops them.

## How it works

```
 on the satellite                                           over the tailnet                on the brain
microphone ─► wake word ─► record until you stop talking ─────────────────────► Nemotron ─► local command, or Claude ─► action ─► Piper ─► speaker
               (openWakeWord)   (Silero voice activity)    <───── the voice ──────  (hears as you speak)                (timers, music, ...)
```

- `satellite.py` runs the satellite: it listens, sends the recording to the brain as it's made, and plays what comes back.
- `backend.py` runs the brain: it hears each satellite's recording, works out the answer, and sends the speech back. Each satellite connects with a password of its own.
- `voice_assistant/` holds everything else, one module per job: `microphone.py` and `speaker.py` own the audio devices, `wake_word.py`, `vad.py`, `speech_to_text.py` and `text_to_speech.py` handle speech, `intents.py` the commands handled locally, `llm.py` Claude, `actions.py` everything Nova can do - each tool Claude is offered, declared beside the code which carries it out - and `clock.py`, `music.py` with `navidrome.py` and `spotify_controller.py`, `home_assistant.py`, `volume.py` and `weather.py` the features themselves.
- The microphone is opened once and never closed, keeping the last few seconds of audio, so nothing said straight after "Hey Nova" is lost. Nemotron on the brain hears the recording as it's made, so it's mostly worked out the words by the time you stop talking. The speaker is opened once too, and everything that plays goes through it, so a timer or alarm can be stopped the moment you speak.

The reasons behind the bigger choices are in [docs/decisions/](docs/decisions/).

## Installing

Nova is installed in two roles, from the same clone of the repository. Set the brain up first, then each satellite. Both are done with `setup.sh`, which asks a few questions as it goes and is safe to run again - anything already set up is left alone, and each step says whether it was done (✓), skipped (–) or needs you (✗). The role is kept in `~/.config/nova/role`, so `update.sh` and a later `setup.sh` know which it is.

### The brain

The brain is a container (or any machine) running Debian, with Python 3.13 and about 2GB of memory, on your tailnet - Nova's is container 107, `nova`, on PVE2. It needs an [Anthropic API key](https://console.anthropic.com/) and, for Spotify, Spotify Premium and a [Spotify app](https://developer.spotify.com/dashboard) for its client ID and secret, with `http://127.0.0.1:8888/callback` as its redirect URI. It has no microphone or speaker, and nothing outside your tailnet can reach it.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh --brain
```

In order, it:

1. Checks this is Debian with Python 3.11 or newer.
2. Installs the system packages (ffmpeg among them), makes a Python environment in `venv/`, and installs the Python packages - the exact versions in `requirements-backend.txt`.
3. Moves anything an older install kept in the repository folder to its new place (see Configuring).
4. Asks for the assistant's name, wake word and Spotify speaker name, saving any you change to `~/.config/nova/config.yaml`.
5. Asks for your API keys and writes them to `~/.config/nova/.env`, readable only by you. An existing one is left as it is.
6. Downloads Piper, the voice (`en_US-hfc_female-medium`), the Nemotron speech-to-text model and openWakeWord's own models into `models/`. They're checked against pinned SHA-256 hashes, so a tampered download stops setup rather than being run. Nemotron needs about 1.5GB free: `models/` can be a link to a folder somewhere with more room, and everything follows it.
7. Installs the brain as the `nova-backend` service, which runs as your user and starts at boot, and starts it.

Then let each satellite in, which prints a password for it - once, so keep it for the satellite's setup:

```bash
venv/bin/python backend.py add-satellite kitchen
```

`remove-satellite NAME` stops letting one in. Put the brain on your tailnet with `tailscale serve`, and it's reached by its name and port 10400, as `nova:10400`. The brain's own settings and keys, and everything it saves, are in `~/.config/nova` and `~/.local/share/nova` on its machine, and are where the rest of this README says "the brain".

The brain's log is `journalctl -u nova-backend -f`, and `sudo systemctl restart nova-backend` restarts it. To update it, run `bash update.sh` there.

### A satellite

You'll need a Raspberry Pi with Raspberry Pi OS (64-bit), a USB microphone and a speaker, on the same tailnet as the brain, and the password `add-satellite` gave for it.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh
```

In order, it:

1. Checks this is Raspberry Pi OS or Debian, with Python 3.11 or newer, a speaker and a microphone.
2. Installs the system packages, makes a Python environment in `venv/`, and installs the Python packages - the exact versions in `requirements.lock`, which is only what the microphone, speaker and wake word need.
3. Moves anything an older install kept in the repository folder to its new place (see Configuring).
4. Asks for the assistant's name, wake word, Spotify speaker name and the brain's address (`nova:10400`), saving them to `~/.config/nova/config.yaml`.
5. Asks for the satellite's password and writes it to `~/.config/nova/.env` as `NOVA_BACKEND_TOKEN`, readable only by you. It's the only secret a satellite holds.
6. Finds your wake word model (see below) and copies it into `models/`, and downloads openWakeWord's own models there. A satellite downloads no speech or voice models - the brain does the hearing and the speaking.
7. Offers to write `/etc/asound.conf`, so the assistant and Spotify can share the speaker and microphone - in short bursts, so a sound stops quickly and the wake word reacts sooner - backing up the old one.
8. Offers to install raspotify, so the Pi shows up as a Spotify Connect speaker.
9. Sets the satellite to start at boot, as the `voice-assistant` user service, and starts it.

Each satellite is named by its own machine's name, which the brain knows its password by - the same name given to `add-satellite`. Set `backend.satellite` in `config.yaml` to use another.

`bash setup.sh --yes` asks nothing, taking what it finds and the defaults; `--no-spotify` leaves Spotify out, and `--no-service` doesn't start Nova at boot. `--brain` and `--satellite` choose the role; without one, an existing install keeps its own.

To update to the latest code later - it never touches your settings, keys or saved data - run this on the brain and on each satellite:

```bash
bash update.sh
```

and to take a satellite off the Pi, asking before removing anything which can't be got back:

```bash
bash uninstall.sh
```

### Music

Navidrome or Spotify - `music.player` in `config.yaml` says which, and it's Spotify unless you change it. Both are set up on the brain, which plays the music through each satellite's speaker.

**Navidrome.** Make a Navidrome user for Nova (or use your own), then on the brain add its password to `~/.config/nova/.env` yourself:

```
NAVIDROME_PASSWORD=the-password
```

and point Nova at it in `~/.config/nova/config.yaml`:

```yaml
music:
  player: "navidrome"
navidrome:
  url: "http://navidrome.local:4533"
  username: "nova"
```

Songs are fetched as MP3 - Navidrome converts anything else as it sends it - and decoded with ffmpeg on the brain, which `setup.sh --brain` installs. Your liked songs are the ones starred in Navidrome. Nova never sends the password itself, only a salted hash of it, as Navidrome's API has it.

**Spotify.** Spotify Premium is needed. Sign in once, on the brain - it prints a link to open in a browser:

```bash
venv/bin/python -m voice_assistant.spotify_controller
```

### Home Assistant, for the lights and the calendar

This is set up on the brain, which is what talks to Home Assistant - a satellite holds no token. Nova asks Home Assistant for things in plain words, through its own Assist, which can only reach the devices you've exposed to it. What Assist doesn't understand - "dim the lights" - goes to Claude, which can switch and set any light, exposed or not, but nothing else. Whatever's exposed, Nova never locks, unlocks or opens anything by voice.

1. In Home Assistant, make a user for Nova - Settings > People > Add person, allowed to log in. Knowing Nova's room, and Claude working the lights, need it to be an administrator, as Home Assistant only lets administrators ask what's in which room. As an ordinary user, Nova still works through Assist, without rooms.
2. Log in as that user, and under its profile > Security, make a long-lived access token.
3. On the brain, add it to `~/.config/nova/.env` yourself - it's a password, so don't paste it anywhere else:

   ```
   HA_TOKEN=the-token
   ```
4. Put Home Assistant's address in `~/.config/nova/config.yaml`:

   ```yaml
   home_assistant:
     url: "http://homeassistant.local:8123"
   ```
5. In Settings > Voice assistants > Expose, choose which lights Assist may control. Their names and areas are what you'll call them. Claude isn't limited to these - it can work any light.
6. For your calendar, add Google Calendar (or any other) to Home Assistant through its integration. Every calendar it has is read, unless you list the ones you want in `home_assistant.calendars`.

Then restart the brain. Its log says `Home Assistant at ...` once it's on.

### Nova in Home Assistant

Nova can also be a device of its own in Home Assistant, set up on the brain, through its MQTT broker (the Mosquitto add-on), so you can see and change things from Home Assistant's dashboards and automations. It has eleven controls: Status, Volume, Alarm volume, Music volume, Next alarm, Timers, Stop, Restart, Speak, Backend and Microphone muted. Nothing said to Nova, and none of its answers, is sent there - timers show only when they end. Each satellite is a device of its own. Without this, Nova works as it always did, and it carries on if the broker goes down.

1. In Home Assistant, make another user for Nova - Settings > People > Users > Add user, called `nova`, **not** an administrator, and with "Can only log in from the local network" on. The Users tab is only there with Advanced mode on in your profile. The broker accepts Home Assistant's users.
2. On the brain, add its password to `~/.config/nova/.env` yourself:

   ```
   MQTT_PASSWORD=the-password
   ```
3. Say where the broker is in the brain's `~/.config/nova/config.yaml`. The host is Home Assistant's own address on your home network, not its web address:

   ```yaml
   mqtt:
     host: "192.168.1.20"
     username: "nova"
   ```
4. Restart the brain, and the device appears in Home Assistant. Put it in an area (its page > the pencil > Area): with an administrator's `HA_TOKEN`, that's how Nova knows what room it's in, so "lights on" means that room's lights. It looks every half a minute until Home Assistant has the device, then every five minutes, so giving it an area or moving it takes a few minutes to be noticed.

The microphone switch works from Home Assistant or the settings page, and stays as it was through a restart. While it's muted, Nova doesn't hear "Hey Nova", but what you send it with Speak is still said.

### ntfy, for alerts and your phone

1. On your ntfy server, make Nova a user with only the access it needs, and a token for it - here with a `homelab` topic for alerts, and `clipboard` and `reminders` for your phone:

   ```bash
   ntfy user add nova
   ntfy access nova homelab rw
   ntfy access nova clipboard wo
   ntfy access nova reminders wo
   ntfy token add nova
   ```
2. On the brain, add the token to `~/.config/nova/.env` yourself:

   ```
   NTFY_TOKEN=the-token
   ```
3. Say which topic is which in the brain's `~/.config/nova/config.yaml`:

   ```yaml
   ntfy:
     url: "https://ntfy.example.com"
     alerts: ["homelab"]
     phone: "clipboard"
     reminders: "reminders"
     restarts: "homelab"
   ```

Then restart the brain. What it sends is tagged `nova`, so it never reads its own messages back out.

### The settings page

Nova serves a page for looking after it from a phone or a laptop - how it's doing, its volumes, timers and alarms, what it remembers, its keys (which it only ever saves, never shows back) and its main settings. The star by Nova's name shows whether it's waiting, listening, thinking or speaking, and as a volume's dragged, Nova beeps on its speaker at each new level, so you can hear how loud it is. With music set up, it shows what's playing, and plays something else; with Home Assistant, it switches and dims your lights, sets their colour and warmth, groups them by room, and runs your scripts and scenes - never one whose name says it locks or opens anything. It's on the brain, not the satellites - one page for the whole house - and listens there alone, and only your own Tailscale login gets in:

1. In Tailscale's admin page, turn on HTTPS certificates - under DNS.
2. On the brain, have Tailscale pass the tailnet's requests on to the page, with a certificate for the brain's name:

   ```bash
   sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```
3. Say who's let in, in the brain's `~/.config/nova/config.yaml`:

   ```yaml
   settings_page:
     allow: ["you@example.com"]
   ```

Then restart the brain, and open `https://` and its name on your tailnet - `tailscale status` shows it - on any of your devices. A change has to come from the page itself, so another website can't make one in your name; settings are saved to the brain's `~/.config/nova/config.yaml`, the one before kept as `config.yaml.bak`. A satellite's own settings - the brain's address, and its password - are changed on the satellite, by hand.

### The wake word model

"Hey Nova" is a custom [openWakeWord](https://github.com/dscripka/openWakeWord) model, `hey_nova.onnx`, and it isn't in this repository - keep a copy somewhere safe, and copy it onto the satellite before running `setup.sh`. Only satellites listen for it. Without it, Nova still starts, but listens for openWakeWord's stock "Hey Jarvis" until `models/hey_nova.onnx` is there.

### Exact package versions

`requirements.txt` says what a satellite needs; `requirements.lock` says what's known to work on the Pi; and `requirements-backend.txt` is the brain's, all of them exact. To install exactly those (Python 3.13 only):

```bash
venv/bin/pip install --no-deps -r requirements.lock           # a satellite
venv/bin/pip install --no-deps -r requirements-backend.txt    # the brain
```

## Configuring

The repository's `config.yaml` has every setting, with its default and a comment saying what it's for - the wake word, how long to wait for you to talk, timer and alarm behaviour, the music player, and more. Don't change it there: put just the settings you want different in **`~/.config/nova/config.yaml`**, in the same sections, and they're used on top:

```yaml
weather:
  location: "Leeds"
music:
  player: "navidrome"
```

That file is this install's own, outside the repository, so an update never touches it and your settings never end up on GitHub. It's checked as Nova starts: a misspelt setting, or one of the wrong kind, stops it with a message naming it - `journalctl -u nova-backend` shows it on the brain, `journalctl --user-unit voice-assistant` on a satellite - rather than being quietly ignored. Almost everything is set on the brain; a satellite's file has little beyond its brain's address, its name and the wake word.

Keys and passwords go in **`~/.config/nova/.env`**, readable only by you, never in a settings file. Everything Nova saves - timers, alarms, volume levels, what it remembers about you, the Spotify sign-in and its logs - is in **`~/.local/share/nova/`**. An install from before this layout has its files moved there the first time it starts.

The settings most likely to need changing:

| Setting | What it's for |
| --- | --- |
| `weather.location` | Your town or city. Empty until you set it. |
| `music.player` | `navidrome` or `spotify`. |
| `backend.address` | On a satellite, its brain, as `name:port`. |
| `thresholds.vad_threshold` | How sure Nova must be that it's hearing speech. Raise it if noise keeps it listening; lower it if quiet speech is missed. |
| `thresholds.silence_duration` | How long a pause ends what you're saying. |
| `thresholds.wake_word_confidence` | How sure the wake word model must be. Raise it if Nova wakes by itself. |
| `logging.log_transcripts` | Whether what's said is kept in the logs. |
| `ntfy.quiet_from`, `ntfy.quiet_until` | The night, when only the most urgent alerts are read out. |

## Running it

As a service (the usual way, once `setup.sh` has installed it). On a satellite:

```bash
systemctl --user restart voice-assistant
```
```bash
journalctl --user-unit voice-assistant -f
```

and on the brain:

```bash
sudo systemctl restart nova-backend
```
```bash
journalctl -u nova-backend -f
```

Or by hand, to watch what it's doing - `satellite.py` on a satellite, `backend.py` on the brain:

```bash
venv/bin/python satellite.py
```

Logs are in `~/.local/share/nova/logs/` - `smart_assistant.log` on a satellite and `backend.log` on the brain - trimmed at 5 MB. How long each stage of every interaction takes is kept in `latency.jsonl` beside it, and summarised by:

```bash
venv/bin/python scripts/latency_report.py
```

If something isn't working, [docs/troubleshooting.md](docs/troubleshooting.md) goes through it by symptom.

## Developing

The tests run anywhere - on a PC without a microphone, speaker or any of the heavy packages - since `tests/conftest.py` stands in for whatever isn't installed:

```bash
pip install pytest ruff numpy soxr soundfile pyyaml requests
```
```bash
python -m pytest
```
```bash
ruff check .
```

Both also run on GitHub for every push (`.github/workflows/checks.yml`), along with a weekly check of `requirements.lock` for packages with known security problems. The scripts in `scripts/manual/` need real hardware or accounts, so they're run by hand, and `scripts/soak_monitor.py` keeps an eye on a long-running assistant - see [docs/release-checklist.md](docs/release-checklist.md) for when to use them. Before changing the speech-to-text model, record yourself with `scripts/record_samples.py`, and measure the models on those recordings with `scripts/compare_speech_to_text.py`.

When picking work back up after a break, start with [docs/roadmap.md](docs/roadmap.md).
