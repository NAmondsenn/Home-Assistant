# 0012: Music fetched by voice

**Context.** Nova plays music from Navidrome, which has only what is in the user's library. When something is missing, the user opens SoulSync himself, finds it and downloads it. SoulSync searches Soulseek (through slskd), picks a match, tags it and files it straight into the folder Navidrome scans, so a finished download needs no moving. It has an HTTP API under `/api/v1/`, switched on in the user's install and guarded by an API key (a request without one gets `AUTH_REQUIRED`). For albums, SoulSync already prefers the explicit edition of a release; for single songs it avoids live, remix, acoustic, instrumental and radio-edit versions, but does not tell clean from explicit.

Home Assistant has nothing that can request music: its Lidarr integration is sensors only, and no integration offers a service for SoulSync, slskd or Lidarr. So this is a case where Nova talks to the service itself, as decision 0008 allows when Home Assistant can't do the job.

What the user wants, in his words: "hey nova play ...., then if its not downloaded it asks me to download, or hey nova download ...", with a beep and "... requested" when it starts and another beep and "... has been downloaded" when it is done.

**Decision.**

1. **Two ways in.**
   - "Play X", when Navidrome has nothing for X: instead of today's "I couldn't find X on Navidrome.", Nova says it hasn't got X and asks whether to download it. A yes requests it (as in 2); a no, or no answer, ends it there.
   - "Download X", "get me X", "download the album X by Y": requested straight away, without a question.
   - "Download X" when Navidrome already has the song X: "You've already got X.", and nothing is requested. If what it has is a clean version, Nova says so and offers to download the explicit one; a yes requests it. (Builder's call.)
   - "Download the album X" when Navidrome has some or all of it: it is requested all the same, as the library may hold only a few of its tracks (SoulSync files each song it fetches under its album). SoulSync queues only the tracks it hasn't got, and if it queues none, Nova says "You've already got X.", named as Navidrome has it - as an announcement once SoulSync answers, a few seconds later, rather than straight away. A clean edition is offered as for a song. (The user's call, 10 Nov 2026: "Fetch the rest".)
   - The yes to an offer is heard with the strict yes and no words that confirm a PC shutdown, never with the clock's offer answers, which take "stop" or "thanks" as a yes.
   - The offer can be switched off on the settings page, under Music: "Offer to download what Navidrome hasn't got", on by default. "Download X" works either way.
   - A download needs "download" or "get" in the user's own words, in what he just said or the exchange before it (so "Gooba" answering "What would you like me to download?" counts, though an exchange which ended in an offer doesn't, or "play Get Lucky" would make "okay" a yes), or a plain yes to an offer. Anything else Claude tries - taking "okay" to an offer for a yes - becomes the offer again, for a plain yes. The settings page's Play never offers a download, as nothing there can hear a yes. (Found while building.)

2. **Songs and albums, never a whole artist.** What is asked for is a song (a title, perhaps with an artist) or an album (a title, perhaps with an artist). When it isn't clear which, it is a song. A request for everything by an artist is refused, with something like "I only download songs and albums, not a whole artist." Why: the user said a whole discography "could go wrong", and it can mean dozens of albums.

3. **Through SoulSync, called directly by the brain.** The installed SoulSync (checked 8 Nov 2026, from its code) has what this needs:
   - Songs: `POST /api/v1/request`, which takes a `query` ("Artist - Title") and optionally `title`, `artist`, `duration_ms` and a `notify_url`. Progress comes from `GET /api/v1/request/<id>`, and `GET /api/v1/downloads` lists what is downloading.
   - Albums: its MCP tools (`POST /mcp`), since the REST API has no album request. `find_album` searches, `request_album` puts the tracks SoulSync doesn't already have, or isn't already getting, on its wishlist, and `process_wishlist` starts them downloading now rather than at the wishlist's next run. SoulSync gives an album no id to follow it by, so Navidrome alone says how it is getting on. It also has a Lidarr-compatible API under `/lidarr/api/v1/`, which wasn't needed. No other service is needed.
   - The MCP tools only answer while MCP is switched on in SoulSync's settings ("Answer MCP requests at /mcp"). With it off, Nova says "SoulSync's MCP is off, so it can't download albums. Turn it on in SoulSync's settings." The key must be an admin-scope one, made in SoulSync's settings: a key which can only read can't request anything. (Found while building.)
   - SoulSync changes often, so the builder confirms the request and response shapes against the running version, read-only, with the key, before writing the client.

