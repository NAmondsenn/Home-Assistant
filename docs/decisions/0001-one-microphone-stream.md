# 1. One microphone stream, never closed

19 October 2026 · PERF-04, BUG-25

**Context.** The wake word and the recording each opened the microphone for themselves, through two different libraries, and nothing listened in between - so words said straight after "Hey Nova" were lost ("It's time to..."). Unplugging the microphone left the old stream blocked for good. PortAudio's default input buffer also held only one of the Pi's 125ms bursts of audio, and lost about a fifth of it.

**Decision.** `microphone.py` opens one sounddevice stream at startup and keeps the last 10 seconds at 16kHz. Everything that listens reads from it at its own place, and a recording starts 0.3s before the wake word was detected. A stream which goes quiet is reopened. Silero decides what's speech, not loudness.

**Consequences.** Nothing is lost between stages, and nothing needs resampling after capture. The end of "Nova" is sometimes in the recording, so it's stripped from the transcription. This is also the "audio frames in" boundary a satellite will need.
