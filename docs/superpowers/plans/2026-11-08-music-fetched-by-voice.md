# Music fetched by voice - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "Download X", or a yes after "play X" finds nothing, asks SoulSync for the song or album, and Nova says on the satellite that asked when it was requested and when Navidrome has it - or that it couldn't get it.

**Architecture:** `voice_assistant/soulsync.py` talks to SoulSync: songs through its REST API, albums through its MCP tools. `voice_assistant/downloads.py` holds what Nova does with a download: the checks before it (set up, artist, already got, clean, already waiting), the offer after "play X", and a watch on a thread of its own for each request, which polls SoulSync and Navidrome until the song or album lands or the time limit passes. The brain makes one `Downloads` and has it say each message on the asking satellite, or send it to the phone; the tools Claude calls are `download_music` and, for a yes, `request_download`. "Download X" goes to Claude, as "play X" does - there are no local rules for it.

**Tech Stack:** Python 3.13, requests, pytest. SoulSync's HTTP API (`/api/v1/`) and MCP (JSON-RPC over `POST /mcp`); Navidrome's Subsonic API with OpenSubsonic's `explicitStatus`.

**Spec:** `docs/decisions/0012-music-fetched-by-voice.md`. Albums go through SoulSync's MCP tools `find_album` and `request_album`, not its Lidarr-compatible API: those tools are made for one album at a time, where Lidarr's model adds and monitors an artist before its albums, which risks the whole discography point 2 rules out.

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, key, user name or recorded response holding the user's paths in it. The key is never logged: a failed request logs only the kind of error, as Navidrome's do.
- X, in what's said, is the title and artist as asked - `split_by` on Claude's query, "Gooba by 6ix9ine", or the title alone - except "You've already got X.", which names what Navidrome has.
- Fixed sentences, verbatim: "X requested." / "X has been downloaded." / "I couldn't download X." (then a reason sentence when there is one) / "You've already got X." / "I only download songs and albums, not a whole artist." / "X came down as a clean version." / "Only N of the M tracks of X came down." / "X has been downloaded, but N of its tracks came down as clean versions." (for one: "X has been downloaded, but one of its tracks came down as a clean version.").
- Nova's own words: "I haven't got X. Do you want me to download it?" / "You've only got a clean version of X. Do you want me to download the explicit one?" / "I'm already getting X." / "Downloading music isn't set up. SoulSync's address goes in Settings, under Music, and its key under Keys." Reasons: "SoulSync isn't answering." / "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking." / "SoulSync couldn't find it." / "SoulSync said: <its own message>."
- A download needs "download X" (or "get me X") or a plain yes to an offer. The yes is `conversation.agreed` (AGREED and DECLINED), through `ActionResult.ask_first`, as for a PC shutdown - never `Clock.answer_to_offer`. Never a whole artist. Nova never deletes files and never plays what landed.
- Every download message goes to the satellite that asked through `Session.announce` (its `announcement_sound.wav` first, as alerts have it), at any hour: nothing in this feature reads `Ntfy.quiet`. With that satellite away, or the announcement not said, it goes to `ntfy.phone`. Nothing is said about downloads but the outcome of the user's own request.
- Time limits from the request: `music.song_download_minutes` 10 and `music.album_download_minutes` 30, read at each check, so a change on the page is used straight away. SoulSync and Navidrome are asked every 15 seconds (`POLL_EVERY`); once SoulSync says done and Navidrome hasn't got it 60 seconds later (`SCAN_AFTER`), Navidrome is asked to scan, once.
- A track is clean if Navidrome marks it `explicitStatus` "clean", or it is unmarked and its title or its album's names a clean version: "clean", "edited" or "radio edit" in a bracketed part or after " - ". A song is had clean when every match is clean; an album when any track is.
- "Downloaded" counts only tracks which weren't in Navidrome when the request was made (by Navidrome's id).
- A request still waiting when the brain stops is forgotten, and logged.
- Brain only: the satellite and the protocol don't change.
- House style: `.superpowers/sdd/2026-11-06-parked-minors/common.md` (comments, Google docstrings, British spelling, spaced " - " dashes, commit titles in plain words ending with the Co-Authored-By line). Tests on the Pi through `bash .superpowers/tools/pi_test.sh`, focused while working and the whole suite in London and `TZ_OVERRIDE=UTC` once at the end of each task, then ruff; `~/tools/node/bin/node --check voice_assistant/web/settings.js` when it changes. Tests fake SoulSync and Navidrome, never the real ones.

## Review Focus

1. **A real title with "Clean" or "Edited" in it** - Taylor Swift's "Clean", "Edited Highlights" - is its title, not a clean version: only Navidrome's mark or a bracketed or " - " part counts. Test in Task 2.
2. **What was already in Navidrome isn't the download:** an old clean copy (after the explicit offer) isn't taken for the new one, and "Rumours (Live)" isn't "Rumours" when "download Rumours" checks what's had. Tests in Tasks 2 and 4.
3. **The same thing asked for twice while it waits** makes one request to SoulSync and says "I'm already getting X." Tests in Tasks 4 and 5.
4. **A blip mid-watch** - SoulSync or Navidrome out of reach for a poll, or Navidrome refusing a scan because Nova's user isn't an admin - is logged and the watch carries on to its time limit. Tests in Tasks 2 and 4.
5. **Nowhere to say it:** the asking satellite gone or cut off when the outcome comes goes to the phone; with no phone topic either, it's logged and never raises. Tests in Task 6.

---

### Task 1: What SoulSync and Navidrome really send (live check, by the controller)

Read-only, by the controller, not an implementer. No repo change and no commit: the result goes in the ledger, `.superpowers/sdd/2026-11-08-music-fetched-by-voice/progress.md`. Task 2 doesn't need it to start; Task 3 does.