4. **The original, normal release - never a clean version.**
   - Nova asks for the plain title, never a live, remix, acoustic, sped-up or other version, unless the user named one ("play the live version of ...").
   - Where SoulSync's API lets the caller steer the choice (words to avoid, a candidate to pick, a preference for explicit), Nova uses it to avoid clean, edited and radio-edit versions. As built, an album's edition is picked by Nova itself from `find_album`'s candidates, as below. A song request takes only a title and artist, so there is nothing to steer with, and the check after it lands is what stops a clean version being called downloaded.
   - Once it has landed, Nova checks what Navidrome holds. Navidrome reports each track as explicit, clean or unmarked (OpenSubsonic's `explicitStatus`, from the files' tags).
     - A track counts as clean if Navidrome marks it clean, or if it is unmarked but its title or its album's title calls it clean, edited, censored or a radio edit - in a bracketed part or after a spaced dash (a hyphen, an en dash or an em dash), and only when nothing but "album", "version", "edit" or "edition" follows the word there. So "(Clean Version)", "(Clean Album Version)" and "(Radio Edit)" are clean, but "(feat. Clean Bandit)" and "(Clean Bandit Remix)" are not, and Taylor Swift's "Clean" is only a title. Unmarked tracks are otherwise taken as fine, since most songs with nothing explicit in them carry no mark at all. (Builder's call, open to the user.)
     - For an album, SoulSync's candidates are judged too, and a clean edition is never chosen: "clean", "edited", "censored" or "radio edit" anywhere in a MusicBrainz note on the edition, or in its title's brackets or dashed part, rules it out - as does a live, remix or other version, unless the title asked names it. Of the rest, the closest to the title and artist asked for is chosen, and among equals an album comes before a single or EP of the same name, then an edition MusicBrainz notes as explicit.
     - A song that came down as a clean version is not reported as downloaded: Nova says "X came down as a clean version" instead, and logs it, so the user can replace it.
     - Nova never deletes files.

5. **What Nova says, and when.** Every message goes to the satellite that asked, with the existing chime before it.
   - Accepted by SoulSync: "X requested."
   - In Navidrome: "X has been downloaded." It is not played, even when the request started from "play X".
   - Not found, failed, or given up on (6): "I couldn't download X.", with the reason when SoulSync gave one.
   - These are said **at any hour**: they skip the quiet hours other announcements keep, because a request made at night means the user is awake.
   - If that satellite isn't connected when the outcome is known, the message goes to the user's phone through ntfy instead.

6. **Giving up.** Nova stops waiting after 10 minutes for a song and 30 for an album, from the request. Both are settings on the page under Music, used straight away. After that Nova says it couldn't get it and stops watching. SoulSync may carry on in the background, and anything that lands later is simply in Navidrome.

7. **"Downloaded" means Navidrome has it.** SoulSync reporting a request done is not enough: Nova looks the song or album up in Navidrome by title and artist, every 15 seconds, and only counts what wasn't there when the request was made.
   - Navidrome's own file watcher picks new files up, so Nova asks it to scan only in case it hasn't, and only once: 60 seconds after SoulSync says done if Navidrome still hasn't got it, or - as SoulSync never says an album is done, and can leave a song "downloading" - shortly before the time limit, so the last look sees what it found. A scan Navidrome refuses, because Nova's user isn't an admin, is logged and the watch carries on.
   - An album is downloaded once the tracks SoulSync queued have landed, not every track of the album: SoulSync skips the ones the library already has or is already getting, so with two of Rumours' eleven in the library, nine is all of it. Tracks are counted as distinct titles - a clean and an explicit copy of one song are one track, a live or remix take is its own. If SoulSync queues nothing, there is nothing to wait for, and Nova says at once "You've already got X." if Navidrome had any of it other than a clean version, or else "I couldn't download X. SoulSync says it already has every track, or is already getting them." These were the builder's calls:
     - If only some had landed when Nova gave up: "Only N of the M tracks of X came down", M being the number queued.
     - If some came down clean: "X has been downloaded, but N of its tracks came down as clean versions."

8. **In the background.** Waiting never holds up a conversation. Several requests can be waiting at once, each watched on its own. A request still waiting when the brain restarts is forgotten, and that is logged.

9. **The settings page, not files.** SoulSync's address goes under Music. Its API key, which the user makes in SoulSync's own settings, goes under Keys as `SOULSYNC_API_KEY` in the brain's `.env`, saved and never shown back. With no address or key, "download X" says downloading isn't set up, and "play X" for something missing says what it says today.

**Not part of this.**
- Recommending music, which waits for the AI machine.
- Choosing between versions by voice.
- Starting playback when a download lands.
- Lidarr itself, the separate app, as a second route. SoulSync's own album tools cover albums.
- Replacing the clean versions already in the library. That is a one-off clean-up of the library, done separately.

**Must not happen.**
- A clean version reported as downloaded.
- A download nobody asked for: the offer after "play X" needs a yes.
- Nova talking about downloads at night unprompted: only the outcome of the user's own request is said at night.

**Left to the builder.**
- SoulSync's endpoints and their exact payloads, checked against the running version.
- How "nothing for X" is told apart from several possible matches.
- How the yes after the offer is heard. The clock's offers already handle one.
- How often to ask SoulSync for progress.
- How an artist-only request is recognised.
- The exact words, beyond those fixed above.

**Tests** fake SoulSync and Navidrome, as they fake Home Assistant now. Nothing in the repository names the user's hosts or holds a key.

**Hardware.** None. The brain only sends a few small HTTP requests while a download is waiting.
