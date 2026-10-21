# 3. Everyday commands are handled before Claude, strictly

19 October 2026 · BUG-32, BUG-14

**Context.** Every request went to Claude first, so a timer or a pause paid for a round trip to the cloud, and none of them worked with the internet down. Claude was also left to do clock arithmetic for "remind me at 4pm".

**Decision.** `intents.py` handles a short list of commands itself - timers by length, pause, resume, skip, previous, volume, the time and date, the weather, and cancelling the only timer - but only when the whole request is exactly one of them. Anything with more to it, and any answer to a question Nova asked, still goes to Claude. Clock times are passed to the clock as times ("16:00"), and worked out there.

**Consequences.** The basics are quicker, and work offline. The patterns are deliberately narrow, so a near-miss falls through to Claude rather than being misread - widening them should come with tests for what they mustn't catch. Local exchanges still go into Claude's history, so follow-ups make sense.