**Files:** none in the repo.

**Interfaces:**
- Produces: a ledger entry, in the template below, which Task 3's fakes and mapping follow, and which confirms Task 2's `explicitStatus` values.

- [ ] **Step 1: The key.** Ask the user to make an API key in SoulSync's settings and add it to the brain's `~/.config/nova/.env` as `SOULSYNC_API_KEY` himself (the running Nova ignores it until this is deployed). Never ask for it in chat.

- [ ] **Step 2: Navidrome.** On the brain as the nova user, with `navidrome.url`, `navidrome.username` and `NAVIDROME_PASSWORD`: `search3` for a track known to be explicit, then `getSong` on it, and `getUser` for Nova's user. Record whether `explicitStatus` is there, the values seen, and `adminRole` (Navidrome's `startScan` needs an admin).

- [ ] **Step 3: SoulSync's REST API.** With the key, `GET /api/v1/downloads` and `GET /api/v1/request/<id>` for an id found there; the same without the key. Record the fields and their types, the words for waiting, done and failed, and where a failure's reason is.

- [ ] **Step 4: SoulSync's MCP.** `POST /mcp`: `initialize`, `tools/list` (the input schemas of `find_album` and `request_album`), and `tools/call find_album` for an album already in the library. Record whether a session header is needed, whether replies are JSON or an event stream, where the candidates are and each one's fields, any explicit flag, and whether tracks come as titles or a count.

- [ ] **Step 5: The writes, from SoulSync's code.** `POST /api/v1/request` and `request_album` are read in SoulSync's installed code (its route handler and tool), not called: their reply fields, their error replies, and how an album request's progress can be followed (an id `/api/v1/request/<id>` takes, entries in `/api/v1/downloads` tied to it, or nothing). Also any field that steers the choice (words to avoid, a preference for explicit). Call each for real only if the user names a song and an album he wants and says yes.

- [ ] **Step 6: Record, masked.** Field names, types and status words only - no host names, paths, user names or the key. Template:

```
SoulSync (checked <date>, version <v>):
- POST /api/v1/request {query, title?, artist?, ...}: reply {...}; request id at <path>; error reply <status, shape>
- GET /api/v1/request/<id>: state at <path>; waiting = [...], done = [...], failed = [...]; reason at <path or none>
- no key: <status> <shape>
- /mcp: session header <name or none>; replies <json | event stream>
- find_album {args}: candidates at <path>, each {...}; explicit flag <path or none>; tracks <titles at path | count at path | none>
- request_album {args}: reply {...}; progress <request id at path | downloads entries matched by ... | none>
- steering fields: <list or none>
Navidrome: explicitStatus <present, values>; Nova's user adminRole <true | false>
```

---

### Task 2: Navidrome - what landed, clean versions, and a scan

Independent of Task 1, so it can run while the user makes his key.

