# Release checklist

Run through this on the Pi whenever a branch is put on it, before relying on it. It takes about 15 minutes. Jot down roughly what time anything odd happens, so it can be matched against the log.

## Putting a branch on the Pi

```bash
git fetch && git switch <branch> && bash update.sh
```

`update.sh` brings the branch up to date, installs `requirements.lock`, fetches any new models, rewrites the service file and restarts Nova, then shows the end of the log. It should end with `Nova ready!` and `Listening for 'Hey Nova'...`, with no errors above. To go back, switch to the previous branch or tag and run it again.

## Hearing you

1. In one breath: "Hey Nova what time is it". The whole question is answered, with the time said like "8:15 PM".
2. "Hey Nova", wait for the blip, then "what's the date?"
3. Straight after the blip, quickly: "set a timer for two minutes".
4. With a pause mid-sentence: "Hey Nova, remind me in ten minutes... to check the oven". It shouldn't cut you off at the pause.
5. Quietly, from across the room.
6. With a fan, a tap or instrumental music on: it stops listening about a second after you finish.
7. "Hey Nova", then say nothing. After a few seconds there's a falling blip, and it's done.
7a. Ask something with a two-sentence answer ("how far away is the moon?"): it starts speaking noticeably sooner than it used to, and the second sentence follows without a gap.
7b. The moment a reply finishes, "Hey Nova" again: it's heard first time.
7c. Part way through a long answer, "Hey Nova, stop": it stops talking almost at once - in well under a quarter of a second - and says nothing more. "Hey Nova, what's the date?" part way through another one: it stops, and answers that instead.
8. Talk normally nearby for a few minutes without saying "Hey Nova". Note any false wake-ups.

## Music

With Navidrome (`music.player: navidrome`), and again with Spotify if you still use it:

8a. "Hey Nova, play Fleetwood Mac" - "Playing Fleetwood Mac" is said first, then the music starts. "Play the Rumours album", "play my liked songs", "play some jazz".
9. "Hey Nova, pause the music" - chime, and it pauses.
10. While it plays: "Hey Nova, skip this song".
11. While it plays: "Hey Nova, what time is it?" - the music pauses, the answer, the music comes back. Said at your normal volume from where you'd usually be, "Hey Nova" is heard over the music (echo cancelling, Navidrome only).
12. While it plays: "Hey Nova, turn it down" - the music, not Nova, gets quieter.
13. While it plays, let a reminder go off - the music pauses for it.

## Timers, reminders and alarms

14. A one-minute timer; when it rings, "Hey Nova, stop". It stops straight away, not at the end of its sound - and the log says "Dismissed by voice without asking the model", even if it was heard as something like "bus stop" or "soap".
15. "Hey Nova, remind me in two minutes to drink water" - later, "This is a reminder: drink water". Between its rounds, ask something else: the answer ends "Shall I turn it off?" - "Yes, I don't need it any more" turns it off ("not yet" would leave it).
16. "Hey Nova, remind me at" a time a few minutes ahead - it goes off at that minute.
17. An alarm a couple of minutes ahead - it rings in bursts, getting louder. "Hey Nova, stop" in a gap stops it straight away.
18. "Hey Nova, set a reminder", then answer its question without saying "Hey Nova".

## Everything else

19. "Hey Nova, what's the weather?" (with `weather.location` set).
19a. "Hey Nova, turn off the kitchen lights" (whatever yours are called in Home Assistant) - chime, and they go off. "Set the living room lights to 30 percent." "Unlock the front door" - it refuses.
19b. "Hey Nova, what have I got on Thursday?" and "am I busy this weekend?" - read from your Google Calendar, with the times. "What have I got on this week?" - an overview without the times, in about 15 seconds. "When's my next shift?" and "when's my MOT?" (or anything else on it). Anything at midnight is said as "midnight", not "12 AM".
19c. "Hey Nova, remember that my favourite colour is green" - "I'll remember that". "What do you know about me?" includes it; "forget my favourite colour" - and it's gone.
20. Unplug the microphone for 5 seconds, plug it back in, wait 10, then "Hey Nova" - it works without a restart.
21. Unplug the network cable, wait 10 seconds, then "Hey Nova", wait for the blip, "set a one minute timer" - it's set straight away. "Hey Nova, how far away is the moon?" - straight away, it says it can't reach the internet. Plug the cable back in.
21a. With the network cable out: "turn on the lamp" says straight away that it can't reach Home Assistant, and Navidrome that it can't reach Navidrome - with no network at all, neither can be. With only the internet down (the router's own connection out, the Pi still on the network), the lights and Navidrome still work.
22. `systemctl --user restart voice-assistant` - it stops within a second or two, rather than hanging for 20.
23. `kill -STOP $(systemctl --user show -p MainPID --value voice-assistant)` - within about a minute, systemd restarts it (`journalctl --user-unit voice-assistant` says "Watchdog timeout").

## Leaving it running

For a soak test, leave it running as normal for a few days, with this in another terminal, and use it as you would anyway:

```bash
venv/bin/python scripts/soak_monitor.py --hours 72
```

It samples the assistant's memory, CPU, restarts and log size every 10 minutes into `soak.csv` in `~/.local/share/nova/logs`, and says at the end whether anything grew that shouldn't have. Afterwards, the speaker shouldn't have run dry, nor the microphone overflowed - this should find nothing:

```bash
journalctl --user-unit voice-assistant | grep -E "ran dry|overflowed"
```
