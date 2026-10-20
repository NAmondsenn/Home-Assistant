# Release checklist

Run through this on the Pi whenever a branch is put on it, before relying on it. It takes about 15 minutes. Jot down roughly what time anything odd happens, so it can be matched against the log.

## Putting a branch on the Pi

```bash
git fetch && git switch <branch> && git pull --ff-only
```
```bash
venv/bin/pip install --no-deps -r requirements.lock
```
```bash
systemctl --user restart voice-assistant && journalctl --user -u voice-assistant -n 30
```

The log should end with `Nova ready!` and `Listening for 'Hey Nova'...`, with no errors above. If the service file changed (`voice-assistant.service`), rebuild it first - `setup.sh` does that.

## Hearing you

1. In one breath: "Hey Nova what time is it". The whole question is answered, with the time said like "8:15 PM".
2. "Hey Nova", wait for the blip, then "what's the date?"
3. Straight after the blip, quickly: "set a timer for two minutes".
4. With a pause mid-sentence: "Hey Nova, remind me in ten minutes... to check the oven". It shouldn't cut you off at the pause.
5. Quietly, from across the room.
6. With a fan, a tap or instrumental music on: it stops listening about a second after you finish.
7. "Hey Nova", then say nothing. After a few seconds there's a falling blip, and it's done.
8. Talk normally nearby for a few minutes without saying "Hey Nova". Note any false wake-ups.

## Music

9. Play Spotify, then "Hey Nova, pause the music" - chime, and it pauses.
10. While it plays: "Hey Nova, skip this song".
11. While it plays: "Hey Nova, what time is it?" - the music pauses, the answer, the music comes back.
12. While it plays: "Hey Nova, turn it down" - the music, not Nova, gets quieter.
13. While it plays, let a reminder go off - the music pauses for it.

## Timers, reminders and alarms

14. A one-minute timer; when it rings, "Hey Nova, stop". It stops straight away, not at the end of its sound.
15. "Hey Nova, remind me in two minutes to drink water" - later, "This is a reminder: drink water".
16. "Hey Nova, remind me at" a time a few minutes ahead - it goes off at that minute.
17. An alarm a couple of minutes ahead - it rings in bursts, getting louder. "Hey Nova, stop" in a gap stops it straight away.
18. "Hey Nova, set a reminder", then answer its question without saying "Hey Nova".

## Everything else

19. "Hey Nova, what's the weather?" (with `weather.location` set).
20. Unplug the microphone for 5 seconds, plug it back in, wait 10, then "Hey Nova" - it works without a restart.
21. Unplug the network cable, wait 10 seconds, then "Hey Nova", wait for the blip, "set a one minute timer" - it's set straight away. "Hey Nova, how far away is the moon?" - straight away, it says it can't reach the internet. Plug the cable back in.
22. `systemctl --user restart voice-assistant` - it stops within a second or two, rather than hanging for 20.

## Leaving it running

For a soak test, leave it running as normal for a few days, with this in another terminal, and use it as you would anyway:

```bash
venv/bin/python scripts/soak_monitor.py --hours 72
```

It samples the assistant's memory, CPU, restarts and log size every 10 minutes into `logs/soak.csv`, and says at the end whether anything grew that shouldn't have.
