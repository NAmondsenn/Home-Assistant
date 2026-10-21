# 2. One speaker, which every sound goes through

19 October 2026 · BUG-07, BUG-18, BUG-36, UX-03

**Context.** Every sound was a blocking write to a stream of its own. Nothing could be stopped part way - "stop" to an alarm waited for its 34 second sound - the microphone heard timers as the user still talking, and two threads could open streams at once. Closing a stream also threw away whatever was still buffered, which swallowed the 0.14s listening sound whole.

**Decision.** `speaker.py` opens one output stream at startup and mixes whatever is playing; each sound is a Playback which can be waited for or stopped. The wake word silences every timer and alarm sound at once. The clock only decides what should ring, and each round plays on a thread of its own, one announcement at a time. Announcements pause the music too, through the same counted hold as a conversation.

**Consequences.** Stopping is immediate, give or take the output's 125ms buffer. Talking over Nova's own reply (barge-in) still isn't possible - that needs echo cancellation, and the wake word listening while Nova speaks.