**Files:**
- Modify: `voice_assistant/navidrome.py` (the Subsonic talking, lines 136-270, moves into a base class; `play`'s not-found reply at 371-372)
- Test: `tests/test_navidrome.py`

**Interfaces:**
- Produces:
  - `VERSIONS = ("clean", "edited", "radio edit", "live", "remix", "acoustic", "instrumental", "sped up", "slowed", "karaoke")` and `CLEAN_VERSIONS = ("clean", "edited", "radio edit")`.
  - `version_of(name: str) -> Optional[str]` - the first of `VERSIONS` named, as whole words, in a bracketed part of `name` or after a spaced " - ", else None. `marked_clean(name: str) -> bool` is `version_of(name) in CLEAN_VERSIONS`.
  - `is_clean(track: Dict) -> bool` - the Global Constraints' rule, from `explicitStatus`, `title` and `album`.
  - `class NavidromeLibrary` with `__init__(self, url: str, username: str, password: str)`, and `_sign_in`, `_call`, `_failure_message` (static), `_search`, `_album_songs` moved up from `NavidromePlayer` unchanged.
  - `NavidromeLibrary.find_songs(title: str, artist: Optional[str]) -> List[Dict]` - songs whose title has every word of `title` (`likeness` 2 or more), by the artist when one is given (likeness 2 or more), leaving out one marked as a version other than a clean one unless `title` names it. Searched as `_find_song` does, the title alone when title and artist find nothing.
  - `NavidromeLibrary.find_album_tracks(title: str, artist: Optional[str]) -> List[Dict]` - every track of each album matching by the same rule, each with its `album`.
  - Both raise `requests.RequestException` or `NavidromeError`, as `_call` does.
  - `NavidromeLibrary.start_scan() -> bool` - `startScan`; False on any failure, a refusal logged once at WARNING saying Nova's Navidrome user needs to be an admin for it.
  - `class NavidromePlayer(NavidromeLibrary)`, otherwise as now. `play`'s "I couldn't find X on Navidrome." reply gains `"missing": True` when a query was given.

- [ ] **Step 1: Write the failing tests**

```python
def test_clean_is_navidromes_mark_or_a_title_that_says_so():
    assert is_clean({"title": "Gooba", "explicitStatus": "clean"})
    assert not is_clean({"title": "Gooba (Clean)", "explicitStatus": "explicit"})
    assert is_clean({"title": "Gooba (Clean)"}) and is_clean({"title": "Gooba - Radio Edit", "explicitStatus": ""})
    assert is_clean({"title": "Gooba", "album": "Day69 [Edited Version]"})
    assert not is_clean({"title": "Gooba", "album": "Day69"})
    # A song called Clean is only its title.
    assert not is_clean({"title": "Clean", "album": "1989"}) and not is_clean({"title": "Edited Highlights"})


def test_songs_are_found_by_every_word_of_the_title_and_the_artist(make_library):
    # Search answers: 1 Gooba / 6ix9ine, 2 Gooba (Clean) / 6ix9ine, 3 Gooba / Other, 4 Goo / 6ix9ine.
    assert [song["id"] for song in library.find_songs("Gooba", "6ix9ine")] == ["1", "2"]
    assert [song["id"] for song in library.find_songs("Gooba", None)] == ["1", "2", "3"]


def test_a_live_version_isnt_the_song_asked_for(make_library):
    # Search answers: 5 Dreams (Live) / Fleetwood Mac.
    assert library.find_songs("Dreams", "Fleetwood Mac") == []
    assert [song["id"] for song in library.find_songs("Dreams Live", "Fleetwood Mac")] == ["5"]


def test_an_albums_tracks_are_every_track_of_each_matching_album(make_library):
    # Albums: a1 Rumours / Fleetwood Mac (3 songs), a2 Rumours (Live) / Fleetwood Mac, a3 Rumours / Other.
    tracks = library.find_album_tracks("Rumours", "Fleetwood Mac")
    assert [track["id"] for track in tracks] == ["t1", "t2", "t3"] and {track["album"] for track in tracks} == {"Rumours"}


def test_a_scan_is_asked_for_and_a_refusal_is_logged(make_library, caplog):
    assert make_library({"startScan": {"scanStatus": {"scanning": True}}}).start_scan() is True
    refused = make_library({"startScan": {"status": "failed", "error": {"code": 50, "message": "not authorized"}}})
    assert refused.start_scan() is False and "admin" in caplog.text


def test_something_missing_is_marked_so_a_download_can_be_offered(make_player):
    assert make_player().play("Nothing like it", search_type="track") == {
        "success": False, "message": "I couldn't find Nothing like it on Navidrome.", "missing": True}
```

`make_library` builds a `NavidromeLibrary("http://navidrome.local:4533/", "nova", "secret")` with `_session = FakeNavidrome(answers)`, beside `make_player`.

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_navidrome.py -x`
Expected: FAIL with "cannot import name 'NavidromeLibrary'".

- [ ] **Step 3: Implement the Interfaces above in `voice_assistant/navidrome.py`**

`find_songs` and `find_album_tracks` keep a match only when `version_of` its name is None, one of `CLEAN_VERSIONS`, or a word of `title`. Every existing test in the file must pass unchanged.

- [ ] **Step 4: Run them to see them pass, then the suite in both time zones and ruff**

Run: `bash .superpowers/tools/pi_test.sh tests/test_navidrome.py` then the full suite, then `TZ_OVERRIDE=UTC` the same, then ruff.
Expected: PASS everywhere; ruff clean.

- [ ] **Step 5: Commit** - "Find what landed in Navidrome, tell clean versions apart and ask for a scan"

---

### Task 3: The SoulSync client

**Files:**
- Create: `voice_assistant/soulsync.py`, `tests/test_soulsync.py`

**Interfaces:**
- Consumes: Task 1's ledger entry; Task 2's `likeness`, `split_by`, `version_of` and `marked_clean`.
- Produces:
  - `class SoulSyncError(Exception)` with `__init__(self, reason: Optional[str] = None)` and `.reason`, a sentence from the Global Constraints' reasons.
  - `@dataclass Progress: state: str` ("waiting", "done" or "failed") `, reason: Optional[str] = None`.
  - `@dataclass AlbumRequest: id: Optional[str], tracks: List[str], track_count: int` - `tracks` the titles SoulSync gave (empty if it gave only a count), `track_count` how many it said the album has.
  - `class SoulSync` with `__init__(self, url: str, key: str)` (address without a trailing slash, key sent as `Authorization: Bearer <key>`) and `from_config(cls, config, key: Optional[str]) -> Optional[SoulSync]` (None without `soulsync.url` or a key).
  - `request_song(title: str, artist: Optional[str]) -> str` - the request id; `song_progress(request_id: str) -> Progress`.
  - `request_album(title: str, artist: Optional[str]) -> AlbumRequest` - `find_album`, then `request_album` for the candidate `pick_album` chose; `album_progress(request: AlbumRequest) -> Progress` - however Task 1 found an album followed, else `Progress("waiting")` always (the watch then goes by Navidrome alone).
  - `pick_album(candidates: List[Dict], title: str, artist: Optional[str]) -> Optional[Dict]`.
  - All four calls raise `SoulSyncError`, never `requests` errors.

- [ ] **Step 1: Write the failing tests**

The fakes answer with Task 1's recorded shapes, kept as constants at the top of the file (`SONG_ACCEPTED`, `SONG_WAITING`, `SONG_DONE`, `SONG_FAILED`, `NO_KEY`, `ALBUM_FOUND`, `ALBUM_REQUESTED`, and the album's progress if there is one), with made-up names and ids.

```python
def test_a_song_is_asked_for_as_artist_dash_title_with_the_key():
    assert soulsync.request_song("Gooba", "6ix9ine") == RECORDED_ID
    url, body, headers = http.posted[0]
    assert url == "http://soulsync.local:8008/api/v1/request"
    assert body == {"query": "6ix9ine - Gooba", "title": "Gooba", "artist": "6ix9ine"}
    assert headers["Authorization"] == "Bearer the-key"


def test_a_title_alone_is_asked_for_as_it_is():
    soulsync.request_song("Gooba", None)
    assert http.posted[0][1] == {"query": "Gooba", "title": "Gooba"}


def test_a_songs_progress_is_waiting_done_or_failed_with_its_reason():
    assert [soulsync.song_progress(RECORDED_ID) for _ in range(3)] == [
        Progress("waiting"), Progress("done"), Progress("failed", f"SoulSync said: {RECORDED_REASON}.")]


def test_an_album_is_found_then_requested_through_mcp():
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album", "request_album"]
    assert request.track_count == RECORDED_TRACK_COUNT


def test_the_album_picked_is_the_closest_and_never_a_clean_edition():
    candidates = [_candidate("Rumours (Clean)", "Fleetwood Mac"), _candidate("Rumours (Live)", "Fleetwood Mac"),
                  _candidate("Rumours", "Someone Else"), _candidate("Rumours", "Fleetwood Mac")]
    assert pick_album(candidates, "Rumours", "Fleetwood Mac") is candidates[3]
    assert pick_album(candidates, "Rumours Live", "Fleetwood Mac") is candidates[1]
    assert pick_album(candidates[:1], "Rumours", "Fleetwood Mac") is None


def test_no_album_found_says_so():
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Nothing like it", None)
    assert error.value.reason == "SoulSync couldn't find it."


def test_a_refused_key_says_which_key_to_check():
    # Answered with NO_KEY, as SoulSync answers without one.
    assert error.value.reason == "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking."


def test_soulsync_out_of_reach_is_said_plainly_and_the_key_never_logged(caplog):
    # The fake raises requests.ConnectionError.
    assert error.value.reason == "SoulSync isn't answering." and "the-key" not in caplog.text


def test_it_is_made_only_with_an_address_and_a_key(make_config):
    assert SoulSync.from_config(make_config(soulsync={"url": ""}), "the-key") is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008"}), None) is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008/"}), "the-key").url \
        == "http://soulsync.local:8008"
