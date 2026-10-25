# 6. The Pi as a light satellite, with PVE2 as the brain

22 October 2026 · Phase 5 · Agreed with the user, not yet built · Replaces 0004's stage 2 and its standalone mode

**Context.** 0004 moved speech-to-text to PVE2 and kept everything else on the Pi, with a full standalone mode for when PVE2 is down. The user wants the opposite balance: the Pi is a light satellite, and PVE2 is the brain that "should do all the heavy lifting". Their reasons:

- Nova should run on a smaller, cheaper Pi. How small isn't known yet.
- More satellites, and the Windows and Mac clients of Phase 6, will share the one brain.
- PVE2 is where their own always-on assistant will grow later. It will know them, work with their email and homelab, and do things like maintaining the servers.

They want "the best for the future, most efficient, fastest, and most secure, stable version". A fallback for when PVE2 is down is "nice", but "only worth it if it comes at very little hardware cost".

On the Pi today, Nova uses 627 to 677MB of memory (measured in Phase 2's soak). It runs:

- the microphone and echo cancelling;
- the wake word and the end of speech;
- Piper, and Moonshine as a fallback;
- Claude, Home Assistant, the clock and the music;
- the settings page and ntfy.

PVE2, looked at today:

- An i5-8400 with 6 cores and 31GB of memory, of which 23GB is free. Intel graphics only.
- Container 107, `nova`:
  - unprivileged, with 4 cores and 2GB of memory, about 1GB of it used by Nemotron;
  - a 4GB disk with 2.1GB free;
  - starts on boot, and is in the weekly backup of every guest (Sundays, 03:30).
- `local-lvm`, which holds every container's disk, is 88% full: 61GB holding 99GB promised to ten disks.
- `library` (/mnt/library, a disk of its own) has 135GB free.

**Goals**

- The Pi does only what has to happen where the user is: hearing, the wake word, playing sound and stopping it.
- PVE2 does everything else, for any number of satellites, in one place.
- No slower than today, with stopping as instant as it is now - measured, not assumed.
- No secrets on the Pi but its own satellite password.
- A small fallback that costs the Pi almost nothing.
- Room for the future: more satellites, push-to-talk clients, and the user's own model in Claude's place.

**Not now**

- A local language model.
- The Phase 6 clients.
- Ready-made satellites (Home Assistant's Voice PE, ESP32 boxes).
- Compressed audio.
- The rest of Phase 5 - the battery levels, the PC, fewer false wakes. They come after this, on the brain.

**Would make it a failure, even if it worked**

- Slower replies, or a "stop" that isn't instant.
- An alarm that doesn't ring because PVE2 is down.
- Keys or the user's data on the Pi.
- Anything reachable from outside the tailnet.
- A new container, or `local-lvm` filling up.
- The user's words or Nova's answers published to Home Assistant. 0005's rule stands.
- Two copies of anything that can drift apart: one codebase, and one source of truth for each thing.

## Decision

**1. The split.**

| The satellite (the Pi) | The brain (PVE2, container 107) |
| --- | --- |
| The microphone, its pre-roll, echo cancelling | Speech-to-text - Nemotron, as now |
| The wake word | Deciding when the user has stopped talking |
| The speaker: the one owner of what plays - speech, chimes, music - with ducking and volume | The conversation: everyday commands, Claude, Home Assistant, the calendar, weather, memory, routines |
| Stopping what's playing, at once | Nova's voice - Piper, in Nova's voice |
| Ringing timers and alarms, from its own copy | Timers and alarms - the one list |
| raspotify, the speaker Spotify plays to | Music: choosing it; Navidrome's decoded and sent to the satellite; Spotify driven through its Web API |
| Mute | The settings page, ntfy both ways, Nova's devices in Home Assistant |
| A recorded "I can't reach the server" | Every key, and all the user's data |

**2. The connection.** The one that exists, in `voice_assistant/protocol.py`: one connection per satellite, over the tailnet, with each message a line of JSON followed by any audio. It grows to carry things both ways:

- **From the satellite:** the wake word heard, the microphone's audio (16kHz, 16-bit, as now), what's started or stopped playing, its volume and mute, and pings.
- **From the brain:**
  - stop listening;
  - audio to play - speech, chimes or music, each a stream that can be stopped by its id;
  - stop, duck, or set the volume;
  - the list of timers and alarms;
  - the recorded fallback phrase.

Audio goes as it is, uncompressed. Speech at 22,050Hz is 44KB a second and music in stereo 176KB, which is nothing on the home network, and a small Pi has no codec to run. The satellite buffers enough music to ride over a blip.

**3. Proving who's who.** Today the Pi gives a password, and the Pi trusts whatever answers on the tailnet. From now on, both sides prove they know the satellite's password: a challenge and an answer, with HMAC, which needs nothing new installed. So nothing else on the tailnet can pose as the brain, which answers the question 0004 left open. Each satellite has a password of its own, so a lost Pi is shut out without touching the others. It stays reachable on the tailnet only, as now.

**4. Speed.** The wake word, echo cancelling and stopping stay on the Pi, so they stay instant. After the wake word, the satellite sends the pre-roll and the microphone at once, as it does now. The brain decides when the user has stopped. It has the audio already, and it's one fewer model on the Pi. Speech is made a sentence at a time and sent as it's made, so the first word plays as soon as it does today.

It's measured before and after, with the latency timeline (PERF-08):

- the wake word to the first word of the reply;
- how quickly a stop goes quiet;
- the Pi's memory and processor.

Those figures choose the smaller Pi.

**5. When PVE2 can't be reached.** The satellite:

- rings timers and alarms from its copy, which is updated whenever the list changes;
- stops anything ringing or playing when it hears the wake word;
- keeps volume and mute working;
- answers anything else with the recorded "I can't reach the server", which the brain makes in Nova's voice and the satellite keeps;
- keeps trying to reconnect.

raspotify plays on regardless when Spotify is used from a phone. There's no Moonshine, Piper, Claude or everyday commands on the Pi: 0004's standalone mode is removed.

**6. Several satellites.** On the brain, everything belongs to a satellite where it should: its room (its own Home Assistant device), its volume and mute, what it's playing, and the conversation in progress. A timer or alarm rings on the satellite it was set from, and the settings page shows them all. It's built and tested for any number, and run with one.

**7. Home Assistant.** One Nova device per satellite, kept by the brain over MQTT. Its controls are 0005's, unchanged to the user. A device is unavailable while its satellite isn't connected. Home Assistant's token and the MQTT password move to PVE2.

**8. Where it lives on PVE2.**

- **Container 107** - no new container. Its memory goes up from 2GB to 4GB.
- **Bigger files** (voices now, models later) on `library`, mounted into the container, so `local-lvm` doesn't grow.
- **The code and its Python packages** on the container's own disk.
- **The user's data** (memory, routines, alarms, settings and keys) on the container's disk, so the weekly backup has it.
- **The brain** runs as a service with a watchdog, as Nova does on the Pi.

**9. The code.** One codebase.

- The Pi installs only the satellite, with lighter requirements of its own: no Claude, Moonshine, Piper or Spotify client.
- The brain installs the rest, and `update.sh` updates either.
- The Pi's data and keys are moved to PVE2 once, checked there, and then removed from the Pi.

**10. The settings page** moves to PVE2 with the rest. It's still on the tailnet only and for the user's own login, at the `nova` machine's address. It gains each satellite's state.

**11. The future.**

- Claude stays behind `llm.py`, so the user's own model can take its place later without the satellites knowing.
- The user is getting a machine of its own for AI, with a graphics card. Hearing (`speech_to_text.py`), Nova's voice (`text_to_speech.py`) and the model (`llm.py`) each stay behind their own module, so any of them can move onto that machine later, with PVE2 still the brain. Nothing for it is built now.
- The user may move the whole brain to that machine - PVE3 - later. So everything the brain is lives in container 107, and the satellites find it by its tailnet name, which lives inside the container and moves with it. Moving it is then a backup and restore, or a migrate if PVE2 and PVE3 are joined as a cluster. Only the `library` mount is tied to PVE2, and its files go on PVE3's own storage. If PVE3 is often restarted to experiment, the brain can stay on PVE2 and use PVE3's model over the network, for about a millisecond more.
- The connection is Wyoming's message shape, so a converter for ready-made satellites could be added later.

**Order.** This comes before the rest of Phase 5:

1. Measure today.
2. Build the split, and the fallback.
3. Move the data and deploy, with the user's OK for the container's memory and mount.
4. Measure again.
5. Then the battery levels, the PC and fewer false wakes. The check that it's the user's own voice runs on the brain, on the clip after the wake word.
6. Then the 72-hour soak of everything since Phase 3.

## Settled after a builder's read

Each of these is a call made in writing this, not by the user.

1. **Room on PVE2.** What the brain needs is measured on PVE2 first, in step 1, before the split is built. The container may have up to all six of PVE2's cores and whatever memory it needs, and the user's OK at deploying covers both. If PVE2's processor turns out to hold Nova back, that's a hardware limit, and the user is told.
2. **When listening ends.** At whichever comes first:
   - the brain saying the user has stopped;
   - 10 seconds of audio, the most a message carries now, checked on the satellite;
   - the connection dropping, after which the satellite plays the fallback phrase.

   While PVE2 can't be reached, the satellite doesn't stream at all.
3. **The wake word.** The satellite acts on it at once, exactly as Nova does today with replies, music and ringing, and then tells the brain. The satellite owns what plays, so a command from the brain for a stream that has already stopped is ignored. While PVE2 is down, the wake word dismisses whatever rings.
4. **One writer for the clock.** The brain owns the list of timers and alarms. The satellite only reports what happened - this rang, that was dismissed - and the brain applies it, including what happened while it was unreachable. Offline, the satellite can dismiss but not snooze.
5. **Music when PVE2 is down.** Navidrome's music stops with it - Nova is down then, which the user accepted. Spotify is driven by the brain through its Web API, exactly as the Pi drives it today: pausing it for announcements, and its volume. raspotify carries on by itself.
6. **The network.** Container 107 is on the home network and reaches Home Assistant's broker and web address directly, as the Pi does now (checked on 22 October). Nothing new is opened.
7. **Passwords.** The brain makes a satellite's password when the satellite is added, and keeps it with its other keys. The brain holds every secret anyway, so keeping these as they are, which HMAC needs, is accepted. The Pi gets its own at install, and removing it on the brain shuts that satellite out. A second connection from the same satellite replaces the first.
8. **The move.** The Pi's data is copied to PVE2, not moved, and the brain is checked. Only then does the Pi become a satellite. Nova's old version and data stay on the Pi for a week, as the way back - switching branch again - and are then deleted. A short gap while it switches over is fine.
9. **The fallback phrase.** A default recording in Nova's voice ships with the code, so a new Pi has it before it has ever connected. The brain's replaces it.
10. **Timing.** Measured on the Pi, which sees both the wake word and the first sound of the reply, so there are no clocks to keep in step. The brain logs its own steps alongside.
11. **Routines at a set time** play on the satellite chosen when the routine was made - for now, the only one.

## Left to the builder

- The messages' names and fields.
- How much music the satellite buffers.
- How the satellite's copy of the alarms is kept in step.
- How the end of speech is found on the brain - Silero, as on the Pi now, or Nemotron's own.
- How the `library` mount is set up for an unprivileged container.
- How the satellite and the brain are installed.
- How the move of the Pi's data is checked.

## Calls made in writing this, not by the user

- Uncompressed audio both ways.
- Both sides proving the password, with one password per satellite.
- The brain, not the Pi, deciding when the user has stopped talking.
- Navidrome's music decoded on PVE2 and sent to the Pi, so the Pi needs no Navidrome password and no decoder.
- A timer or alarm rings on the satellite it was set from.
- The wake word, while PVE2 is down, stops whatever's ringing.
- The container's memory goes to 4GB, and bigger files go on `library`.
- The settings page moves to PVE2, so its address changes.

## Shaped by hardware - and what would lift it

The user would rather add hardware than settle, so every call made here because of a hardware limit is listed, with what would remove it:

- **Bigger files go on `library`, not the container's own disk,** because `local-lvm` is nearly full (below).
  - With more space for `local-lvm` - a bigger or second SSD - everything could sit on the container's disk, in the same backup, as one thing.
- **Speech-to-text runs on PVE2's processor (Nemotron),** because PVE2 has no NVIDIA graphics card.
  - It hardly matters now: the words are ready 0.29s after the user stops, with 5.1% of words wrong. The reply's real wait is Claude, about a second.
  - A graphics card might bring that under 0.1s and a few points fewer wrong. That's an estimate, not a measurement.
- **No local language model; Claude stays,** for the same reason.
  - The user's own always-on assistant needs a graphics card with plenty of memory - 16GB or more, for a model that can reason and act at talking speed - or a machine of its own.
  - Nothing in this design is in its way: the model sits behind `llm.py`.
- **The fallback is small, with no speech-to-text or voice on the Pi,** because the user wants the Pi as small and cheap as possible.
  - That's the user's call, not a limit forced on them.

Nothing else here is shaped by hardware. Uncompressed audio, the password check and the wake word staying on the Pi are for speed, security and privacy.

## Separate, and worth sorting soon

`local-lvm` is 88% full and promises 99GB on 61GB. If it fills, every container on it - Jellyfin, Immich, Navidrome and Nova among them - can stop or lose writes. That needs one of three things:

- shrinking what's promised;
- moving a disk or two to another storage;
- adding space.

Nova's part of this design adds nothing to `local-lvm`.

## Consequences

- The Pi gets much lighter, and that's measured.
- Everything about the user is in one place on PVE2, backed up weekly, and nothing secret is on the Pi.
- A second satellite, or a Phase 6 client, joins the brain rather than copying it.
- With PVE2 down, Nova only rings, stops and says it can't reach the server. The user accepted that ("idrc if that means the pi is offline").
- 0004's standalone mode, and its "the clock never leaves the Pi", are replaced: the brain holds the clock, and the satellite holds a copy to ring from.
