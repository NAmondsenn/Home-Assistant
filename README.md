# Nova

A self-hosted voice assistant, with a Raspberry Pi in each room to talk to. Say "Hey Nova", ask for something, and it answers out loud - timers, reminders, alarms, music from your own Navidrome server or Spotify, the lights through Home Assistant, your calendar from Google, the weather, and general questions through Claude. Everything stays on your own network except the questions themselves, Spotify, your calendar, which is read from Google, and the notifications and rings sent to the Mac, tablet and phone through the Home Assistant app, which go by its push service.

It's built in two halves - see [docs/decisions/0006](docs/decisions/0006-the-pi-as-a-satellite.md) - and in stages, in [docs/roadmap.md](docs/roadmap.md). The **brain** runs on a home server (a container on PVE2) and does everything which thinks: speech-to-text, Claude, the voice, music, Home Assistant, timers, memory and the settings page. A **satellite** is a Raspberry Pi in a room, which only hears and speaks: its microphone, speaker and wake word. One brain looks after any number of satellites.

## What it can do

- **Timers and reminders** - "set a timer for ten minutes", "remind me to take the pizza out in 20 minutes", "remind me at 4pm to call mum". Timers ring until they're answered, for up to ten minutes; reminders come back every half a minute for five. Both survive a restart.
- **Alarms** - "wake me up at 7 on weekdays". They ring in bursts, building up from quiet, with gaps for "Hey Nova, stop" to be heard in.
- **Music** - from your own [Navidrome](https://www.navidrome.org/) server, or from Spotify: play a song, artist, album, genre or playlist (or your liked songs), pause, skip, go back, shuffle, repeat, "what's this song?". The music pauses while Nova is talking, and comes back when it's done. Navidrome's plays through Nova itself, so it pauses instantly and keeps working with the internet down.
- **Volume** - for Nova's voice, for timers and alarms, and for the music, each kept separately.
- **The lights** - "turn off the kitchen lights", "set the living room lights to 30 percent", "make the lamp blue", "make the lights warmer", through Home Assistant. Short commands like "lights on" mean the lights in the room Nova is in.
- **In Home Assistant** - Nova is a device there, with its volumes, next alarm, timers, stop, restart, a box to make it say something, and a switch to mute its microphone. See "Nova in Home Assistant" below.
- **Your calendar** - "what have I got on Thursday?", "am I busy this weekend?", from every calendar in your Google account - see "Google Calendar" below. "What have I got on this week?" is an overview - which days there's work or uni, and anything else by day - rather than every time of every day. "When's my MOT?" looks for one thing, up to three months ahead. And "what time do I have work tomorrow?", "when's my next shift?" or "when's gym tomorrow?" - each of your calendars but your own is an activity, by its name, with every event on it. "Put the dentist on Thursday at 3" adds it to your calendar, and says back what it added.
- **Batteries** - "how's my Mac's battery?", "what's my phone on?" or "battery levels", for every device Home Assistant has a battery level for. Nova also warns you as one runs low. See "Batteries" below.
- **Your phone** - "where's my phone?" says where Home Assistant's app last put it, and "ring my phone" or "find my tablet" rings it, wherever it is. Where it is never goes to Claude. See "Finding your phone" below.
- **Lists** - "add milk to my shopping list", "what's on my shopping list?", "tick off milk" or "I've got the eggs", for Home Assistant's own to-do lists, found by the words you use for them, so "notes" is a list called notepad. With no list named, adding and reading use your main one - todo, unless you choose another on the settings page, under Calendars and lists - and ticking off looks through them all. Assist is asked first, but never to remove anything: a thing is only ever ticked off, so it can be put back in Home Assistant. Several things at once - "add eggs, milk and bread" - go to Claude, which adds each one, and with Claude out of reach they're added as one, rather than lost.
- **Your PC** - "turn on my PC" wakes it with Home Assistant's Wake-on-LAN button, and "shut down my PC", "restart my PC", "put my PC to sleep", "turn off my PC's screen" and "lock my PC" press its HASS.Agent buttons. "Is my PC on?" says whether it is. Shutting down and restarting ask first - "Shut down the PC?" - and only a plain yes said straight back does it ("yes", "go ahead", "do it") - "no", "stop" or "cancel" leaves it, and anything else is taken as a fresh request; Claude can ask, but never press without one. There's nothing to set up if HASS.Agent's device and the Wake-on-LAN button are in Home Assistant: Nova finds the PC by its device, whatever its buttons are called, and never presses its other buttons, nor any of Proxmox's. With more than one PC or more than one Wake-on-LAN button, choose which button wakes each PC on the settings page, under Calendars and lists - one button wakes one PC, so choosing it for one takes it off another.
- **The weather** - now, today or tomorrow, from Home Assistant's own weather: Met's forecast, for Home Assistant's home location, so there's nothing to set in Nova. A forecast over three hours old is said with its age. A home with more than one weather entity uses the first by entity id, unless you choose another on the settings page, under Calendars and lists ("Weather from").
- **Anything else** - answered by Claude, which can search the web when you ask it to ("search for...", "look up...").
- **Follow-ups** - when Nova asks you something, just answer; there's no need to say "Hey Nova" again.
- **Talking over it** - "Hey Nova" is heard over the music Nova plays and over its own replies, which stop the moment it's said. "Hey Nova, stop" stops whatever it's saying or playing.
- **Not answering a film** - while a media player which Home Assistant puts in the satellite's room is playing (HASS.Agent's, for a PC, say), the wake word has to be surer, and Claude is told, so that what sounds like speech from what's playing, rather than a request for Nova, is ignored without a word. Stop, pause, volume, timers and the other everyday commands are answered as ever, and Nova's own music doesn't count. See `thresholds.media_wake_word_confidence` under Configuring.
- **Remembering you** - mention something about yourself ("I've started a new job at Aldi", "my sister's called Amy") and Nova keeps it, says "I'll remember that", and uses it from then on. "What do you know about me?" reads it back; "forget that..." or "forget everything you know about me" takes it away. It's kept in `~/.local/share/nova/memory.json`, and goes to Claude with each request so it can use it.
- **Notifications** - with your own [ntfy](https://ntfy.sh/) server, alerts posted to the topics you choose - your homelab's, say - are read out as they arrive, and "tell me more" reads all of one. At night only the most urgent are; "any notifications?" has the rest. "Send that to my phone" sends Nova's last answer there, and "send that to my Mac" (or the tablet) sends it through the Home Assistant app's notifications instead, as do a reminder nobody answered and Nova restarting after a problem.
- **Routines** - several things at once from a few words of your choosing ("Hey Nova, good morning"), or by themselves at a set time: each step is something you'd say to Nova - "what's the weather", "turn on the kitchen lights", "play my morning playlist" - done just as if you had, so a routine can do anything Nova can. Made and run on the settings page, and kept in `~/.local/share/nova/routines.json`.
- **A settings page** - how Nova's doing, what's playing, your lights and Home Assistant's scripts and scenes, your batteries and when each warns, your calendars and lists, the sign-ins to Spotify and Google, its volumes, timers and alarms, what it remembers, its keys and its main settings, from your phone or laptop - over your tailnet, and only for you, or, if you choose, through Nginx Proxy Manager at an address of your own, behind a password of its own.

The everyday commands - timers, pause, skip, volume, the time, the weather, the lights, the calendar and the lists - are recognised on the brain without asking Claude, so they're quicker. With the internet down, the brain still handles timers, alarms, the time, the lights and Navidrome; anything which needs Spotify or Claude says straight away that it can't reach the internet, rather than going quiet. With the brain unreachable, a satellite can't understand anything, and says so - but its timers and alarms, which it keeps a copy of, still ring, and "stop" still stops them.

## How it works

```
 on the satellite                                           over the tailnet                on the brain
microphone ─► wake word ─► record until you stop talking ─────────────────────► Nemotron ─► local command, or Claude ─► action ─► Piper ─► speaker
               (openWakeWord)   (Silero voice activity)    <───── the voice ──────  (hears as you speak)                (timers, music, ...)
```

- `satellite.py` runs the satellite: it listens, sends the recording to the brain as it's made, and plays what comes back.
- `backend.py` runs the brain: it hears each satellite's recording, works out the answer, and sends the speech back. Each satellite connects with a password of its own.
- `voice_assistant/` holds everything else, one module per job. On the brain, `brain.py` holds what every satellite shares, and gives each one connected a session of its own (`session.py`), which answers it through `remote.py`, the satellite as the brain sees it. `conversation.py` works out each reply: `intents.py` the commands handled locally, `llm.py` Claude, and `actions.py` everything Nova can do - each tool Claude is offered, declared beside the code which carries it out. `vad.py`, `speech_to_text.py` and `text_to_speech.py` handle speech, and `clock.py`, `music.py` with `navidrome.py` and `spotify_controller.py`, `home_assistant.py`, `volume.py` and `weather.py` are the features themselves. On a satellite, `microphone.py` and `speaker.py` own the audio devices, `wake_word.py` hears "Hey Nova", and `fallback.py` rings the timers and alarms from its copy while the brain can't be reached. Between the two, `satellite_link.py` keeps the connection, and `protocol.py` says what goes over it and how each end proves the password.
- The microphone is opened once and never closed, keeping the last few seconds of audio, so nothing said straight after "Hey Nova" is lost. Nemotron on the brain hears the recording as it's made, so it's mostly worked out the words by the time you stop talking. The speaker is opened once too, and everything that plays goes through it, so a timer or alarm can be stopped the moment you speak.

The reasons behind the bigger choices are in [docs/decisions/](docs/decisions/).

## Installing

Nova is installed in two roles, from the same clone of the repository. Set the brain up first, then each satellite. Both are done with `setup.sh`, which asks a few questions as it goes and is safe to run again - anything already set up is left alone, and each step says whether it was done (✓), skipped (–) or needs you (✗). The role is kept in `~/.config/nova/role`, so `update.sh` and a later `setup.sh` know which it is.

### The brain

The brain is a container (or any machine) running Debian, with Python 3.13 and about 4GB of memory, on your tailnet. It needs an [Anthropic API key](https://console.anthropic.com/) and, for Spotify, Spotify Premium and a [Spotify app](https://developer.spotify.com/dashboard) for its client ID and secret, with `http://127.0.0.1:8888/callback` as its redirect URI. It has no microphone or speaker, and nothing outside your tailnet can reach it - unless you serve the settings page through Nginx Proxy Manager, which opens a port on the brain to your whole network: anything there but NPM gets a bare 403, and through NPM the page needs its password (see [At an address of your own, through Nginx Proxy Manager](#at-an-address-of-your-own-through-nginx-proxy-manager)).

The brain never runs as root. Make it a user of its own (here `nova`, with no sudo), clone the repository as that user, and run the setup as root, from outside or inside the container - root does only the system steps (packages, the service), and everything else, including the packages in `venv/`, the models, and the settings and keys in that user's `~/.config/nova`, is done as the user who owns the clone. It refuses a clone owned by root.

```bash
runuser -u nova -- git clone https://github.com/NAmondsenn/Home-Assistant.git /home/nova/Home-Assistant
cd /home/nova/Home-Assistant
bash setup.sh --brain          # as root
```

In order, it:

1. Checks this is Debian with Python 3.11 or newer - though the exact versions in step 2 are for Python 3.13, which Debian 13 has.
2. Installs the system packages (ffmpeg among them), makes a Python environment in `venv/`, and installs the Python packages - the exact versions in `requirements-backend.txt`.
3. Asks for the assistant's name, wake word and Spotify speaker name, saving any you change to `~/.config/nova/config.yaml`.
4. Asks for your API keys and writes them to `~/.config/nova/.env`, readable only by you. An existing one is left as it is.
5. Downloads Piper, the voice (`en_US-hfc_female-medium`), the Nemotron speech-to-text model and Parakeet (a second, small one, asked when Nemotron hears nothing in a lone word such as "Pause.") into `models/`. They're checked against pinned SHA-256 hashes, so a tampered download stops setup rather than being run. Nemotron needs about 1.5GB free, and Parakeet about 400MB more - without room for Parakeet, or the internet to fetch it, setup carries on without it. `models/` can be a link to a folder somewhere with more room, and everything follows it. openWakeWord's own models (the brain uses one for its voice activity check) are fetched by its package, into `venv/`, with no hash check of Nova's.
6. Installs the brain as the `nova-backend` system service, which runs as that user and starts at boot, and starts it.

Then let each satellite in, by its machine's name: on the settings page (see [The settings page](#the-settings-page)), under Satellites, type it in and Add a satellite. The page shows a password for it once, so keep it for the satellite's setup. Without the page, `add-satellite` does the same, printing the password - run it as the brain's user, not root, as the passwords are kept in that user's `~/.config/nova/satellites.json`, which is where the service reads them.

```bash
cd /home/nova/Home-Assistant
runuser -u nova -- venv/bin/python backend.py add-satellite kitchen
```

`remove-satellite NAME` stops letting one in, run the same way, and lets it go straight away if it's connected - as Remove does, under Satellites on the settings page. Its timers, alarms and routines move to the default satellite (the first added, unless another is made the default on the page), and its copy of the alarms is emptied if it's connected. One that isn't keeps its copy for a week, so wipe it or connect it before switching it on.

From here on, anything this README has you run on the brain which reads or writes its settings, keys or data - a `venv/bin/...` command, or a file in `~/.config/nova` or `~/.local/share/nova` - is for the brain's user, not root, whose own folders the service never looks in. Run commands with `runuser -u nova -- `, as above, and edit its files as that user, e.g. `runuser -u nova -- nano /home/nova/.config/nova/.env`. Only `systemctl`, `journalctl`, `tailscale` and the setup and update scripts are for root. Put the brain on your tailnet, where it's reached by its name and port 10400, as `nova:10400` - as root:

```bash
tailscale serve --bg --tcp 10400 tcp://localhost:10400
```

The brain itself only listens on its own machine, so that's the one way in. The brain's own settings and keys, and everything it saves, are in `~/.config/nova` and `~/.local/share/nova` on its machine, and are where the rest of this README says "the brain".

The brain's log is `journalctl -u nova-backend -f`, and `systemctl restart nova-backend` (as root) restarts it. To update it, run `bash update.sh` there, as root, from its clone - or `bash update.sh --no-pull` to use the code as it is, after switching branch, say, instead of fetching first.

### A satellite

You'll need a Raspberry Pi with Raspberry Pi OS (64-bit), a USB microphone and a speaker, on the same tailnet as the brain, and the password the settings page (or `add-satellite`) gave for it. There's no standalone mode: without its brain a satellite can only ring its timers and alarms, and say it can't reach it. The speaker and the microphone have to be two separate sound cards, and not the Pi's own HDMI or headphone output, which setup's shared audio step leaves out. For "Hey Nova" itself you'll need a wake word model of your own - see [The wake word model](#the-wake-word-model) - or the satellite answers to "Hey Jarvis" instead.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh
```

In order, it:

1. Checks this is Raspberry Pi OS or Debian, with Python 3.11 or newer, a speaker and a microphone.
2. Installs the system packages, makes a Python environment in `venv/`, and installs the Python packages - on Python 3.13 the exact versions in `requirements.lock`, on 3.11 the newest ones `requirements.txt` allows. Either is only what the microphone, speaker and wake word need.
3. Asks for the assistant's name, wake word, Spotify speaker name and the brain's address (`nova:10400`), saving them to `~/.config/nova/config.yaml`.
4. Says the name the brain knows this satellite by - its machine's name - and asks for the password the settings page gave for it (not shown as it's typed), writing it to `~/.config/nova/.env` as `NOVA_BACKEND_TOKEN`, readable only by you. It's the only secret a satellite holds. Left blank, the satellite won't start until it has one, which setup says - give it one later with `bash setup.sh --new-password`.
5. Looks for your wake word model (see [The wake word model](#the-wake-word-model)) and copies it into `models/` - without one, the satellite listens for "Hey Jarvis" - and has openWakeWord download its own models, which its package keeps in `venv/`. A satellite downloads no speech or voice models - the brain does the hearing and the speaking.
6. Offers to write `/etc/asound.conf`, so the assistant and Spotify can share the speaker and microphone - in short bursts, so a sound stops quickly and the wake word reacts sooner - backing up the old one. It looks for a speaker and a microphone on two different sound cards, leaving out the Pi's own HDMI and headphone outputs, and writes nothing if it can't find both.
7. Offers to install raspotify, so the Pi shows up as a Spotify Connect speaker (see "Spotify on a satellite", under Music).
8. Sets the satellite to start at boot, as the `voice-assistant` user service, and starts it.

Each satellite is named by its own machine's name, which the brain knows its password by - the same name given to `add-satellite`. Set `backend.satellite` in `config.yaml` to use another. Either way, it must fit the rule `add-satellite` and the settings page keep to: lower-case letters, digits and hyphens, not starting or ending with a hyphen.

`bash setup.sh --yes` asks nothing, taking what it finds and the defaults; `--no-spotify` leaves Spotify out, and `--no-service` doesn't start Nova at boot. `--brain` and `--satellite` choose the role; without one, an existing install keeps its own.

To give a satellite a new password - one turned away, or a password that may have got out - press New password beside it on the settings page, under Satellites. The brain lets it go straight away, and turns it away until it has the new one, which the page shows once. Then, on the Pi, as its user:

```bash
cd Home-Assistant
bash setup.sh --new-password
```

It asks for the new password (not shown as it's typed), puts it in `~/.config/nova/.env` in place of the old one, leaving the rest of the file as it was, and restarts the satellite. A Pi with a new machine, or a new SD card, is set up as above with the same name, after New password - adding it again isn't needed, and removing it would move its timers, alarms and routines to the default satellite.

To update to the latest code later - it never touches your settings, keys or saved data - run this on the brain (as root) and on each satellite (as its user), adding `--no-pull` to use the code as it is rather than fetching first - after switching branch by hand, say, or to go back to a tag, which can't be pulled:

```bash
bash update.sh
```

and to take a satellite off the Pi, asking before removing anything which can't be got back:

```bash
bash uninstall.sh
```

### Music

Navidrome or Spotify - `music.player` in `config.yaml` says which, and it's Spotify unless you change it. Both are set up on the brain, which plays the music through each satellite's speaker.

**Navidrome.** Make a Navidrome user for Nova (or use your own), then put its password under Keys on the settings page (at `https://` and the brain's name on your tailnet - see [The settings page](#the-settings-page)), and its address and user under Settings. Or, on the brain, add the password to `~/.config/nova/.env` yourself:

```
NAVIDROME_PASSWORD=the-password
```

and point Nova at it in `~/.config/nova/config.yaml` (Settings has these too, used once Nova restarts):

```yaml
music:
  player: "navidrome"
navidrome:
  url: "http://navidrome.local:4533"
  username: "nova"
```

Songs are fetched as MP3 - Navidrome converts anything else as it sends it - and decoded with ffmpeg on the brain, which `setup.sh --brain` installs. Your liked songs are the ones starred in Navidrome. Nova never sends the password itself, only a salted hash of it, as Navidrome's API has it.

**Spotify.** Spotify Premium is needed. Its client ID and secret (`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, from Spotify's developer dashboard) go under Keys on the settings page, or in the brain's `~/.config/nova/.env` yourself, and your Spotify app has to list the brain's `SPOTIFY_REDIRECT_URI` as a redirect URI - setup.sh sets it to `http://127.0.0.1:8888/callback`. Once Nova has restarted with them, sign in on the settings page, under Music. Spotify's page opens in a new tab, and once you've agreed it sends you to an address that doesn't load: paste that address back on the settings page. Every satellite's Spotify uses the sign-in from its next command, with no restart. Without the page, sign in on the brain instead, as its user - it prints a link to open in a browser:

```bash
runuser -u nova -- venv/bin/python -m voice_assistant.spotify_controller
```

**Spotify on a satellite.** Spotify plays through raspotify on the satellite, which `setup.sh` offers to install, so the Pi shows up as a Spotify speaker called `spotify.device_name` (`Voice-Assistant` unless you changed it). raspotify has to be signed in to your Premium account too, once, apart from Nova's own sign-in above. Its own guide says that, with Spotify Connect discovery (zeroconf, over mDNS) left on, as `setup.sh` leaves it, it needs no username or password: open the Spotify app on a phone or computer on the same local network as the Pi and choose the Pi from its list of devices. raspotify then keeps the login, as `setup.sh` leaves its credential cache on and gives it the folder to keep it in. These are raspotify's own steps, not yet tried from a fresh Nova install. If Nova says it can't find the speaker, see [docs/troubleshooting.md](docs/troubleshooting.md).

### Home Assistant, for the lights

This is set up on the brain, which is what talks to Home Assistant - a satellite holds no token. Nova asks Home Assistant for things in plain words, through its own Assist, which can only reach the devices you've exposed to it. Besides the lights, that's switches, media players, scripts and scenes - "turn off AdGuard protection", "pause the TV", "is the fan on?", "run goodnight", "activate movie night" - after Nova's own commands, so "pause" is still Nova's music and "turn off the timer" Nova's timer. Dimming is Nova's own: it steps each light's brightness itself. What Assist doesn't understand - "make the lamp warmer" - goes to Claude, which can switch and set any light, exposed or not, run a script or scene exposed to Assist once Nova has checked it as below, and read how things are - "is Navidrome up?", "is the garage door open?" - but nothing else. What Claude reads goes to Anthropic, so Nova leaves out where people and things are, network and hardware addresses, the apps and windows in use, notifications and the batteries (which have their own question), and gives a phone's, a Mac's or the PC's sensors only as numbers and times.

Whatever's exposed, Nova never locks, unlocks or opens anything by voice: it checks what Home Assistant says each thing is - a lock, a valve, an alarm panel, or a door, garage door, gate or window - not only the words. Nor does it give Assist anything of the Proxmox integration, nor the PC's HASS.Agent commands, whether each is a button, a switch or anything else, bar the sleep, monitor sleep and lock buttons - through Assist, a shutdown or restart would be done with no yes. Something which acts and isn't in Home Assistant's registry isn't given Assist either, as it could be a new one of Proxmox's: a new one is, once the registry's next read has it - it's read every five minutes - but one with no unique id never is, so it's refused until it has one: `unique_id` in its YAML, or made in the UI. A YAML template or command_line switch and a scene with no id are like that. An old-style `group.*` can't take a unique id at all, so make it a group helper in the UI instead (Settings > Devices & services > Helpers > Group). A script, scene or group is only run once Nova has checked what it does, and a script Home Assistant can't show the contents of - one written in YAML outside `scripts.yaml` - isn't run by voice or offered on the settings page; nor is one that stops or restarts Home Assistant or its host.

1. In Home Assistant, make a user for Nova - Settings > People > Add person, allowed to log in. It needs to be an administrator for most of this, as Home Assistant only lets administrators ask what's in which room, read its registry (what each thing is and what's exposed) and show a script's contents. As an ordinary user, the wider rule is off - only light sentences go to Assist, without rooms, and only while Assist's sentence debugging doesn't answer either, as without the registry nothing it says they'd act on can be vouched for - there are no batteries, and no script is run at all, by voice, by Claude or from the settings page. An administrator's token can do anything in Home Assistant, so keep it as private as a password: what Nova does with it is kept to the lights, finding and ringing your phone, the PC's own buttons, reading how things are for Claude, and the scripts and scenes you run from the settings page or Claude runs for you, by Nova's own code, not by the token.
2. Log in as that user, and under its profile > Security, make a long-lived access token.
3. Put it under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself - it's a password, so don't paste it anywhere else:

   ```
   HA_TOKEN=the-token
   ```
4. Put Home Assistant's address under Settings on the settings page, or in `~/.config/nova/config.yaml`:

   ```yaml
   home_assistant:
     url: "http://homeassistant.local:8123"
   ```
5. In Settings > Voice assistants > Expose, choose what Assist may control. Exposing something now means Nova works it by voice, so check what's exposed already - a switch such as AdGuard's protection may well be, from before. Names and areas are what you'll call things. Scripts aren't exposed by default, so expose any you'll ask for, such as goodnight - a routine's "run goodnight" step goes through Assist too. "Run goodnight" runs Home Assistant's script called goodnight; "goodnight routine" (or a routine's own words) is Nova's own routine. Claude isn't limited to these for the lights - it can work any light.

Then restart the brain. Its log says `Home Assistant at ...` once it's on. The calendar isn't Home Assistant's - Nova reads it from Google itself, below.

### Google Calendar

Nova reads your calendars straight from Google, not through Home Assistant, with a sign-in of its own kept on the brain - readable by Nova's user only, and never on a satellite. It asks Google for only what it uses: the list of your calendars, reading their events, and adding events to them.

1. In [Google Cloud's console](https://console.cloud.google.com/), make a project for Nova, and under APIs & Services > Library switch on the Google Calendar API.
2. Under the OAuth consent screen, make it External, and add the three scopes Nova asks for: `calendar.calendarlist.readonly`, `calendar.events.readonly` and `calendar.events.owned`. Then publish it, so it's In production - in Testing, Google ends the sign-in after a week. Google may warn that the app isn't verified; it's your own, so carry on.
3. Under Credentials, make an OAuth client ID of type Desktop app. Its client ID and secret go under Keys on the settings page, or in the brain's `~/.config/nova/.env` yourself - the secret is a password, so don't paste it anywhere else:

   ```
   GOOGLE_CLIENT_ID=the-client-id
   GOOGLE_CLIENT_SECRET=the-client-secret
   ```

   Then restart the brain, which makes the calendar as it starts - the settings page can.
4. Sign in on the settings page, under Calendars and lists: "Sign in to Google" opens Google's page in a new tab. Google then sends that tab to an address which won't load; that's expected, and you paste that address back on the settings page. It's used from the next question, with no restart. "Sign in again" there signs in as a different account, or after the sign-in was removed in your Google account. Every box on Google's page needs ticking - a sign-in without one of them isn't kept, and says which.

Every calendar in your Google account's own list is read - one added there later turns up by itself, within five minutes - with each repeating event on each of its days. On the settings page, under Calendars and lists, each is shown by its name in Google, with a switch for whether it's in what's on: one switched off isn't read for "what's on" or "when's my MOT?", but is still read when it's asked about as an activity. Nothing of it is kept on the brain: with Google out of reach, Nova says "I can't reach Google's calendar right now", and once the sign-in needs doing again, it says so. `home_assistant.calendars` isn't used any more, and can be deleted from the brain's `config.yaml`.

Each calendar but your own - Google's primary one - Google's own (holidays, your contacts' birthdays) and one named by an email address, as a person's own shared with you is, is an activity, by its name: with a Work calendar, "what time do I have work tomorrow?" reads that calendar alone, and every event on it is work, whatever it's called - bar an all-day one, which "what's on" says by its own name. A Gym calendar is "when's gym tomorrow?" once Nova has read Google's list with it in - at start, and when the calendar's asked about once the list is five minutes old. "Working" and "shifts" mean Work, and "lectures" and "classes" Uni; a name of two words is heard by its first too - "uni" for "Uni timetable" - unless another starts with it, and in "when's uni?" or "am I at uni?" only with a day after it. A calendar named like one of Nova's own things, such as Timer or Music, is left to Claude, so it never takes over Nova's own command. Which are activities can be switched on the settings page - each calendar's "An activity" - and it's used straight away: heard from the next question, and told to Claude from its next request. Each choice is kept by the calendar, in the brain's `calendars.json` in Nova's data folder, so one Google stops listing for a while is as it was when it's back. A calendar named by an email address is never said by it, or told to Claude: your own, which Google names by yours, is "your calendar", and any other "a shared calendar" - switched on as an activity, it's said that way too, but can't be asked about by name. `home_assistant.activities` isn't used any more either, and can be deleted.

Adding by voice - "put the dentist on Thursday at 3" - goes to your own, primary calendar unless you name another, and lasts an hour unless you say when it ends; a day without a time is all day. Nova says back what it added - "Added Dentist, Thursday the 29th at 3 PM." - only once Google has taken it, and names the calendar when it's not the one new events go to. Google only lets Nova add to calendars you own, so a shared one such as Uni says so. Nova never changes or deletes an event, and never sends one twice: if Google doesn't answer in time, or has trouble of its own, it says it can't tell whether it was added, so you can check before asking again. "New events go to", on the settings page, sends them to another of your own calendars instead, from the next one added - back to the primary if that one's deleted or no longer yours.

### Batteries

With Home Assistant set up, the brain finds every device it has a battery level for - phones, tablets, laptops, remotes - by itself, so a new one is there without telling Nova. Ask "how's my Mac's battery?", "what's my phone on?" or "battery levels", and Nova says each level, with how old the reading is if it's stale.

It also warns you as a battery falls: once at 5% for a phone or tablet, and at 15% and 5% for anything else, each only once until the device has charged. The warning goes to your phone through ntfy (`ntfy.phone`), and is said out loud as well on the default satellite while you're home and it isn't quiet hours (ntfy's `quiet_from` and `quiet_until`). With no ntfy, or if sending fails, it's said instead, and held until the morning if it comes at night. Each device's marks, and its switches for warning you and for sending to your phone, are on the settings page's Batteries section, and take effect straight away. `config.yaml`'s `batteries` section has only the marks each kind of device starts with - one for a phone or tablet, two for anything else - how often the batteries are read and when a reading counts as old. See `docs/decisions/0007-battery-warnings.md`.

### Finding your phone

With Home Assistant set up, the brain finds every phone, tablet or Mac with the Home Assistant app on it. Ask "where's my phone?" and Nova says whether it's at home, in one of Home Assistant's zones, or near a street and town, with how long ago that was. "Ring my phone", "find my tablet" or "ring the Mac" rings it too, through the app's own notifications: an Android phone says "Nova's looking for your phone" out loud on its alarm volume, an iPhone gets a critical alert, and a Mac a notification with its sound. Where it is goes no further than Nova, however it was asked - even of Claude: Claude's history only has "I said where your phone is.", and "where's my phone?" is still Nova's own while it's listening for an answer.

The one step: on the phone, let the Home Assistant app ring through Do Not Disturb - on Android, add Home Assistant to the apps allowed through Do Not Disturb, in the phone's Do Not Disturb settings; on an iPhone, allow its critical alerts in Settings > Notifications > Home Assistant. Without it, a phone on silent or Do Not Disturb doesn't ring.

### Nova in Home Assistant

Nova can also be a device of its own in Home Assistant, set up on the brain, through its MQTT broker (the Mosquitto add-on), so you can see and change things from Home Assistant's dashboards and automations. It has eleven controls: Status, Volume, Alarm volume, Music volume, Next alarm, Timers, Stop, Restart, Speak, Backend and Microphone muted. Nothing said to Nova, and none of its answers, is sent there - timers show only when they end. Each satellite is a device of its own. Without this, Nova works as it always did, and it carries on if the broker goes down.

1. In Home Assistant, make another user for Nova - Settings > People > Users > Add user, called `nova`, **not** an administrator, and with "Can only log in from the local network" on. The Users tab is only there with Advanced mode on in your profile. The broker accepts Home Assistant's users.
2. Put its password under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself:

   ```
   MQTT_PASSWORD=the-password
   ```
3. Say where the broker is under Settings on the settings page (its host, port and user, used once Nova restarts), or in the brain's `~/.config/nova/config.yaml`. The host is Home Assistant's own address on your home network, not its web address:

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
2. Put the token under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself:

   ```
   NTFY_TOKEN=the-token
   ```
3. Say which topic is which under Settings on the settings page, or in the brain's `~/.config/nova/config.yaml`:

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

Nova serves a page for looking after it from a phone or a laptop - how it's doing, its volumes, timers and alarms, what it remembers, its keys (which it only ever saves, never shows back) and its main settings. The star by Nova's name shows whether it's waiting, listening, thinking or speaking, and as a volume's dragged, Nova beeps on its speaker at each new level, so you can hear how loud it is. With music set up, it shows what's playing, and plays something else; with Home Assistant, it switches and dims your lights, sets their colour and warmth, groups them by room, and runs your scripts and scenes - never one whose name says it locks or opens anything, and a script asks first where a scene is one tap; and for each battery it sets when it warns you and whether it's sent to your phone. Calendars and lists chooses which of your calendars are in what's on, which are activities and which one new events go to, how each device is sent to, your main to-do list, which weather entity the forecast is from, and which Wake-on-LAN button wakes each PC - each used from the next request. Spotify is signed in to under Music, and Google under Calendars and lists. Stop silences whatever rings and what Nova's saying, on every satellite, and a removed alarm, timer or reminder, or a forgotten memory, can be undone for ten seconds. It's on the brain, not the satellites - one page for the whole house - and listens there alone, and only your own Tailscale login gets in:

1. In Tailscale's admin page, turn on HTTPS certificates - under DNS.
2. On the brain, have Tailscale pass the tailnet's requests on to the page, with a certificate for the brain's name (as root, which the brain's setup is run as; anyone else puts `sudo` in front):

   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```
3. Say who's let in, in the brain's `~/.config/nova/config.yaml`:

   ```yaml
   settings_page:
     allow: ["you@example.com"]
   ```

Then restart the brain, and open `https://` and its name on your tailnet - `tailscale status` shows it - on any of your devices. A change has to come from the page itself, so another website can't make one in your name; settings are saved to the brain's `~/.config/nova/config.yaml`, the one before kept as `config.yaml.bak`. Most are used straight away; under each, the page says whether it's used straight away or once Nova restarts, and when a save waits for a restart, it offers one. A satellite's own settings are changed on the satellite: the brain's address by hand, and its password with `bash setup.sh --new-password`, once New password, under Satellites, has made one.

#### At an address of your own, through Nginx Proxy Manager

The page can also be served through Nginx Proxy Manager (NPM), at `https://nova.<your domain>`, so it opens at home on any of your devices without Tailscale on - behind a password of its own, as NPM's requests carry no Tailscale login. The tailnet address stays as it is, with no password.

1. Set the page password first, on the page at its tailnet address: under Keys, Page password (for NPM) - at least 12 characters. It's kept only as a salted hash, never shown again, and a new one logs out every device using the old.
2. In the brain's `~/.config/nova/config.yaml`, give the page a second port and say that only NPM's address may use it, then restart Nova:

   ```yaml
   settings_page:
     proxy_port: 8766
     proxy_from: ["NPM's address"]
   ```

   That port listens on every address of the brain's, but anything but NPM gets a bare 403. With `proxy_from` empty, the port isn't opened at all.
3. In NPM, add a proxy host: domain `nova.<your domain>`, scheme `http`, forward to the brain's address and port 8766, with your wildcard certificate, Force SSL on, and the "LAN + Tailscale" access list. No websockets are needed.

Then open `https://nova.<your domain>`. Until you log in, it shows only a login form, and none of Nova's own is served - only the page's files, which are all in this repository. A login lasts 30 days on that device and can do everything the tailnet address can - keys and settings included. Keys are never shown, but a login can change where Nova finds Home Assistant, Navidrome, ntfy or MQTT, and Nova would send that service's token or password to the new address once it restarts, so a login is worth as much as the keys. On a shared or lost device, use Log out (under Keys, on that address) or Log out everywhere, which ends every login made with the page password, whichever device - from the tailnet address too. Replacing the page password at the NPM address asks for the current one as well. After 5 wrong passwords in a row, a device's tries are refused for a minute; NPM says which device each try is from, in the `X-Real-IP` header it sends by default. A forgotten page password is set again at the tailnet address.

### The wake word model

"Hey Nova" is a custom [openWakeWord](https://github.com/dscripka/openWakeWord) model, `models/hey_nova.onnx`. It isn't in this repository, and none comes with Nova, so you train one or supply your own. Only satellites listen for it.

**Without it**, Nova still starts, but the satellite listens for openWakeWord's own stock "Hey Jarvis" instead - saying "Hey Nova" does nothing - until `models/hey_nova.onnx` is there. Its log says `No wake word model at ...` as it starts, and setup says the same.

**To train one**, openWakeWord has an automated training utility: a Google Colab notebook which makes a model in under an hour, though its README warns that the model may do poorly in some settings, and a fuller notebook, `notebooks/automatic_model_training.ipynb` in openWakeWord's repository, which can make a better one but needs more development experience. Its README, under "Training new models", has both. Nova needs the `.onnx` file.

**To supply your own**, copy the file into `models/` on each satellite, before running `setup.sh` or after (then restart the satellite). Name it after the wake word, in lower case with each space an underscore, so "Hey Nova" is `hey_nova.onnx`. For another phrase, give `assistant.wake_word` that phrase on the satellite - setup asks for it - and name the model to match, as `hey_computer.onnx` for "Hey Computer". The brain takes the wake word off the front of what it hears using its own `assistant.wake_word`, so give it the same phrase.

Keep a copy of the file somewhere safe, as nothing else has it.

### Exact package versions

`requirements.txt` says what a satellite needs; `requirements.lock` says what's known to work on the Pi; and `requirements-backend.txt` is the brain's, all of them exact. To install exactly those (Python 3.13 only):

```bash
venv/bin/pip install --no-deps -r requirements.lock           # a satellite
runuser -u nova -- venv/bin/pip install --no-deps -r requirements-backend.txt    # the brain, from root
```

## Configuring

The repository's `config.yaml` has every setting, with its default and a comment saying what it's for - the wake word, how long to wait for you to talk, timer and alarm behaviour, the music player, and more. Don't change it there: put just the settings you want different in **`~/.config/nova/config.yaml`**, in the same sections, and they're used on top:

```yaml
music:
  player: "navidrome"
thresholds:
  silence_duration: 0.8
```

That file is this install's own, outside the repository, so an update never touches it and your settings never end up on GitHub. It's checked as Nova starts: a misspelt setting, one of the wrong kind, or one out of its range (a 5-second silence, say) stops it with a message naming it - `journalctl -u nova-backend` shows it on the brain, `journalctl --user-unit voice-assistant` on a satellite - rather than being quietly ignored. `update.sh` checks them the same way before it stops Nova, and if the new code would refuse them, it says why, takes the code it pulled back out and leaves Nova running as it was. A setting Nova no longer has, such as `weather.location`, is only warned about, with where it's set now, and can be deleted. Almost everything is set on the brain; a satellite's file has little beyond its brain's address, its name and the wake word.

Keys and passwords go in **`~/.config/nova/.env`**, readable only by you, never in a settings file. Everything Nova saves - timers, alarms, volume levels, what it remembers about you, the Spotify sign-in and its logs - is in **`~/.local/share/nova/`**.

The settings most likely to need changing:

| Setting | What it's for |
| --- | --- |
| `music.player` | `navidrome` or `spotify`. |
| `backend.address` | On a satellite, its brain, as `name:port`. |
| `thresholds.vad_threshold` | How sure Nova must be that it's hearing speech. Raise it if noise keeps it listening; lower it if quiet speech is missed. |
| `thresholds.silence_duration` | How long a pause ends what you're saying. |
| `thresholds.wake_word_confidence` | How sure the wake word model must be. Raise it if Nova wakes by itself. The brain's setting, not the satellite's own: on the settings page, under Listening, as "How sure Nova must be it heard its wake word", and the brain sends it to each satellite. Used straight away. |
| `thresholds.media_wake_word_confidence` | The same while a media player which Home Assistant puts in the satellite's room is playing, as a film's speech can sound like the wake word. Nova's own music doesn't count, and the higher of the two is used then. On the settings page, under Listening, and used straight away. |
| `wake_clips.keep` | Whether the satellite keeps 3 seconds of audio around each wake (2 before the wake word was heard, 1 after), so that a voice check can be trained on real false wakes. Off unless you switch it on in the satellite's own settings, for as long as it's needed. The clips stay on the Pi, readable by you only, and are never sent anywhere. Used once the satellite restarts. |
| `logging.log_transcripts` | Whether what's said is kept in the logs. |
| `sounds.listening_enabled`, `sounds.chime_enabled`, `sounds.reminder_enabled`, `web_search.enabled`, `conversation.history_max_age`, `spotify.pause_while_speaking` | The blip, the chime for done, and the sound before alerts and warnings (alarms, timers and reminders always sound), searching the web, how long a conversation's thread is kept, and pausing Spotify while Nova talks. On the settings page, and used straight away. |
| `ntfy.quiet_from`, `ntfy.quiet_until` | The night, when only the most urgent alerts are read out. |
| `voice.speed` | How fast Nova speaks, from 0.75 to 1.5 times its usual speed. On the settings page, with a button to hear it first, and used from the next reply. |

## Running it

As a service (the usual way, once `setup.sh` has installed it). On a satellite:

```bash
systemctl --user restart voice-assistant
```
```bash
journalctl --user-unit voice-assistant -f
```

and on the brain, as root:

```bash
systemctl restart nova-backend
```
```bash
journalctl -u nova-backend -f
```

Or by hand, to watch what it's doing - `satellite.py` on a satellite, and `backend.py` on the brain (as its user, with `runuser -u nova -- ` in front, with the service stopped):

```bash
venv/bin/python satellite.py
```

Logs are in `~/.local/share/nova/logs/` - `smart_assistant.log` on a satellite and `backend.log` on the brain - trimmed at 5 MB, each with a `latency.jsonl` beside it. The brain's has how long each stage of every interaction takes, summarised by (on the brain, as its user, with `runuser -u nova -- ` in front):

```bash
venv/bin/python scripts/latency_report.py
```

A satellite's has, for each wake word, how long until the first of the reply reached it - the whole wait, as the user hears it - a line of JSON each, which the report doesn't summarise.

If something isn't working, [docs/troubleshooting.md](docs/troubleshooting.md) goes through it by symptom.

## Developing

The tests run anywhere - on a PC without a microphone, speaker or any of the heavy packages - since `tests/conftest.py` stands in for whatever isn't installed. The few tests which start a fresh Python are the exception, so `python-dotenv` has to be really installed:

```bash
pip install pytest ruff numpy soxr soundfile pyyaml requests python-dotenv
```
```bash
python -m pytest
```
```bash
ruff check .
```

Both also run on GitHub for every push (`.github/workflows/checks.yml`), along with a check of both locks - `requirements.lock`, the satellite's, and `requirements-backend.txt`, the brain's - for packages with known security problems, which is also run once a week, as those turn up without the code changing. The scripts in `scripts/manual/` need real hardware or accounts, so they're run by hand, and `scripts/soak_monitor.py` keeps an eye on a long-running assistant - [docs/release-checklist.md](docs/release-checklist.md) names each and says when to use it. Before changing the speech-to-text model, record yourself with `scripts/record_samples.py`, to measure the models on. `scripts/record_wake_samples.py` records "Hey Nova", a film and you talking, to train a voice check on - a check that it's your voice saying "Hey Nova", to tell it from a film's - which isn't built yet.

When picking work back up after a break, start with [docs/roadmap.md](docs/roadmap.md).