```

Plus one test for `album_progress` following what Task 1 recorded (or staying "waiting" if there's nothing to follow), and one for any steering field Task 1 found, sent with clean, edited and radio-edit versions avoided.

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_soulsync.py -x`
Expected: FAIL with "No module named 'voice_assistant.soulsync'".

- [ ] **Step 3: Implement `voice_assistant/soulsync.py`**

A `requests.Session` carrying the key; `REQUEST_TIMEOUT = 10` seconds for the REST calls and `MCP_TIMEOUT = 30` for the MCP ones (`find_album` searches beyond the network). `_mcp(tool: str, arguments: Dict) -> Any` sends JSON-RPC `tools/call`, after `initialize` and with its session header if Task 1 found one is needed, reading a single `data:` line if replies come as an event stream. `pick_album` never picks a candidate `marked_clean`, picks one whose `version_of` is another version only when the title asked names it, prefers one SoulSync flags explicit among equals, and otherwise scores as `navidrome.best` does. A 401, or SoulSync's `AUTH_REQUIRED`, is the key reason; a connection error or timeout is "SoulSync isn't answering."; SoulSync's own error message is "SoulSync said: ...". Errors log their kind only, as `NavidromeLibrary._failure_message` does.

- [ ] **Step 4: Run them to see them pass, then the suite in both time zones and ruff**

Expected: PASS; ruff clean.

- [ ] **Step 5: Commit** - "Ask SoulSync for songs and albums, and how they're getting on"

---

### Task 4: Watching a download in the background

**Files:**
- Create: `voice_assistant/downloads.py`, `tests/test_downloads.py`

**Interfaces:**
- Consumes: Task 2's `NavidromeLibrary` (`find_songs`, `find_album_tracks`, `start_scan`, `_failure_message`), `is_clean`, `likeness`, `split_by`, `normalise`; Task 3's `SoulSync`, `SoulSyncError`, `Progress`, `AlbumRequest`; `music._missing(config, "navidrome")`.
- Produces:
  - `POLL_EVERY = 15`, `SCAN_AFTER = 60`, `NOT_SET_UP` (the sentence in Global Constraints).
  - `@dataclass(frozen=True) Wanted: title: str, artist: Optional[str], kind: str` ("track" or "album"); `Wanted.from_query(query: str, search_type: Optional[str]) -> Wanted` (an album only for "album"); `.name -> str` ("Gooba by 6ix9ine", or the title alone); `.key -> str` (`kind` and `normalise(name)`, for telling the same request apart).
  - `class Downloads` with `__init__(self, config, say: Callable[[Optional[str], str], None], soulsync: Optional[Callable[[], Optional[SoulSync]]] = None, library: Optional[Callable[[], Optional[NavidromeLibrary]]] = None, now: Callable[[], float] = time.monotonic, wait: Optional[Callable[[float], bool]] = None)`. By default `soulsync` is `SoulSync.from_config(config, os.getenv("SOULSYNC_API_KEY"))` and `library` a `NavidromeLibrary` from `navidrome.url`, `navidrome.username` and `NAVIDROME_PASSWORD`, each made at the time of use; `wait(seconds)` returns True once stopping, by default the wait of a stop event.
  - `problem() -> Optional[str]` - `NOT_SET_UP` without SoulSync's address or key, else `music._missing(config, "navidrome")`'s sentence, else None.
  - `start(wanted: Wanted, satellite: Optional[str]) -> bool` - False if the same `key` is already waiting; else starts a daemon thread named "download" running `_watch`, and True.
  - `_watch(wanted: Wanted, satellite: Optional[str]) -> str` - the whole of one request, returning its last message (empty if stopped).
  - `waiting() -> List[str]` - the names waiting; `stop()` - logs at WARNING "Forgot 2 downloads still waiting, as Nova is stopping: X, Y" ("1 download" for one, nothing with none waiting) and ends every watch, which then says nothing more.

- [ ] **Step 1: Write the failing tests**

`FakeSoulSync` answers progress from a list and records requests; `FakeLibrary` answers each `find_*` call from a list of results and counts scans; `FakeTime` gives `now` and a `wait` which moves it on; `said` keeps `(satellite, message)`. `GOOBA` and `GOOBA_CLEAN` are songs with ids "1" and "2".

