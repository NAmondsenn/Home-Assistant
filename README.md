# Nova

A self-hosted voice assistant for the Raspberry Pi 5. Say "Hey Nova", ask for something, and it answers out loud - timers, reminders, alarms, Spotify, the weather, and general questions through Claude. Everything except the questions themselves, and Spotify, runs on the Pi.

It's being built in stages - see [docs/roadmap.md](docs/roadmap.md). The standalone Pi version comes first. Later, the heavy lifting moves to a home server, the Pi becomes one of several satellites around the house, and Home Assistant handles the lights and other devices.

## What it can do

- **Timers and reminders** - "set a timer for ten minutes", "remind me to take the pizza out in 20 minutes", "remind me at 4pm to call mum". Timers ring until they're answered, for up to ten minutes; reminders come back every half a minute for five. Both survive a restart.
- **Alarms** - "wake me up at 7 on weekdays". They ring in bursts, building up from quiet, with gaps for "Hey Nova, stop" to be heard in.
- **Spotify** - play a song, artist, album or playlist (or your liked songs), pause, skip, go back, shuffle, repeat, "what's this song?". The music pauses while Nova is talking, and comes back when it's done.
- **Volume** - for Nova's voice, for timers and alarms, and for the music, each kept separately.
- **The weather** - now, today or tomorrow, from Open-Meteo.
- **Anything else** - answered by Claude, which can search the web when you ask it to ("search for...", "look up...").
- **Follow-ups** - when Nova asks you something, just answer; there's no need to say "Hey Nova" again.

The everyday commands - timers, pause, skip, volume, the time and the weather - are recognised on the Pi itself, without asking Claude, so they're quicker. With the internet down, timers, alarms, Nova's own volume and the time carry on working; anything which needs Spotify, the forecast or Claude says straight away that it can't reach the internet, rather than going quiet.

Lights and other devices aren't done yet; that comes with Home Assistant.

## How it works

```
microphone ─► wake word ─► record until you stop talking ─► Whisper ─► local command, or Claude ─► action ─► Piper ─► speaker
               (openWakeWord)   (Silero voice activity)       (faster-whisper)                    (timers, Spotify, ...)
```

- `main.py` runs the loop above, and decides how each request is answered and confirmed.
- `voice_assistant/` holds everything else, one module per job: `microphone.py` and `speaker.py` own the audio devices, `wake_word.py`, `vad.py`, `speech_to_text.py` and `text_to_speech.py` handle speech, `intents.py` the commands handled locally, `llm.py` Claude, `actions.py` everything Nova can do - each tool Claude is offered, declared beside the code which carries it out - and `clock.py`, `spotify_controller.py`, `volume.py` and `weather.py` the features themselves.
- The microphone is opened once and never closed, keeping the last few seconds of audio, so nothing said straight after "Hey Nova" is lost. The speaker is opened once too, and everything that plays goes through it, so a timer or alarm can be stopped the moment you speak.

The reasons behind the bigger choices are in [docs/decisions/](docs/decisions/).

## Installing on a Raspberry Pi

You'll need a Raspberry Pi 5 with Raspberry Pi OS (64-bit), a USB microphone, a speaker, and an [Anthropic API key](https://console.anthropic.com/). For Spotify, you'll also need Spotify Premium and a [Spotify app](https://developer.spotify.com/dashboard) for its client ID and secret, with `http://127.0.0.1:8888/callback` as its redirect URI.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh
```

`setup.sh` asks a few questions as it goes, and is safe to run again - anything already set up is left alone. In order, it:

1. Asks for the assistant's name, wake word and Spotify speaker name, and saves them to `config.yaml`.
2. Asks for your API keys and writes them to `.env`, readable only by you. An existing `.env` is left as it is.
3. Finds your wake word model (see below) and copies it into `models/`.
4. Installs the system packages, makes a Python environment in `venv/`, and installs the Python packages.
5. Downloads Piper (the voice), the Whisper models and openWakeWord's own models into `models/`.
6. Offers to write `/etc/asound.conf`, so the assistant and Spotify can share the speaker and microphone. It asks which card is which - see `aplay -l` and `arecord -l`.
7. Offers to install raspotify, so the Pi shows up as a Spotify Connect speaker.
8. Offers to start the assistant at boot, as a user service.

Then sign in to Spotify once - it prints a link to open in a browser:

```bash
venv/bin/python voice_assistant/spotify_controller.py
```

### The wake word model

"Hey Nova" is a custom [openWakeWord](https://github.com/dscripka/openWakeWord) model, `hey_nova.onnx`, and it isn't in this repository - keep a copy somewhere safe, and copy it onto the Pi before running `setup.sh`. Without it, Nova still starts, but listens for openWakeWord's stock "Hey Jarvis" until `models/hey_nova.onnx` is there.

### Exact package versions

`requirements.txt` says what's needed; `requirements.lock` says what's known to work on the Pi. To install exactly those (Python 3.13 only):

```bash
venv/bin/pip install --no-deps -r requirements.lock
```

## Configuring

Everything you'd want to change is in `config.yaml`, with a comment on each setting - the wake word, the voice, how long to wait for you to talk, timer and alarm behaviour, where the weather is for, and whether conversations are kept in the logs. Keys and secrets go in `.env`, never in `config.yaml`.

The settings most likely to need changing:

| Setting | What it's for |
| --- | --- |
| `weather.location` | Your town or city - in `config.local.yaml`. Empty until you set it. |
| `thresholds.vad_threshold` | How sure Nova must be that it's hearing speech. Raise it if noise keeps it listening; lower it if quiet speech is missed. |
| `thresholds.silence_duration` | How long a pause ends what you're saying. |
| `thresholds.wake_word_confidence` | How sure the wake word model must be. Raise it if Nova wakes by itself. |
| `logging.log_transcripts` | Whether what's said is kept in the logs. |

Settings for this one machine - where you live, for the weather - go in `config.local.yaml` beside it instead. It's never committed, so they stay out of the public repository, and an update can't overwrite them. Anything in it overrides `config.yaml`, setting by setting:

```yaml
weather:
  location: "Leeds"
```

Volume levels set by voice are saved in `data/volumes.json`, and timers and alarms in `data/`.

## Running it

As a service (the usual way, once `setup.sh` has installed it):

```bash
systemctl --user restart voice-assistant
```
```bash
journalctl --user -u voice-assistant -f
```

Or by hand, to watch what it's doing:

```bash
venv/bin/python main.py
```

Logs are in `logs/smart_assistant.log`, trimmed at 5 MB. How long each stage of every interaction takes is kept in `logs/latency.jsonl`, and summarised by:

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

Both also run on GitHub for every push (`.github/workflows/checks.yml`), along with a weekly check of `requirements.lock` for packages with known security problems. The scripts in `scripts/manual/` need real hardware or accounts, so they're run by hand, and `scripts/soak_monitor.py` keeps an eye on a long-running assistant - see [docs/release-checklist.md](docs/release-checklist.md) for when to use them.

When picking work back up after a break, start with [docs/roadmap.md](docs/roadmap.md).
