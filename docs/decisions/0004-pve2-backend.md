# 4. PVE2 as a backend, with the Pi keeping everything that has to be instant

20 October 2026 · Phase 4 · Proposed - stage 1's model still to be chosen

**Context.** Phase 4 moves the heavy work off the Pi and onto PVE2. A look at PVE2 found an Intel i5-8400 (6 cores, AVX2), 31GB of memory with about 24GB free, and no NVIDIA graphics card - just the processor's own Intel graphics. It already runs six containers (Jellyfin, Immich, Navidrome and others) and is mostly idle. So there's no Parakeet on a graphics card, and no local language model quick enough to replace Claude. What PVE2 does have is a faster processor for speech-to-text, and room for the things several devices will share later.

Home Assistant's way of doing this was considered first. Wyoming, and the ESPHome-based Linux Voice Assistant which replaced its Pi satellite in January 2026, treat the Pi as a microphone and a speaker, with Home Assistant as the brain. Timers live in Home Assistant, so they'd stop with it. Echo cancelling is left to the microphone's own hardware, and nothing works when the server is down. Nova would lose its echo cancelling, its offline commands and its alarms to fit.

**Decision.** Nova keeps a protocol of its own between the Pi and PVE2: one connection over the tailnet, with each message a line of JSON, followed by any audio. That's Wyoming's message shape, which needs no new library. Nova keeps talking to Home Assistant through Assist, as it does now.

| Stays on the Pi | Moves to PVE2 |
| --- | --- |
| Microphone, echo cancelling, wake word, end of speech | Speech-to-text - stage 1, if it wins the test |
| Speaker, music playback, talking over replies and music | The conversation - Claude, Home Assistant, calendar, weather, memory - stage 2 |
| Timers and alarms, always | Later: the settings page's API, and ntfy both ways |
| Everyday commands, Piper's voice, Moonshine as the fallback | |

It's done in two stages:

1. **Speech-to-text.** Only if PVE2 measurably beats the Pi on recordings of the user's own voice. The Pi sends the recording as it's made, as it already gives it to Moonshine, and gets the words back. Moonshine on the Pi leaves next to nothing to wait for once the user stops, so PVE2 has to win on getting the words right.
2. **The conversation.** Only once something needs it in one place - a second Pi, a push-to-talk client, the settings page or ntfy. Anything that belongs to the Pi - a timer, the volume, the music, "stop" - comes back to the Pi to be done, so the clock never leaves it.

**Standalone mode** is the Nova which runs today, with the backend switched off. The Pi checks PVE2 every few seconds. If it isn't answering, requests go straight to Moonshine and Claude from the Pi. While PVE2 is working, Moonshine isn't kept loaded - it's loaded when PVE2 stops answering, so the first request after that waits a few seconds, and the rest don't. If a request to PVE2 is sent but not answered within about a second, the same recording is transcribed on the Pi instead. Timers, alarms and everyday commands never wait on PVE2.

**Consequences.** Standalone isn't a second assistant - it's the same code, so it can't drift from the backend. The Pi stops being the bottleneck for speech-to-text without becoming a dumb speaker. Two questions are left for stage 2: where memory lives when the Pi is on its own, and how the backend proves to the Pi that it's PVE2 beyond being on the tailnet. If the test shows PVE2 isn't worth it, stage 1 is skipped. Moonshine on the Pi now works out the words while the user is still talking, which on the Pi left 0.24s to wait once the recording stopped, rather than 0.74s. On 38 recordings of the user's own voice, Parakeet got 6.8% of words wrong where Moonshine got 14-16%, so on getting the words right PVE2 wins. On PVE2's processor, though, Parakeet took 0.58s once the recording had stopped - it only starts then - against 0.24s for Moonshine on the Pi.

Once Nova is a satellite, it may move to a smaller Pi, freeing this Pi 5 for something else. Whatever it moves to still has to run standalone mode - the wake word, echo cancelling, Piper and Moonshine - so its memory and speed are measured on the current Pi before a smaller one is chosen.