```python
def test_a_song_is_requested_then_said_downloaded_once_navidrome_has_it():
    # SoulSync: waiting, then done; Navidrome: nothing before, nothing, then Gooba.
    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine has been downloaded."
    assert said == [("kitchen", "Gooba by 6ix9ine requested."), ("kitchen", "Gooba by 6ix9ine has been downloaded.")]
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_done_in_soulsync_isnt_downloaded_until_navidrome_has_it_and_it_scans_once():
    # SoulSync done at once; Navidrome has nothing for three minutes, then Gooba.
    assert said[-1] == ("kitchen", "Gooba by 6ix9ine has been downloaded.")
    assert library.scans == [SCAN_AFTER + POLL_EVERY]  # when, by the fake clock; once only


def test_a_song_that_came_down_clean_is_never_said_downloaded(caplog):
    assert said[-1] == ("kitchen", "Gooba by 6ix9ine came down as a clean version.")
    assert "Gooba (Clean)" in caplog.text  # logged, so the user can replace it


def test_an_old_clean_copy_isnt_taken_for_the_download():
    # Navidrome: [GOOBA_CLEAN] before, the same at the first poll, then [GOOBA_CLEAN, GOOBA].
    assert said[-1] == ("kitchen", "Gooba by 6ix9ine has been downloaded.")


def test_a_song_is_given_up_on_after_ten_minutes():
    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")
    assert 600 <= clock.given_up_at < 600 + POLL_EVERY


def test_the_time_limits_are_read_as_they_are_now():
    # music.song_download_minutes changed from 10 to 2 a minute in.
    assert 120 <= clock.given_up_at < 120 + POLL_EVERY


def test_an_album_is_downloaded_once_every_track_soulsync_named_is_in():
    # AlbumRequest(id, ["Dreams", "Go Your Own Way", "The Chain"], 3); Navidrome's grow by one a poll.
    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")


def test_an_album_with_clean_tracks_says_how_many():
    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded, but 2 of its tracks came down "
                                   "as clean versions.")
    # And with one: "..., but one of its tracks came down as a clean version."


def test_part_of_an_album_by_the_time_limit_says_how_much():
    assert said[-1] == ("kitchen", "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down.")


def test_a_failure_is_said_with_soulsyncs_reason():
    # request_song raises SoulSyncError("SoulSync isn't answering."); and, apart, progress "failed".
    assert said == [("kitchen", "I couldn't download Gooba by 6ix9ine. SoulSync isn't answering.")]


def test_a_poll_that_fails_is_logged_and_the_watch_carries_on(caplog):
    # song_progress raises SoulSyncError once and find_songs requests.ConnectionError once, then all is well.
    assert said[-1] == ("kitchen", "Gooba by 6ix9ine has been downloaded.")


def test_several_are_watched_at_once_and_the_same_one_once():
    assert downloads.start(GOOBA_WANTED, "kitchen") and downloads.start(RUMOURS_WANTED, "bedroom")
    assert downloads.start(Wanted("gooba", "6IX9INE", "track"), "kitchen") is False
    assert sorted(downloads.waiting()) == ["Gooba by 6ix9ine", "Rumours by Fleetwood Mac"]


def test_stopping_forgets_whats_waiting_logs_it_and_says_nothing_more(caplog):
    downloads.stop()
    assert "Forgot 2 downloads still waiting, as Nova is stopping" in caplog.text
    assert [message for _, message in said if not message.endswith("requested.")] == []


def test_without_soulsync_or_navidrome_it_isnt_set_up(make_config, monkeypatch):
    # Each of soulsync.url, SOULSYNC_API_KEY, navidrome.url, navidrome.username and NAVIDROME_PASSWORD missing in turn.
    assert downloads.problem() == NOT_SET_UP  # for SoulSync's two
    assert downloads.problem() == "Navidrome's address isn't set. Add it in Settings, under Music."
```

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_downloads.py -x`
Expected: FAIL with "No module named 'voice_assistant.downloads'".

- [ ] **Step 3: Implement `Wanted` and the watch in `voice_assistant/downloads.py`**

`_watch`, as the tests leave its order open:

```
before = ids of library.find_*(wanted) now (empty, logged, if Navidrome can't be asked)
started = now()
request it (song: request_song; album: request_album) - on SoulSyncError say "I couldn't download X." + reason, end
say "X requested."
until wait(POLL_EVERY) says stopping (then end, saying nothing):
    limit = the kind's minutes, read now; if now() - started >= limit: look in Navidrome once more, say the last outcome, end
    if SoulSync hasn't said done: ask its progress (a SoulSyncError is logged, and the watch goes on)
        failed: say the album's "Only N of the M..." if N > 0, else "I couldn't download X." + its reason; end
        done: done_at = now()
    look in Navidrome (a failure is logged, and the watch goes on); new = tracks whose id isn't in before
        song: any new -> "X has been downloaded." if one isn't clean, else "X came down as a clean version." (logged); end
        album: landed = SoulSync's titles with a new track of likeness 2 or more (with no titles, the new tracks,
               up to track_count); all landed -> downloaded, with the clean count; end
               (with no count from SoulSync: downloaded once it says done and one or more have landed)
    if done, not scanned, and now() - done_at >= SCAN_AFTER: library.start_scan(), once
the last outcome at the limit: as above, else for an album with 0 < N < M "Only N of the M tracks of X came down.",
    else "I couldn't download X."
