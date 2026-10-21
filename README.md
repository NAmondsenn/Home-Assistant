# Nova

A self-hosted voice assistant for the Raspberry Pi 5. Say "Hey Nova", ask for something, and it answers out loud - timers, reminders, alarms, music from your own Navidrome server or Spotify, the lights and your calendar through Home Assistant, the weather, and general questions through Claude. Everything except the questions themselves, the weather and Spotify stays on your own network.

It's being built in stages - see [docs/roadmap.md](docs/roadmap.md). The standalone Pi version comes first. Later, the heavy lifting moves to a home server, and the Pi becomes one of several satellites around the house.

## What it can do

- **Timers and reminders** - "set a timer for ten minutes", "remind me to take the pizza out in 20 minutes", "remind me at 4pm to call mum". Timers ring until they're answered, for up to ten minutes; reminders come back every half a minute for five. Both survive a restart.
- **Alarms** - "wake me up at 7 on weekdays". They ring in bursts, building up from quiet, with gaps for "Hey Nova, stop" to be heard in.
- **Music** - from your own [Navidrome](https://www.navidrome.org/) server, or from Spotify: play a song, artist, album, genre or playlist (or your liked songs), pause, skip, go back, shuffle, repeat, "what's this song?". The music pauses while Nova is talking, and comes back when it's done. Navidrome's plays through Nova itself, so it pauses instantly and keeps working with the internet down.
- **Volume** - for Nova's voice, for timers and alarms, and for the music, each kept separately.
- **The lights** - "turn off the kitchen lights", "set the living room lights to 30 percent", through Home Assistant.
- **Your calendar** - "what have I got on Thursday?", "am I busy this weekend?", from whichever calendars Home Assistant has, such as Google Calendar. "What have I got on this week?" is an overview - which days there's work or uni, and anything else by day - rather than every time of every day. "When's my MOT?" looks for one thing, up to three months ahead. And "what time do I have work tomorrow?", "when's my next shift?" or "when do I finish uni today?" - say what each is called on the calendar in `home_assistant.activities`.
- **The weather** - now, today or tomorrow, from Open-Meteo.
- **Anything else** - answered by Claude, which can search the web when you ask it to ("search for...", "look up...").
- **Follow-ups** - when Nova asks you something, just answer; there's no need to say "Hey Nova" again.
- **Talking over it** - "Hey Nova" is heard over the music Nova plays and over its own replies, which stop the moment it's said. "Hey Nova, stop" stops whatever it's saying or playing.
- **Remembering you** - mention something about yourself ("I've started a new job at Aldi", "my sister's called Amy") and Nova keeps it, says "I'll remember that", and uses it from then on. "What do you know about me?" reads it back; "forget that..." or "forget everything you know about me" takes it away. It's kept in `~/.local/share/nova/memory.json`, and goes to Claude with each request so it can use it.
- **Notifications** - with your own [ntfy](https://ntfy.sh/) server, alerts posted to the topics you choose - your homelab's, say - are read out as they arrive, and "tell me more" reads all of one. At night only the most urgent are; "any notifications?" has the rest. "Send that to my phone" sends Nova's last answer there, as do a reminder nobody answered and Nova restarting after a problem.
- **A settings page** - how Nova's doing, its volumes, timers and alarms, what it remembers, its keys and its main settings, from your phone or laptop - over your tailnet, and only for you.

The everyday commands - timers, pause, skip, volume, the time, the weather, the lights and the calendar - are recognised on the Pi itself, without asking Claude, so they're quicker. With the internet down, timers, alarms, Nova's own volume, the time, the lights and Navidrome carry on working; anything which needs Spotify, the forecast or Claude says straight away that it can't reach the internet, rather than going quiet.

## How it works

```
microphone ─► wake word ─► record until you stop talking ─► Moonshine ─► local command, or Claude ─► action ─► Piper ─► speaker
               (openWakeWord)   (Silero voice activity)       (or Whisper)                         (timers, music, ...)
```

- `main.py` runs the loop above, and decides how each request is answered and confirmed.
- `voice_assistant/` holds everything else, one module per job: `microphone.py` and `speaker.py` own the audio devices, `wake_word.py`, `vad.py`, `speech_to_text.py` and `text_to_speech.py` handle speech, `intents.py` the commands handled locally, `llm.py` Claude, `actions.py` everything Nova can do - each tool Claude is offered, declared beside the code which carries it out - and `clock.py`, `music.py` with `navidrome.py` and `spotify_controller.py`, `home_assistant.py`, `volume.py` and `weather.py` the features themselves.
- The microphone is opened once and never closed, keeping the last few seconds of audio, so nothing said straight after "Hey Nova" is lost. Moonshine hears the recording as it's made, so it's mostly worked out the words by the time you stop talking. The speaker is opened once too, and everything that plays goes through it, so a timer or alarm can be stopped the moment you speak.

The reasons behind the bigger choices are in [docs/decisions/](docs/decisions/).

## Installing on a Raspberry Pi

You'll need a Raspberry Pi 5 with Raspberry Pi OS (64-bit), a USB microphone, a speaker, and an [Anthropic API key](https://console.anthropic.com/). For Spotify, you'll also need Spotify Premium and a [Spotify app](https://developer.spotify.com/dashboard) for its client ID and secret, with `http://127.0.0.1:8888/callback` as its redirect URI.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh
```

`setup.sh` asks a few questions as it goes, and is safe to run again - anything already set up is left alone, and each step says whether it was done (✓), skipped (–) or needs you (✗). In order, it:

1. Checks this is Raspberry Pi OS or Debian, with Python 3.11 or newer, a speaker and a microphone.
2. Installs the system packages, makes a Python environment in `venv/`, and installs the Python packages - the exact versions in `requirements.lock`.
3. Moves anything an older install kept in the repository folder to its new place (see Configuring).
4. Asks for the assistant's name, wake word and Spotify speaker name, saving any you change to `~/.config/nova/config.yaml`.
5. Asks for your API keys and writes them to `~/.config/nova/.env`, readable only by you. An existing one is left as it is.
6. Finds your wake word model (see below) and copies it into `models/`.
7. Downloads Piper (the voice), the Moonshine and Whisper models and openWakeWord's own models into `models/`. Piper, its voice and raspotify's key are checked against pinned SHA-256 hashes, so a tampered download stops setup rather than being run.
8. Offers to write `/etc/asound.conf`, so the assistant and Spotify can share the speaker and microphone - in short bursts, so a sound stops quickly and the wake word reacts sooner - backing up the old one.
9. Offers to install raspotify, so the Pi shows up as a Spotify Connect speaker.
10. Sets the assistant to start at boot, as a user service, and starts it.

`bash setup.sh --yes` asks nothing, taking what it finds and the defaults; `--no-spotify` leaves raspotify out, and `--no-service` doesn't start Nova at boot.

To update to the latest code later - it never touches your settings, keys or saved data:

```bash
bash update.sh
```

and to take Nova off the Pi, asking before removing anything which can't be got back:

```bash
bash uninstall.sh
```

### Music

Navidrome or Spotify - `music.player` in `config.yaml` says which, and it's Spotify unless you change it.

**Navidrome.** Make a Navidrome user for Nova (or use your own), then on the Pi add its password to `~/.config/nova/.env` yourself:

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

Songs are fetched as MP3 - Navidrome converts anything else as it sends it - and played through ffmpeg, which `setup.sh` installs. Your liked songs are the ones starred in Navidrome. Nova never sends the password itself, only a salted hash of it, as Navidrome's API has it.

**Spotify.** Spotify Premium is needed. Sign in once - it prints a link to open in a browser:

```bash
venv/bin/python -m voice_assistant.spotify_controller
```

### Home Assistant, for the lights and the calendar

Nova asks Home Assistant for things in plain words, through its own Assist, so it can only reach the devices you've exposed to Assist - nothing else in Home Assistant. Whatever's exposed, it never locks, unlocks or opens anything by voice.

1. In Home Assistant, make a user for Nova - Settings > People > Add person, allowed to log in, **not** an administrator.
2. Log in as that user, and under its profile > Security, make a long-lived access token.
3. On the Pi, add it to `~/.config/nova/.env` yourself - it's a password, so don't paste it anywhere else:

   ```
   HA_TOKEN=the-token
   ```
4. Put Home Assistant's address in `~/.config/nova/config.yaml`:

   ```yaml
   home_assistant:
     url: "http://homeassistant.local:8123"
   ```
5. In Settings > Voice assistants > Expose, choose which lights Nova may control. Their names and areas are what you'll call them.
6. For your calendar, add Google Calendar (or any other) to Home Assistant through its integration. Every calendar it has is read, unless you list the ones you want in `home_assistant.calendars`.

Then restart Nova. The log says `Home Assistant at ...` once it's on.

### ntfy, for alerts and your phone

1. On your ntfy server, make Nova a user with only the access it needs, and a token for it - here with a `homelab` topic for alerts, and `clipboard` and `reminders` for your phone:

   ```bash
   ntfy user add nova
   ntfy access nova homelab rw
   ntfy access nova clipboard wo
   ntfy access nova reminders wo
   ntfy token add nova
   ```
2. On the Pi, add the token to `~/.config/nova/.env` yourself:

   ```
   NTFY_TOKEN=the-token
   ```
3. Say which topic is which in `~/.config/nova/config.yaml`:

   ```yaml
   ntfy:
     url: "https://ntfy.example.com"
     alerts: ["homelab"]
     phone: "clipboard"
     reminders: "reminders"
     restarts: "homelab"
   ```

Then restart Nova. What it sends is tagged `nova`, so it never reads its own messages back out.

### The settings page

Nova serves a page for looking after it from a phone or a laptop - how it's doing, its volumes, timers and alarms, what it remembers, its keys (which it only ever saves, never shows back) and its main settings. The star by Nova's name shows whether it's waiting, listening, thinking or speaking, and as a volume's dragged, Nova beeps on its speaker at each new level, so you can hear how loud it is. It listens on the Pi alone, and only your own Tailscale login gets in:

1. In Tailscale's admin page, turn on HTTPS certificates - under DNS.
2. On the Pi, have Tailscale pass the tailnet's requests on to the page, with a certificate for the Pi's name:

   ```bash
   sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```
3. Say who's let in, in `~/.config/nova/config.yaml`:

   ```yaml
   settings_page:
     allow: ["you@example.com"]
   ```

Then restart Nova, and open `https://` and the Pi's name on your tailnet - `tailscale status` shows it - on any of your devices. A change has to come from the page itself, so another website can't make one in your name; settings are saved to `~/.config/nova/config.yaml`, the one before kept as `config.yaml.bak`.

### The wake word model

"Hey Nova" is a custom [openWakeWord](https://github.com/dscripka/openWakeWord) model, `hey_nova.onnx`, and it isn't in this repository - keep a copy somewhere safe, and copy it onto the Pi before running `setup.sh`. Without it, Nova still starts, but listens for openWakeWord's stock "Hey Jarvis" until `models/hey_nova.onnx` is there.

### Exact package versions

`requirements.txt` says what's needed; `requirements.lock` says what's known to work on the Pi. To install exactly those (Python 3.13 only):

```bash
venv/bin/pip install --no-deps -r requirements.lock
```

## Configuring

The repository's `config.yaml` has every setting, with its default and a comment saying what it's for - the wake word, how long to wait for you to talk, timer and alarm behaviour, the music player, and more. Don't change it there: put just the settings you want different in **`~/.config/nova/config.yaml`**, in the same sections, and they're used on top:

```yaml
weather:
  location: "Leeds"
music:
  player: "navidrome"
```

That file is this install's own, outside the repository, so an update never touches it and your settings never end up on GitHub. It's checked as Nova starts: a misspelt setting, or one of the wrong kind, stops it with a message naming it - `journalctl --user-unit voice-assistant` shows it - rather than being quietly ignored.

Keys and passwords go in **`~/.config/nova/.env`**, readable only by you, never in a settings file. Everything Nova saves - timers, alarms, volume levels, what it remembers about you, the Spotify sign-in and its logs - is in **`~/.local/share/nova/`**. An install from before this layout has its files moved there the first time it starts.

The settings most likely to need changing:

| Setting | What it's for |
| --- | --- |
| `weather.location` | Your town or city. Empty until you set it. |
| `music.player` | `navidrome` or `spotify`. |
| `speech_to_text.engine` | `moonshine` (quicker) or `whisper`. |
| `thresholds.vad_threshold` | How sure Nova must be that it's hearing speech. Raise it if noise keeps it listening; lower it if quiet speech is missed. |
| `thresholds.silence_duration` | How long a pause ends what you're saying. |
| `thresholds.wake_word_confidence` | How sure the wake word model must be. Raise it if Nova wakes by itself. |
| `logging.log_transcripts` | Whether what's said is kept in the logs. |
| `ntfy.quiet_from`, `ntfy.quiet_until` | The night, when only the most urgent alerts are read out. |

## Running it

As a service (the usual way, once `setup.sh` has installed it):

```bash
systemctl --user restart voice-assistant
```
```bash
journalctl --user-unit voice-assistant -f
```

Or by hand, to watch what it's doing:

```bash
venv/bin/python main.py
```

Logs are in `~/.local/share/nova/logs/smart_assistant.log`, trimmed at 5 MB. How long each stage of every interaction takes is kept in `latency.jsonl` beside it, and summarised by:

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