```

Every message goes through `say(satellite, message)` and is logged at INFO. The thread catches any other exception, logs it, and says "I couldn't download X." so nobody waits for nothing.

- [ ] **Step 4: Run them to see them pass, then the suite in both time zones and ruff**

Expected: PASS; ruff clean.

- [ ] **Step 5: Commit** - "Watch each download until Navidrome has it, or its time is up"

---

### Task 5: Asking by voice - "download X", and the offer after "play X"

**Files:**
- Modify: `voice_assistant/downloads.py` (the entry points), `voice_assistant/actions.py` (`UNAVAILABLE` 40-52, `ActionResult` 89-148, `confirm` 151-195, `ActionExecutor.__init__` 258-296, `play_music` 427-428, two new tools after `play_playlist`), `voice_assistant/conversation.py` (the answer to an `asked_first`, 162-174)
- Test: `tests/test_downloads.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: Task 4's `Downloads`, `Wanted`, `problem`, `start`, `waiting`; Task 2's `find_songs`, `find_album_tracks`, `is_clean`, `_failure_message`, and `play`'s `"missing"`.
- Produces:
  - `Downloads.offer(query: Optional[str], search_type: Optional[str]) -> Optional[Dict]` - None unless `search_type` is None, "track" or "album", `music.offer_downloads` is on, `problem()` is None and it isn't already waiting; else `{"success": True, "message": "I haven't got X. Do you want me to download it?", "ask_first": Action("request_download", {"query": query, "search_type": search_type})}`.
  - `Downloads.download(query, search_type, satellite) -> Dict` - in order: `problem()`; "artist" refused with "I only download songs and albums, not a whole artist."; an empty query asks "What would you like me to download?"; already waiting is "I'm already getting X."; Navidrome's check (out of reach: its `_failure_message`, nothing requested); had clean, the explicit offer with `ask_first` `request_download`; had, "You've already got X." naming what Navidrome has; else `start` and `{"success": True, "message": "Asked SoulSync for X.", "later": True}`.
  - `Downloads.request(query, search_type, satellite) -> Dict` - the same without Navidrome's check, for a yes.
  - `ActionExecutor(..., downloads=None)` and `self.downloads`; `UNAVAILABLE["downloads"] = "Downloading music isn't set up."`.
  - Tools `download_music(query: Optional[str] = None, search_type: Optional[str] = None)` (`needs="downloads"`, offered to Claude) and `request_download(...)` (`needs="downloads"`, `for_model=False`), each passing `self.satellite`.
  - `ActionResult.confirm` "later": said later, as an announcement - from a reply's `"later": True`; `confirm()` neither says nor chimes it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_downloads.py
def test_downloading_with_nothing_set_up_says_so():
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["message"] == NOT_SET_UP


def test_a_whole_artist_is_never_downloaded():
    assert downloads.download("Fleetwood Mac", "artist", "kitchen") == {
        "success": False, "message": "I only download songs and albums, not a whole artist."}
    assert soulsync.requested == []


def test_something_already_had_is_never_requested():
    assert downloads.download("Gooba by 6ix9ine", None, "kitchen")["message"] == "You've already got Gooba by 6ix9ine."


def test_only_a_clean_copy_had_offers_the_explicit_one():
    reply = downloads.download("Gooba by 6ix9ine", "track", "kitchen")
    assert reply["message"] == "You've only got a clean version of Gooba by 6ix9ine. Do you want me to download the explicit one?"
    assert reply["ask_first"] == Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})


def test_a_download_is_started_and_nothing_said_until_soulsync_answers():
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": True, "message": "Asked SoulSync for Gooba by 6ix9ine.", "later": True}
    assert started == [(Wanted("Gooba", "6ix9ine", "track"), "kitchen")]


def test_the_same_thing_asked_for_while_it_waits_is_requested_once():
    assert downloads.download("gooba by 6ix9ine", "track", "kitchen")["message"] == "I'm already getting gooba by 6ix9ine."


def test_navidrome_out_of_reach_requests_nothing():
    assert downloads.download("Gooba", "track", "kitchen")["message"] == "I can't reach Navidrome right now."


def test_the_offer_is_only_for_a_song_or_album_and_can_be_switched_off():
    assert downloads.offer("Gooba by 6ix9ine", "track")["message"] == "I haven't got Gooba by 6ix9ine. Do you want me to download it?"
    assert downloads.offer("Fleetwood Mac", "artist") is None and downloads.offer("jazz", "playlist") is None
    # music.offer_downloads off, or SoulSync not set up:
    assert downloads.offer("Gooba by 6ix9ine", "track") is None


# tests/test_actions.py
def test_play_offers_a_download_for_something_navidrome_hasnt_got(make_executor):
    result = make_executor(FakeNavidromePlayer(missing=True), downloads=downloads).execute(
        Action("play_music", {"query": "Gooba by 6ix9ine", "search_type": "track"}))
    assert (result.ok, result.say) == (True, "I haven't got Gooba by 6ix9ine. Do you want me to download it?")
    assert result.ask_first == Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})


def test_play_says_what_it_says_today_with_no_offer(make_executor):
    # No downloads, or offer() giving None.
    assert result.say == "I couldn't find Gooba by 6ix9ine on Navidrome." and result.ask_first is None


def test_only_the_users_yes_can_request_without_checking(make_executor):
    assert "download_music" in names_offered and "request_download" not in names_offered
    assert executor.execute(Action("request_download", {"query": "Gooba"}, from_model=True)).say == "Sorry, I can't do that."


def test_a_result_said_later_is_neither_said_nor_chimed_now():
    assert confirm([ActionResult.from_reply({"success": True, "message": "Asked SoulSync for Gooba.", "later": True})]) == ([], [])


# tests/test_conversation.py
def test_yes_to_a_download_offer_requests_it_and_says_nothing_yet(tmp_path):
    assert conversation.handle("play Gooba by 6ix9ine").say == "I haven't got Gooba by 6ix9ine. Do you want me to download it?"
    assert conversation.handle("yes please", follow_up=True).say == ""
    assert downloads.requested == [("Gooba by 6ix9ine", "track", "kitchen")]
    assert llm.history[-1]["assistant"]  # never empty, which the API refuses


@pytest.mark.parametrize("answer", ["thanks", "stop", "no", "okay thanks"])
def test_anything_but_a_plain_yes_to_a_download_offer_requests_nothing(tmp_path, answer):
    assert downloads.requested == []


def test_an_offer_left_unanswered_requests_nothing(tmp_path):
    conversation.start()
    conversation.handle("yes", follow_up=True)
    assert downloads.requested == []


# tests/test_llm.py
def test_a_download_is_an_album_only_when_asked_for_by_name(llm):
    llm.client = FakeClient(_reply("Okay.", _tool("download_music", query="Rumours by Fleetwood Mac", search_type="album")))
    assert llm.process_query("download rumours")["actions"][0].params["search_type"] == "track"
    assert llm.process_query("download the album rumours")["actions"][0].params["search_type"] == "album"
```

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_downloads.py tests/test_actions.py tests/test_conversation.py tests/test_llm.py -x`
Expected: FAIL with "'Downloads' object has no attribute 'download'".

- [ ] **Step 3: Implement the Interfaces above**

`play_music` keeps today's reply unless it has `"missing"` and `self.downloads.offer(...)` gives one. In `Conversation.handle`'s answer to `asked_first`, a yes whose result is ok and confirms "later" replies with nothing, and `self.llm.remember(text, result.say)` keeps "Asked SoulSync for X." - a PC's yes is unchanged. `download_music`'s description, as Claude reads it:

> "Download a song or an album the user hasn't got yet, for 'download X', 'get me X' or 'download the album X by Y'. Correct obvious mis-transcriptions, and give it as 'title by artist' when you know the artist. Pass the title as the user said it: never add 'live', 'remix' or any other version unless they named it. For everything by an artist, call it with search_type 'artist' - it explains it can't."

with `query` ("What to download, spelled correctly, e.g. 'Gooba by 6ix9ine'.") and `search_type` (enum "track", "album", "artist"; "Only 'album' if the user says the word album, and 'artist' for everything by an artist rather than one song or album.").

- [ ] **Step 4: Run them to see them pass, then the suite in both time zones and ruff**

Expected: PASS; ruff clean.

- [ ] **Step 5: Commit** - "Download a song or album when asked, or on a yes after play finds nothing"

---

### Task 6: Said on the satellite that asked, at any hour, or sent to the phone

**Files:**
- Modify: `voice_assistant/brain.py` (`__init__` after the clock, ~351; `_say_download` beside `_say_battery`, ~1259; `_clean_up`, ~1542), `voice_assistant/session.py` (the `ActionExecutor` at 215-218)
- Test: `tests/test_brain.py`, `tests/test_session.py` (its `brain` fixture gains `downloads=None`)

**Interfaces:**
- Consumes: Task 4's `Downloads(config, say=...)` and `stop()`.
- Produces: `Brain.downloads`; `Brain._say_download(satellite: Optional[str], message: str) -> None` - `session_for(satellite).announce(message)`; with no session, `announce` returning False or raising, `self.ntfy.send_soon(self.ntfy.phone, message, title="Music download")`; with no ntfy or phone topic, a WARNING; never raises. The session's `ActionExecutor` gets `downloads=brain.downloads`. `_clean_up` stops the downloads before the sessions close.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_download_message_is_said_on_the_satellite_that_asked(brain, connect):
    brain._say_download("kitchen", "Gooba by 6ix9ine requested.")
    assert kitchen.announced == ["Gooba by 6ix9ine requested."] and bedroom.announced == []


def test_a_download_message_is_said_in_the_quiet_hours_too(brain, connect, monkeypatch):
    monkeypatch.setattr(brain._night, "quiet", lambda: True)  # and an ntfy whose quiet() is True
    assert kitchen.announced == ["Gooba by 6ix9ine has been downloaded."]


def test_with_its_satellite_away_a_download_message_goes_to_the_phone(brain, connect):
    # Only the bedroom connected; asked from the kitchen.
    assert brain.ntfy.sent == [("phone", "Music download", "Gooba by 6ix9ine has been downloaded.")]
    assert bedroom.announced == []


def test_a_download_message_not_said_to_the_end_goes_to_the_phone(brain, connect):
    kitchen.said = False
    assert brain.ntfy.sent == [("phone", "Music download", "I couldn't download Gooba by 6ix9ine.")]


def test_a_download_message_with_nowhere_to_go_is_logged_and_never_raises(brain, connect, caplog):
    # announce raises; no ntfy.
    brain._say_download("kitchen", "Gooba by 6ix9ine requested.")
    assert "Gooba by 6ix9ine requested." in caplog.text


def test_the_brain_stops_its_downloads_before_its_satellites(brain):
    assert order == ["downloads", "sessions"]


# tests/test_session.py
def test_a_sessions_actions_download_through_the_brain(brain, make_session):
    brain.downloads = object()
    assert make_session().actions.downloads is brain.downloads
```

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_brain.py tests/test_session.py -x -k "download"`
Expected: FAIL with "'Brain' object has no attribute '_say_download'".

- [ ] **Step 3: Implement the Interfaces above**

- [ ] **Step 4: Run them to see them pass, then the suite in both time zones and ruff**

Expected: PASS; ruff clean.

- [ ] **Step 5: Commit** - "Say how a download went on the satellite that asked, or send it to the phone"

---

### Task 7: The settings page and config

**Files:**
- Modify: `config.yaml` (`music`, 50-53, and a new `soulsync` section after `navidrome`), `voice_assistant/config.py` (`LIMITS`, 52-74), `voice_assistant/settings_page.py` (`EDITABLE` after the navidrome rows, 94-97; `KEYS`, 129-132), `voice_assistant/web/settings.js` (`GROUPS`, 112), `setup.sh` (the `.env` template, 364-366)
- Test: `tests/test_config.py`, `tests/test_settings_page.py`

**Interfaces:**
- Produces the settings Tasks 3 to 5 read:
  - `music.offer_downloads: true`, `music.song_download_minutes: 10`, `music.album_download_minutes: 30`; `soulsync.url: ""`, each with a comment (the key in `~/.config/nova/.env` as `SOULSYNC_API_KEY`; see `docs/decisions/0012`).
  - `EDITABLE`, all "now": `("music", "offer_downloads", "Offer to download what Navidrome hasn't got")`, `("music", "song_download_minutes", "Minutes to wait for a song to download")`, `("music", "album_download_minutes", "Minutes to wait for an album to download")`, `("soulsync", "url", "SoulSync's address")`.
  - `LIMITS`: `("soulsync", "url"): Limit("address")`, `("music", "song_download_minutes"): Limit("number", 1, 60, True)`, `("music", "album_download_minutes"): Limit("number", 1, 240, True)`.
  - `KEYS`: `("SOULSYNC_API_KEY", "SoulSync")` after Navidrome's. `GROUPS`' Music gains `"soulsync."`. `setup.sh`: "# For music fetched by voice, a key made in SoulSync's settings:" and "# SOULSYNC_API_KEY=".

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
def test_the_download_settings_have_their_limits():
    assert limit_problem("music", "song_download_minutes", 0) == "should be a whole number from 1 to 60"
    assert limit_problem("music", "album_download_minutes", 241) == "should be a whole number from 1 to 240"
    assert limit_problem("music", "song_download_minutes", 10) is None
    assert limit_problem("soulsync", "url", "soulsync.local") == "should be an address starting http:// or https://"


def test_the_repositorys_config_has_the_download_settings():
    assert Config(...).section("music")["offer_downloads"] is True  # 10 and 30 minutes, soulsync.url ""


# tests/test_settings_page.py
def test_the_download_settings_are_under_music_and_used_straight_away(files):
    shown = {setting["key"]: setting for setting in _page().state()["settings"]}
    assert shown["music.offer_downloads"]["label"] == "Offer to download what Navidrome hasn't got"
    assert all(shown[key]["used"] == NOW for key in ("music.offer_downloads", "music.song_download_minutes",
                                                     "music.album_download_minutes", "soulsync.url"))
    result = page.save_settings({"music.song_download_minutes": 5, "soulsync.url": "http://soulsync.local:8008"}, "test")
    assert result["success"] and not result["restart_needed"]
    assert brain.config.section("music")["song_download_minutes"] == 5


def test_soulsyncs_key_is_saved_and_never_shown(files):
    assert page.set_key("SOULSYNC_API_KEY", "the-soulsync-key", "test")["success"]
    assert 'SOULSYNC_API_KEY="the-soulsync-key"' in (files / ".env").read_text()
    assert "the-soulsync-key" not in json.dumps(_page().state())
```

- [ ] **Step 2: Run them to see them fail**

Run: `bash .superpowers/tools/pi_test.sh tests/test_config.py tests/test_settings_page.py -x -k "download or soulsync"`
Expected: FAIL with a KeyError on "music.offer_downloads".

- [ ] **Step 3: Implement the Interfaces above**

- [ ] **Step 4: Run them to see them pass, the suite in both time zones and ruff, and check the page script**

Run also: `~/tools/node/bin/node --check voice_assistant/web/settings.js`
Expected: PASS; ruff clean; node prints nothing.

- [ ] **Step 5: Commit** - "Put SoulSync's address, its key and the download settings on the settings page"

---

### Task 8: Docs

**Files:**
- Modify: `README.md` (the Music line, 11; a "Fetching music by voice" paragraph after Navidrome's set-up, ~158; the settings table, ~352), `docs/troubleshooting.md`, `docs/release-checklist.md` (Music, after 13), `docs/roadmap.md` (Next, item 3)

**Interfaces:**
- Consumes: the sentences, settings and key from Tasks 4 to 7.

- [ ] **Step 1: Write them**
  - README: "download X", "get me X", the offer after "play X" and how to switch it off, songs and albums but never an artist, the original release rather than a clean one, what's said and that it's said at any hour or sent to the phone, the time limits; set-up: SoulSync's API switched on, a key made in its settings under Keys as `SOULSYNC_API_KEY`, its address under Settings, Music; Nova's Navidrome user an admin for the scan, or Navidrome's own scan is waited for. One table row for `music.offer_downloads`, `music.song_download_minutes`, `music.album_download_minutes` and `soulsync.url`.
  - Troubleshooting: "Downloading music isn't set up", the key reason, "came down as a clean version" (the log names the track, for replacing it by hand), and a scan refused.
  - Release checklist: "download" a song Navidrome hasn't got (a beep and "... requested.", later a beep and "... has been downloaded.", not played); "play" something missing, "thanks" requests nothing, again and "yes" requests it; "download" something had ("You've already got ..."); "download everything by" an artist (refused); "download the album ... by ..." (downloaded, or "Only N of the M tracks ..." within 30 minutes); a request with its satellite switched off before the outcome (it arrives on the phone).
  - Roadmap: item 3 designed (0012) and built, waiting for the hand checks.

- [ ] **Step 2: Check them**

Run: `git diff -U0 -- README.md docs/ | grep -nP '\x{2014}'`
Expected: nothing - no em dashes. Each sentence quoted matches Global Constraints word for word.

- [ ] **Step 3: Commit** - "Say how to fetch music by voice, and how to check it"
