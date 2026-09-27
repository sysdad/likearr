# likearr design

Mirror a user's Spotify follows, saved albums, Liked Songs and owned playlists into Lidarr's
monitoring, and nothing else. Lidarr stays the downloader/library manager. Spotify is the only
source of what is wanted. likearr is the only writer of monitoring for Spotify-backed releases.

Issue numbers (`#123`) in this document, and in code and test comments written before v0.5.0,
refer to the pre-release tracker, which isn't public.

## The rules

| Source | Lidarr monitors |
|---|---|
| Followed artist | Catalogue + new releases, **studio Album + EP only** (tag `albums-only` on the Lidarr artist: Albums only) |
| Saved album | That album only, any type (including a single the user explicitly saved) |
| Liked Song | The **studio Album/EP** the track lives on (Singles rule), or the smallest release holding it (`[rules] liked_track_scope`) |
| Playlist track (playlists the user owns or collaborates on) | Same as Liked Song |
| Manual add / library scan | Nothing; likearr never touches what it doesn't own |

### Singles rule

Singles are excluded because most end up duplicating a song on the eventual album. Spotify may
point a liked track at the single version or the album version. Per liked/playlist track:

1. Map the release Spotify says the track is on (`track.album`, via UPC; name search as a first-class
   fallback because Spotify has already removed `external_ids` once) to its MusicBrainz release group.
   When that fails, the track's **ISRC** names the release instead (`track:album:isrc`, below).
2. Studio **Album or EP** → monitor it. Use MusicBrainz's type, never Spotify's `album_type`
   (Spotify files EPs as `single`).
3. **Single** (or compilation / best-of) → find the studio Album/EP that holds the same song:
   - ISRC → MusicBrainz recordings → their Album/EP release groups, same primary artist.
   - No ISRC match (radio edits and "single versions" get their own ISRC) → normalized-title match
     against the artist's Album/EP tracklists. Strip `Radio Edit`, `Single Version`, `Edit`,
     `Remastered YYYY`, `feat. …`, bracketed qualifiers, case, punctuation. When the release
     Spotify named credits more than one main artist (classical: the composer, then the
     performers; featured guests do not count), only a studio release with the same main artists
     is searched (#164).
   - Several matches → earliest first-release date, then lowest MBID (deterministic).
4. No Album/EP holds it yet → **`pending_album`**: monitor nothing, re-check every run.
5. Still pending `singles_fallback_days` (default 180) after the single's release → monitor the
   single itself and ratchet the artist to the Full profile.

A release that is the only local copy of a liked/playlist track is never a prune candidate.

### The ISRC fallback: `track:album:isrc`

Step 1 above is a title-and-credit search in practice. **Spotify's track objects never carry the
album's UPC**, so the barcode tier is dead on this path and every liked track depends on an exact
name match in MusicBrainz. Without a fallback, a sizeable share of liked and playlist tracks ends
UNMAPPED at `track:album:search`, every one of them saying "no barcode". The track's ISRC is a
real identifier, but every other step that reads it runs *after* the album has mapped.

So when step 1 finds nothing, the ISRC names the release instead. Over the release groups
MusicBrainz files that exact recording under, Various Artists excluded, two tiers:

1. **The release Spotify named** - a candidate whose normalized title equals the Spotify album
   title, or that title with its release qualifiers stripped. The ISRC already proves the
   candidate holds *this* recording, so the title identifies it whatever name the two catalogues
   print for the credit. That credit mismatch is the common failure: MusicBrainz credits John
   Mayer's *TRY! - Live In Concert* to **John Mayer Trio**, and that alone makes the name search
   refuse a release group it found with a perfect score.
2. **The track's own artist** - no candidate carries that title, so fall back to the artist the
   track is credited to, found by name, exactly as the Various Artists case already does. A
   studio Album/EP wins, then the earliest first-release date, then the lowest MBID.

The chosen release group is then fed into steps 2-5 **unchanged**, so this adds no new resolution
rule: `liked_track_scope`, the same-primary-artist check, the Various Artists refusal and the
deterministic tie-breaks all apply as they already do, and the answer is the one a barcode would
have produced. With no ISRC, no candidate, no artist and no match, the track stays UNMAPPED at
`track:album:search` and the detail says what the fallback tried.

**What it does not fix.** MusicBrainz records no ISRC at all for some releases - including *Try!*
itself, where `recording/<id>?inc=isrcs` answers an empty list for every track. Those stay
unmapped here, and the credit-mismatch rejection in `search_release_group` is a separate matter:
John Mayer's *TRY!* is still credited to John Mayer Trio, a *different* MusicBrainz artist, and
accepting that is an identity claim this comparison does not make. A MusicBrainz relationship
joining the two artists is what makes that claim - see "A credit MusicBrainz joins by a
relationship" below. The golden corpus records the *Try!* case as the witness for both.

`search_release_group`'s own title-and-credit comparison (#21, #22): the title side folds both
sides through the same qualifier-stripping normaliser (so a qualifier MusicBrainz carries and
Spotify does not, or the reverse, does not defeat a real match) and retries an empty
quoted-phrase search unquoted before giving up; the credit side folds through the shared
`normalize_name` (drops a leading "the") and drops a bare Spotify `featuring` decoration first.
Both are full equality, never containment - a same-titled release credited to an unrelated artist
is refused.

### Two artists, one name *and* one title: `ambiguous:same-name-artists`

The name search asks for a title by an artist **name**, and the credit gate compares names. Two
different MusicBrainz artists can share a name and each have a release by the same title, and then
both pass the gate exactly (#32). For example, a liked "Busy Earnin'" is by Jungle, the London
modern soul collective, from their 2014 album *Jungle*; the search for "Jungle" by "Jungle" also
returns *Jungle* (1969) by Jungle, a US psychedelic rock band. Breaking that tie on the earliest
first-release date, then the lowest MBID, favours the 1969 album on both keys, so the like would
land on the wrong band every run. The `smallest` scope would then only consider that band's
releases, discarding the ISRC hits that point at the London band, and the name-collision guard
would refuse to add the US band: nothing wrong monitored, but the liked song not either, and the
run `degraded` over a guess.

So ties break **per artist, never across artists**:

- **Within one artist** the resolver picks one release per artist before anything else
  (`search_release_group_candidates` returns them all). A liked or playlist track keeps the
  earliest first-release date, then the lowest MBID. A **saved album** prefers the release that is
  an album - see "A saved album is the album" below.
- **Across artists**, a date is no evidence of which one the user meant. A liked or playlist track
  asks its **ISRC**: the candidate whose artist MusicBrainz files that recording under is the one
  Spotify meant. Exactly one artist must hold it. The chosen release then goes through the rest of
  the Singles rule unchanged, and the detail says how the artist was chosen.
- **Nothing decides it** - no ISRC, an ISRC MusicBrainz does not know, one that only a third
  artist's release carries, or one both artists carry - and the intent is **UNMAPPED** at
  `ambiguous:same-name-artists`, naming every candidate. It does not fall through to the ISRC
  fallback above, whose second tier would look the artist up by the very same name. A **saved
  album** has no ISRC to ask; its barcode, the identifier it does carry, is tried before any name
  search, so reaching this point means the barcode already missed, and a saved album that gets
  here is always ambiguous.
- **Only one artist's title passes** while the search also returned same-named artists whose
  titles did not. Failing the title check is not proof of the wrong artist: the right one's
  "Still Feeling You (Deluxe 2020)" can fail it where a stranger's plain "Still Feeling You"
  passes. So the survivor is taken only unless the ISRC names **one of the other artists** and
  not the survivor's. Then it is not taken: the ISRC fallback above looks for that artist's
  release among the ISRC's own release groups, and if it finds none the intent is ambiguous as
  above. No ISRC, or one only a compilation carries, says nothing against the survivor, which
  stands on its title match. A saved album has nothing to contest it with and takes it.
- **Collaborations do not make a second artist.** A candidate's artist is its *first* credit,
  which the credit gate compares with Spotify's first artist, so "A & B" and "B & A" never tie as
  two same-named artists (the second is refused on the credit), "A & B" and "A & C" are one
  artist, and every compilation shares the one Various Artists MBID.

Correctness beats completeness: an ambiguous like costs one song until MusicBrainz knows its ISRC
(UNMAPPED is re-resolved every run, so that fixes itself); a wrong guess costs a stranger's release
in the library or, as here, a collision that never clears. An ambiguous intent is an ordinary
unmapped intent in the health record (class A, `unmapped`), is listed on its own under "Two artists
share the name" on the web UI's unmatched page, and Explain says "two different artists share this
name and album title; likearr couldn't tell which one you meant". Like any other unmapped intent it
never releases what it used to hold, so a release monitored earlier on a guess stays monitored
until the answer is known - and Explain says exactly that ("likearr keeps ... monitored
from before until it can tell which artist you meant") rather than "nothing is monitored".

The UPC of Spotify's album is not a separate signal here: where Spotify sends one (saved albums),
it is already the first thing tried, and a barcode hit never reaches the name search. Spotify's
track objects carry no album UPC at all.

**Shipped as `RESOLVER_VERSION` 5**, because it changes answers that resolved under 4. This is
the general rule for every version below: a cached RESOLVED answer is only ever recomputed by a
bump, so any change to which answer a resolved intent gets bumps the version. The first run after
a bump re-resolves every cached resolution - budget about an hour at MusicBrainz's 1
request/second on a cold cache, less where `mb_cache` still holds the answer - and the new version
changes the health `Fingerprint`, so that run re-baselines (`resolver-version-changed`) and reports
nothing as new. Dry-run it by hand and review the diff before any apply; a like moving from a
wrong artist's release to the right one is a monitor and an unmonitor.

### A saved album is the album: RESOLVER_VERSION 6

One artist often has several release groups by one title: Yellowcard's *Lights and Sounds* is an
album (2006) and its lead single (2005); Sublime's *Sublime* is the 1996 album, a 1995
compilation and two demos. Breaking that tie on the earliest first-release date alone (#23) sends
a saved album to an earlier single, EP or demo - Sublime's to a 1988 demo with a Full profile
ratchet.

So a **saved album** chooses among its artist's same-titled candidates by, in order: primary
type Album, then EP, then Single, then anything else (Broadcast, Other, untyped); a studio release
(no secondary type) before a Demo, Live or Compilation; the first-release year closest to the year
Spotify gives the album, when it gives one; and only then the earliest date and the lowest MBID.
A **liked or playlist track** is unchanged - earliest date, then MBID - because the `smallest`
scope and the Singles rule reason onward from that release and choose among the artist's
releases themselves. The per-artist choice happens before the title check, so no track path
changes.

Shipped as **`RESOLVER_VERSION` 6**. `scripts/replay_resolver.py` re-resolves an install's
snapshot from its `mb_cache` alone, with no network, and compares with the stored answers; run it
before shipping any resolver change. The `isrc-search:` and `rg-tracks:` rows keep only the
fields the adapter and the replay read (#123; `_ISRC_SEARCH_FIELDS` and `_RG_TRACKS_FIELDS` in
`likearr/adapters/musicbrainz.py`), so a resolver change that needs another field from them has
to add it there and refetch before a replay can test it.

### The Lidarr fallback, too: RESOLVER_VERSION 7

When MusicBrainz fails, or finds nothing, the name search falls back to Lidarr's own
`album/lookup` (`CompositeLookup`). Taking Lidarr's **first** match there would be, for two
artists sharing a name, the same guess the section above keeps out of the MusicBrainz path (#42).
So the fallback returns one match per artist whose name and title both fit - for a single artist,
Lidarr's first match - and the resolver treats those candidates as it treats MusicBrainz's: the
track's ISRC decides between two artists (a cached ISRC answer still works in an outage), or the
intent is `ambiguous:same-name-artists`.

A release group returned by the Lidarr fallback after a MusicBrainz **error** is also not
cached. It is this run's best answer, and the plan acts on it, but it was never checked against
MusicBrainz, and a cached RESOLVED answer is reused until the resolver version moves or it expires.
`CompositeLookup` records those release groups (`provisional_release_groups`) and `shell.run`
skips caching any resolution that rests on one, so the next run asks MusicBrainz again. A plain
MusicBrainz *miss* followed by a Lidarr hit is cached: nothing failed, and asking again would
find the same. The next section extends this to every answer reached after a MusicBrainz error.

Shipped as **`RESOLVER_VERSION` 7**: the change can alter a resolution wherever the fallback saw
two artists, and a guess the fallback cached earlier is only reachable by a bump.

### Nothing reached after a MusicBrainz error is cached (#53)

A rule that looks only at *which release group* an answer rests on misses answers that rest on
none of Lidarr's. The common one: MusicBrainz's name search errors, Lidarr's finds nothing, and
the track's ISRC - answered from MusicBrainz's cache - names the release (the ISRC stand-in,
`track:album:isrc`). It resolves, and caching it would keep it until the next `RESOLVER_VERSION`
bump, although MusicBrainz never answered the search. The ISRC makes a wrong artist unlikely, but
the release can be less precise than the name search would have found.

So the rule is about the **intent**, not the release group. `CompositeLookup.mb_failure_count`
counts every MusicBrainz failure, including the ones it answered some other way (Lidarr's name
search, "no link" for an outward link), and `resolve_all(lookup_failures=...)` reads it before and
after each intent it actually resolves - a cache hit asks nothing and is never marked. An intent
during which it moved goes into `ResolveResult.provisional`, and `shell.plan._persist_resolutions`
caches nothing for it, whatever the path. It is not a dry-run answer: the plan acts on it, and
`run --apply` monitors it, owns it, updates reason sets and unmonitors whatever it replaced, like
any other. Only the cache is skipped, so the next run asks MusicBrainz again. The release-group
check above stays as a second line, for an answer resting on a Lidarr release group reached by any
other intent. A run with provisional intents logs how many answers it did not cache and how many
waiting tracks kept their clock.

**The pending clock moves one way only for a provisional intent.** It is *started* for a track
that is now waiting and has no clock yet: a name search that fails the same way every run would
otherwise keep the clock from ever starting, and the singles fallback from ever firing. It is
never *cleared*: a waiting track that resolves, or fails with `error:metadata`, while MusicBrainz is
failing keeps its clock. An outage therefore never restarts the wait: were the clock cleared, the
next run would count `singles_fallback_days` again from zero. The singles fallback fires on the
date the wait would have ended had the outage not happened, which is the date the rule intends.
The clock only matters for a single whose release date MusicBrainz does not know; a known release date is
the clock, and no outage ever moved it.

What is **not** a failure here: an answer MusicBrainz's adapter served from an expired cache entry
after a failed refetch. That is MusicBrainz's own earlier answer, and it is kept out of `mb_ok` for
the same reason (see the health rules below). The answer itself does not change with this: the
resolver is given the same lookup answers and returns the same release and step, so it needs no
`RESOLVER_VERSION` bump - it changes what is cached, not what resolves (#9 likewise governs when a
cached answer stops being reused). Two things a run can see, neither of them ever cached: during a
MusicBrainz error, a Lidarr term still inside its negative-cache TTL is `error:metadata` where
Lidarr might have answered it (below, under the health rules); and the ambiguity detail's
wording (next paragraph), which only an UNMAPPED answer carries. The pending clock surviving an
outage (above) changes *when* the singles fallback fires, never what it resolves to.

The ambiguity detail says "N different artists named ...", not "MusicBrainz artists": behind the
Lidarr fallback, the candidates come from Lidarr.

### A credit MusicBrainz joins by a relationship: RESOLVER_VERSION 8 (#14)

The name search refuses a release group whose title matches when its artist credit does not, and
that refusal is what keeps same-titled strangers out of the library. But some of what it refuses
is the right record under a different credit: Spotify files *TRY! - Live In Concert* under "John
Mayer", MusicBrainz holds *Try!* under "John Mayer Trio", a separate artist, and knows no ISRC for
it, so neither the name search nor the ISRC stand-in can map it.

So a liked or playlist track gets one more step, **after** the name search and the ISRC
stand-in have both failed and only then (`core.resolver._related_credit`):

1. The name search's own results - the same searches, by the title as Spotify gives it and then
   with its qualifiers stripped - are read again **from the cache only, never the network**, for
   the release groups whose title matches under the same two-tier title gate but whose first
   credited artist is **not** Spotify's (`CreditRelations.release_groups_under_other_credits`).
   Various Artists is dropped, on either side.
2. For each distinct credited artist, MusicBrainz's artist-artist relationships are fetched
   (`artist/<mbid>?inc=artist-rels`, cached under `artist-rels:<mbid>`).
3. A candidate is taken only when one of those is a **`member of band`** or **`collaboration`**
   relationship, in either direction, to an artist whose name *is* Spotify's credit under the same
   fold the name search's credit gate uses (`core.normalize.credits_match`: a leading "the", case,
   punctuation, a bare `featuring`). Full equality there too, never containment.
4. Doubt chooses nothing: two joined credited artists; one credited artist joined to two
   *different* artists of Spotify's name (MusicBrainz has more than one John Mayer); any candidate
   artist whose relationships could not be read (it might be the second joined one, so a failure
   is "unknown", never "none"); or a track ISRC that MusicBrainz files under an artist of Spotify's
   name with a different MBID from the related one.
5. **The liked song must be on the record**: its title, folded exactly as the title fallback folds
   it (`normalize_title`, which drops "- Live", "(Live)", "feat. ..."), must be in the release
   group's tracklist (`release_group_track_titles`, the `rg-tracks:` cache). A title alone is weak
   evidence under a different credit: a related act's own "Live" or "Greatest Hits" is not this
   record. Among the joined artist's releases holding the song the earliest wins. A release an
   opt-out refuses is dropped, and if that leaves nothing the answer is the `track:excluded:*`
   resolution naming it.

The chosen release group then goes through the rest of the Singles rule **unchanged**, filed
under MusicBrainz's credit. For *Try!* that is a new Lidarr artist, John Mayer Trio: *Try!* is
Album + Live, the Trio has no studio album holding the songs, so it is `track:non-studio` to *Try!*
itself and the Trio goes on the Full profile. That is intended: the record is the right music, and
filing it under the Trio is what Lidarr's own import matcher and every tagger expect. It puts a
live *Gravity* on disk beside the *Continuum* one. A compilation (Max Roach's *Verve Jazz Masters
44*, under The Clifford Brown-Max Roach Quintet) takes the same path, unless the Singles rule finds
the song on a studio album by the joined artist first. The detail of every such answer names the
relationship, so `explain` shows why the artist changed.

**Why a relationship and never the names.** Containment ("Lawrence" inside "Clyde Lawrence") is the
same string shape as "Lawrence" inside "Lawrence Welk", and the name-collision guard cannot catch a
containment mistake: it fires on equal names, and by construction these are not equal. A
relationship is an editor asserting that two MBIDs made the music together, the same kind of
evidence as the Spotify URL relationship the followed-artist path relies on. The name comparison
in step 3 only picks out *which* related artist is Spotify's credit. What it cannot rule out: the
credited artist being joined to a *different* artist who shares Spotify's name exactly, with the
same album title on both sides. Resolving Spotify's artist to an MBID would close that, but a
track carries no Spotify artist id, and a name search for one is the kind of guess that can pick
a German DJ for "Lawrence". Any other relationship type is ignored: `sibling` joins Clyde Lawrence
to Gracie Lawrence, not to her records. A type MusicBrainz renames stops matching, which fails
closed.

**What it reaches, and what it does not.** `member of band` is the usual join: *Try!*, Lawrence /
Clyde Lawrence *Homesick*, David Bromberg / David Bromberg Band and Max Roach / The Clifford
Brown-Max Roach Quintet. Sister Sparrow & The Dirty Birds has no artist relationships in
MusicBrainz at all; an editor adding Sister Sparrow as a member fixes it unaided, because an
artist with no relationships is a *negative* cache entry and is asked again after
`negative_cache_days`. Stay Human / The Late Show Band fails on the title before any relationship
is read, and The Late Show Band's `member of band` relationships are to its players, none of them
Stay Human. **Saved albums are not included:** the step covers the liked-track path only, and a
saved album's barcode is tried before any name search.

**Cost.** None for almost every UNMAPPED track: the searches are read from the cache and never
refetched, and a title no other credit carries asks for no relationship. One `artist-rels` request
per distinct credited artist that does carry the title, and one tracklist per release of a joined
artist, both cached for `positive_cache_days` - typically a handful of each. A relationship lookup
that fails chooses nothing (the intent stays UNMAPPED, never a wrong artist) and moves
`mb_failure_count`, so under the rule above (#53) nothing that intent reaches is cached. A tracklist lookup that
fails is `error:metadata`, as it is for the title fallback.

**A limit worth knowing.** `normalize_title` stops at a second " - ", so a Spotify track name with
a long venue suffix ("Gravity - Live at the Sears Centre, Hoffman Estates, IL - November 2005")
does not fold to "Gravity" and fails the tracklist check. The title fallback shares the limit. If
Spotify names the *Try!* tracks that way they stay UNMAPPED, and the dry run shows it.

**Shipped as `RESOLVER_VERSION` 8.** The step only runs where 7 answered UNMAPPED at
`track:album:search`, so it cannot change an answer 7 resolved, but it changes what matches, so it
bumps anyway. A `track:excluded:*` answer reached this way that removes a monitor would be a bug.

### The title Spotify printed comes first: RESOLVER_VERSION 9 (#89)

`_titles_match` calls a MusicBrainz title and a Spotify title the same release when they agree
under `normalize_title`, which drops qualifiers - "(Remastered)", "- EP", but also "(Live)",
"(Demo)" and "(... Remix)". That is right for "is this the release?" and wrong for "which of
these?": CRUISR's plain EP *All Over* (2014-09-23) and the earlier single *All Over (Bear//Face
Remix)* (2014-01-01) both match Spotify's "All Over", the earliest date would take the remix, and
with `allow_remix_releases = false` the song would end at `track:excluded:remix` although its own
EP is a candidate.

So among **one artist's** same-titled candidates a track prefers, before the earliest date: a
title that folds equal to Spotify's (`fold_title` - case, diacritics and punctuation, nothing
dropped); then one equal to Spotify's title with its own trailing decorations stripped ("I'm Ready
- EP" is MusicBrainz's "I'm Ready"); then everything else, by the earliest-date rule. It is
symmetric - Spotify's "X (Live)" prefers MusicBrainz's "X (Live)" over a plain "X" - and it only
chooses *within* an artist, so the same-name handling above (#32) sees the same artists. A
**saved album** takes the same tier after its type and studio preferences and before the year,
because only where those tie can it hit the flaw (two untagged EPs, *The Feeling* and *The
Feeling (Remixes)*); putting it first would let an exact-titled single beat an EP the type rank
chooses.

A title alone cannot split one artist's same-titled Album and Single, and the earliest date would
then name the Single - which the `smallest` scope trusts as a release holding the song: Eminem's
"Mockingbird" would go to the single *Encore*. So within that tie a release **shown to carry the
song** wins before the date: the track's ISRC is filed under it, or its title is the song's (a
title-track single, which MusicBrainz often files without Spotify's ISRC). The ISRC is asked only
when such a tie exists. The same check keeps a song off a same-titled release named after a
*different* song - "Before He Cheats" off the single *Some Hearts*, "Trust In You" off the single
*How Can It Be* - and on the release the ISRC shows holds it.

Shipped with `keep_remix_only_tracks` (below) as **one** bump, so one re-resolve.

### The resolver train: RESOLVER_VERSION 10

Several separate rules shipped under one bump, so a library re-resolves once.

**A barcode is a GTIN (#150).** Spotify pads its UPC with leading zeros (`00888072328433`) that
MusicBrainz does not store (`888072328433`), so an exact string compare would throw away a
barcode hit MusicBrainz returned, and the album would fall to the name search or stay UNMAPPED.
Both sides are compared with leading zeros dropped; an all-zero barcode matches nothing. One release
group holding the barcode is still the answer with no title check - a barcode is an identifier,
and Chet Baker's *Chet Baker in New York* fails a title comparison with Spotify's "In New York
[Original Jazz Classics Remasters]". When the barcode spans **several** release groups (one
pressing filed under both *Tease Me* and the artist's compilation *All She Wrote*), MusicBrainz's
result order does not choose: only titles passing `_titles_match` stay, then the ones credited
to Spotify's primary artist, then the ones with an Official release, and exactly one must remain.
Otherwise the barcode names nothing and the name search decides. A barcode that found nothing is
reported as "no release with barcode X", not "unknown to MusicBrainz".

**A compilation track's artist needs evidence (#152).** On a Various Artists compilation the
track's own artist is whose studio albums the Singles rule searches. The top hit of a name search
is a guess among namesakes: "Evangeline" is a Seattle band the search ranks first and an L.A.
singer it ranks third, so taking the top hit lets a Seattle song with the same title win the
title fallback, even when the track's ISRC names the L.A. album. So the ISRC decides first - the release groups it is filed under, compilations aside, credited to Spotify's artist
name, when they are all one artist - and the name only when exactly one MusicBrainz artist carries
it. Otherwise nothing is searched and the song stays UNMAPPED at `track:various-artists`, its
detail saying how many artists share the name. Lidarr itself lists namesakes and lets a person
choose; a scheduled run has no one to ask, so the safe equivalent is to choose none.

**One ISRC, two songs (#163).** MusicBrainz sometimes files one ISRC on two of an artist's
recordings: Dean Martin's "Good Mornin' Life" and "Kiss" share `USCA29600867`, and only "Kiss" is
on the 1952 single *What Could Be More Beautiful / Kiss*. The `smallest` scope would take that
single as the smallest release holding the liked song, which it does not hold. So where the Singles rule picks
a release *for the song* by its ISRC - the `smallest` scope and `track:isrc->album` - and the ISRC's
recordings carry different titles, a recording whose title shares no word with the liked song's
(common words like "the", "of" or "me" aside) is left out, and the detail says so. A spelling or
subtitle variant shares a word ("Feelin' Alright" / "Feeling Alright"), so the album MusicBrainz
files only under the variant stays a candidate; a strict title-equality rule would have lost it. An
untitled recording is always kept, and if every titled one would go, all are kept. The other ISRC
checks (the ISRC stand-in, the same-name tie-breaks, a compilation track's artist) are unchanged.

**A composer's catalogue too large to search is not an outage (#151).** The title search browses
the track's artist's whole catalogue, and MusicBrainz credits classical recordings to the composer,
so a liked Bach track can hit the 30-page ceiling (`CatalogueTooLarge`). Counted as a metadata
failure, that would be UNMAPPED at `error:metadata`, never cached, and `mb-outage` on every run for
as long as the track stayed liked. Instead the title search is simply skipped, the detail says why ("the
title search was skipped: X's catalogue is too large to browse"), and the track takes the answer it
would have with no title candidate (`track:non-studio`, `track:various-artists`, the refusal, or
`track:pending`). The followed-artist path already treated it this way. The adapter also remembers
the ceiling for the run, so several liked tracks by one composer pay the 30-page crawl once.

**Folding and title details (#166).**

- *Letters outside plain Latin are kept when folding.* A fold that kept only `[0-9a-z]` after NFKD
  would lose letters that do not decompose (`ø`, `ł`) and the non-Latin half of a mixed-script
  string: "MØ" would equal "M" and "Часть 1" would equal "Глава 1", for titles and credits alike.
  The fold keeps every letter and digit in any script, after spelling out the Latin letters that
  have an English spelling (`ß` as `ss`, `ø` as `o`, `æ` as `ae`, `þ` as `th`, `ð` as `d`), so
  "Straße" still equals "Strasse". The MusicBrainz adapter folds its cache keys through the same
  function.
- *A bare "feat" splits a title only in credit position.* Otherwise "A Feat of Clay" and "50 Ft
  Queenie" would normalise to "a" and "50". When what precedes a bare `feat`/`ft`/`featuring` is
  one word of three letters or fewer, or a number, the title is kept whole.
- *A saved album's exact studio title comes first.* A studio release titled exactly as Spotify
  prints it removes every Live, Demo or Compilation candidate of the same artist, so a saved EP
  "X" never goes to the live Album "X (Live)"; between studio releases the type rank decides.
- *A track Spotify no longer serves* (empty name or artist) is UNMAPPED at `source:unavailable`
  with no lookup, and the Unmatched page says so, labelled by its Spotify ID, instead of advising
  that the album be added to MusicBrainz.

**Another orchestra's recording is another record (#164).** MusicBrainz credits a classical
release group to the composer first ("Jean Sibelius; London Philharmonic Orchestra, Paavo
Berglund"), and a release group's `artist_mbid` is its first credit, so the title search walked
Sibelius's whole catalogue and every orchestra's studio album of the work looked like the
artist's: Berglund and the LPO's "The Swan of Tuonela", filed by Spotify on a live album, would
go to Neeme Jarvi and the Gothenburg SO's 1985 studio album. So a `ReleaseGroup` also carries its
main credited artists' MBIDs (`main_artist_mbids`, read from the same `inc=artist-credits`
payloads, so no new request), and when the release Spotify named has **more than one** main
artist, the title search skips any studio release whose set of main artists differs, before
fetching its tracklist. With no candidate left the song lands on the release Spotify named
(`track:non-studio`), which holds the exact recording. A single-artist credit is never checked,
so a Duke Ellington compilation track still maps to his studio album (the "same song" rule), and
the ISRC step is unchanged, since an ISRC names the exact recording.

Featured guests are not main artists. A credit's join phrase joins it to the next, so everything
after a "feat.", "feat", "ft.", "ft" or "featuring" join phrase (any case) is a guest and left out
of the set: a remix single "Dirty Projectors feat. Dawn Richard" is Dirty Projectors', and the song
still finds the album credited to Dirty Projectors; without that exemption such remixes would move
off their albums. "with" is not treated as featuring: in MusicBrainz credits it joins co-billed
performers as often as guests ("Stan Getz With Arthur
Fiedler", "Elvis Presley with the Royal Philharmonic Orchestra"). `main_artist_mbids` is not
stored in the state database and is left out of equality: only the title search reads it, always
on release groups fresh from the lookup.

### A barcode's release group keeps its own credit: RESOLVER_VERSION 11

A barcode names a *release*, and a release can be credited differently from its release group:
Spotify's UPC for *At the Jazz Corner of the World* is a digital release credited to Art Blakey,
while the release group is credited only to Art Blakey & The Jazz Messengers. Lidarr files an
album under the release group's artist, so that is the only credit `album:upc` may use (#268).
MusicBrainz's release search returns the release group without a credit, so each one is fetched
through the cached `release_group_by_id`, as the ISRC path does. One that cannot be fetched is
dropped rather than credited from the release - a wrong artist is worse than no barcode answer,
and the name search still runs - and a MusicBrainz error during the fetch is a lookup error like
any other. A cached answer credited from the release is only reachable by a bump, hence 11.

### `[rules] liked_track_scope`

Which release a liked or playlist track resolves to. Two values, and the resolver is the only
thing that reads it:

- **`album`** (the default, and everything above): the studio Album/EP holding the song.
- **`smallest`**: the smallest *official studio* release holding the song, preferring Single,
  then EP, then Album. Candidates are the release Spotify named plus every release group the
  track's ISRC appears on, kept only when the primary artist credit matches; ties break on the
  earliest first-release date and then the lowest MBID, so the answer is deterministic. Steps are
  `track:smallest:single` / `:ep` / `:album`, and the detail names every candidate considered.
  A secondary-typed release (compilation, live, remix) never wins, and Various Artists is never a
  candidate at all. With no studio candidate the `album` steps above decide instead, so
  `smallest` never resolves *less* than `album` does. It costs one ISRC lookup per liked track,
  which `album` skips whenever Spotify already named a studio Album/EP.
- **The dedupe rule**, part of `smallest`: when the song's artist is a **followed** artist,
  likearr already monitors that artist's whole studio Album/EP catalogue, so a like that resolved
  to the single would put the same song on disk twice. A song that is on a studio Album/EP by a
  followed artist therefore resolves to that release, step `track:smallest:covered-by-follow`. A
  followed artist's song that exists only as a single still resolves to the single.

  Because that rule reads something that changes, the cache has to as well. Every
  `track:smallest:*` resolution records whether its artist was followed when it was made
  (`Resolution.followed`), and it is reused only while that still matches - so **following an
  artist later swaps their already-resolved singles for the albums**, and unfollowing swaps them
  back, on the next run. Without it the answer would be frozen at whatever the follow state happened
  to be the first time the track resolved, which is a plain departure from the rule above. Only
  `track:smallest:*` steps are checked; any other step means no studio release held the song at
  all, which no follow changes, and under `album` the follow state is not consulted.

Changing the value changes what every liked and playlist track resolves to, which is a large diff
by definition: the singles arrive and the albums they were standing in for leave. Review a
dry-run and apply it by hand, never let a scheduled run be the first to see it. Every cached track
resolution records the scope it was made under, and a cached resolution is reused only while the
configured scope still matches - so switching the value, now or later, re-resolves every liked
and playlist track on the next run, without needing a `RESOLVER_VERSION` bump to do it.

### Opting out: box sets, remix EPs and a deny list

Four settings under `[rules]`. The three opt-outs default to off, which resolves as much as the
resolver can; `keep_remix_only_tracks`, which only softens the remix opt-out, defaults on. They
are the rules in likearr that deliberately resolve **less** than the resolver could, so the
exchange is stated plainly: an opted-out track is reported, never dropped, and saving the album on
Spotify always overrides them, because a saved album is monitored whatever its type.

- **`allow_compilation_fallback`** (default `true`). `false` refuses `track:non-studio` when the
  release is **Compilation**-typed. This is the box-set rule: one liked Dinah Washington song
  monitoring *The Complete Dinah Washington on Mercury, Volume 2*, three discs and 53 tracks.
  Compilation-typed **only** - a live album or a soundtrack is still monitored, because for some
  songs that is the record rather than a collection of it, as Cannonball Adderley's *Mercy, Mercy,
  Mercy! Live at "The Club"* is.
- **`allow_remix_releases`** (default `true`). `false` refuses a remix release unless the liked
  track is itself a remix. "Remix" means MusicBrainz's `Remix` secondary type **or** the word in
  the release title, and the title is the half that does the work. The type alone changes nothing,
  because `is_studio` already refuses anything carrying it - and remix EPs are often typed `EP`
  with no secondary types at all. Under `smallest` an EP outranks an Album, which is
  how *Grease (The Remix EP)*, which happens to hold the original recording, beat the soundtrack.
- **`keep_remix_only_tracks`** (default `true`, read only while `allow_remix_releases = false`).
  A song whose **every** release is refused *only* for being a remix is kept on one of them -
  proven by its ISRC or a tracklist first, then one without the `Remix` secondary type (no Lidarr
  metadata profile likearr sets up allows it), then an Album or EP before a Single, then the
  earliest - at `track:remix-only` (RESOLVED), instead of `track:excluded:remix` (issue #89). A
  remix single whose artist's studio Album/EP lists the song is not remix-only: it resolves to
  that album (issue #96). The Knocks'
  "Learn To Fly" is on MusicBrainz only on *The Feeling (Remixes)*, an untagged EP holding the
  original recording: the remix rule was meant to stop a remix EP beating the real album, not to
  cost a song its only home. It never overrides the deny list or the compilation rule - one
  release refused for either among the candidates and the refusal stands - and a Various Artists
  release that is not a remix counts as a home, so *Grease*, whose original lives on the Various
  Artists soundtrack, stays excluded. `false` restores the plain refusal.
- **`deny_releases`** (default `[]`). Release group MBIDs never chosen. For the cases no rule can
  express, because MusicBrainz's data does not distinguish them. One liked copy of "Mercy, Mercy,
  Mercy" can resolve to *Phenix* - a 1975 re-recording credited to plain **Cannonball Adderley**,
  typed Album with no secondary types - while other copies resolve to the `Live`-typed album
  credited to **The Cannonball Adderley Quintet**. Two MusicBrainz artists for one performer, and
  which one a copy maps to decides what the title fallback can even see. Naming the release group
  is the only way to say "not that one". A followed artist's catalogue leaves a denied release group
  out too (#153): "Not this one" is an explicit wish, and unfollowing is too blunt a way to drop one
  album. A saved album is still monitored whatever the list says.

**They filter candidates, they do not veto answers.** Wherever the resolver has a set to choose
from, a refused release leaves the running and the next-best answer wins on its own merits. Only
where there is nothing to fall through to - the release Spotify named, with no candidate left - is
the refusal itself the answer. "No candidate" includes the Singles rule's studio Album/EP search,
which runs from the refused release as it would from any single (issue #96): a song Spotify filed
on a refused box set or remix single lands on the artist's album that lists it. Otherwise it is
reported as UNMAPPED at `track:excluded:compilation`, `track:excluded:remix` or
`track:excluded:denied`, naming the release it refused and the setting that refused it.

**An opted-out intent lets go of its release.** This is the single exception to "a reason is lost
only when it leaves the source" (see `core.diff`), and it is what makes the settings do anything
at all: the song is still liked, so without it the reason would stay live and the box set would
keep its monitor for ever. It is safe because a `track:excluded:*` answer is produced only by a
deterministic rule the user wrote down - no lookup, timeout or MusicBrainz outage can produce one,
which is exactly what the original rule defends against.

**Cost.** The three switches are carried in `ExclusionRules.token`, which is `""` for the defaults,
so a default configuration re-resolves nothing and re-baselines nothing - a resolution cached
before the fields existed carries `""` too. Turning one on re-resolves every liked and playlist track and
moves the health `Fingerprint` (`rules-changed`), because turning an opt-out on unmaps mapped
tracks by design and one switch clears the 5% jump threshold easily. `keep_remix_only_tracks`
adds `k0` only when it is off, so `c1r0` stays `c1r0` for a library that never touched it.
`deny_releases` is
deliberately outside the token and is compared against the release a cached answer chose, so
adding one MBID re-resolves only the intents that landed on it. Each track answer also records the
denied releases it was refused on the way (`Resolution.denied_skipped`, #271), so removing one
re-resolves only the intents that were kept off it, and they can go back to it.

**What was considered and not built.** A track or disc ceiling on `track:non-studio`, preferring
the smallest compilation over the earliest. It needs a track count, which no `MetadataLookup`
method returns and which costs one extra request per candidate, and it catches few real box
sets: *The Complete Hot Five & Hot Seven Recordings, Volume 3* is one disc of 22 tracks, so any ceiling low enough to refuse it also refuses ordinary albums.
The deny list covers those cases for nothing.

## Architecture: functional core, imperative shell

```
sources (SourcePort) ──▶ SourceSnapshot (all-or-nothing)
        │
        ▼
core.resolve  (MetadataLookup port, cached)  ──▶ resolutions
        │
        ▼
core.desire   ──▶ DesiredState   (pure)
        │
        ▼
core.diff     (DesiredState × LidarrView × owned state) ──▶ Diff   (pure)
        │
        ▼
shell.apply   (the ONLY code with side effects on Lidarr; per-batch SQLite transactions)
        │
        ▼
health sinks  (MQTT / webhook / stdout) - one record per run, success or failure
```

- `likearr/models.py` and `likearr/ports.py` are the contract. Core imports nothing else.
- State is a shell concern the core never sees: `core.diff` takes owned state as a plain argument
  (`DesiredState`/`OwnedRelease` values), and `Context.state` is `SqliteState`, concrete, wherever
  the shell needs it. There is no state port in `ports.py` - nothing but that one concrete class
  is ever swapped in for it, and pyright would not have checked a port nobody was typed against.
- Adapters: `adapters/spotify.py`, `adapters/spotify_library.py`, `adapters/musicbrainz.py`,
  `adapters/lidarr.py`, `adapters/state_sqlite.py`, `adapters/health.py`.
- Shell: `shell/run.py` (`run`'s plan and apply), `shell/commands.py` (`auth`, `playlists`,
  `adopt`, `lidarr-files`, `explain`), `shell/setup_commands.py` (`doctor`, `setup-profiles`),
  `shell/prune_commands.py` (`prune-report`, `prune-checks`, `prune-stage`),
  `shell/promote_save.py`, `shell/cli.py`.
- `promote-save` is the one command that writes to Spotify rather than Lidarr. It keeps the same
  shape: a pure decision (`core/match.py`) behind a port (`SpotifyLibraryPort`), with the shell
  holding the plan/apply orchestration and no HTTP of its own.

## Ownership

- State (SQLite) records every release likearr monitored, with reasons, resolver step and version.
- Ownership is claimed only when likearr itself flips a release from unmonitored to monitored.
  A monitor batch's rows are written just before its PUT, because Lidarr can apply a batch and
  still answer with an error (#174). If the PUT fails, likearr reads the batch back and undoes
  only the rows whose album is still unmonitored. If that read fails too, it keeps them all: an
  owned row on an unmonitored album is harmless, since the next run monitors it again.
- likearr never unmonitors anything it doesn't own. `run`/`apply` never deletes an artist or a
  file; `prune-stage` is the one command that removes an artist's Lidarr row (never its files),
  and only once every file of theirs is staged - see [docs/DEPLOY.md](../DEPLOY.md#mounting-the-library-for-prune-stage).
- `adopt` handles releases monitored before likearr existed (user marks a keep-list → `manual`;
  the rest is unmonitored once). It is plan -> reviewed file -> apply: the keep list is baked into
  the plan, `--apply PLAN` refuses a plan whose source or Lidarr digest moved (exit 3), and both
  steps hold the run lock. Lost state means "owns nothing", so `run` unmonitors nothing - but
  `adopt` is NOT a safe recovery from it: with no keep file it unmonitors every release no source
  backs, manual monitors included. Restore the state DB from backup instead - see
  [docs/DEPLOY.md](../DEPLOY.md#backup-and-restore).
- Artists likearr adds carry the Lidarr tag `likearr` and a row in `owned_artists`. That table is
  bookkeeping for artists likearr *added* (tag, profile, ratchet); ownership of a **release** lives
  in `owned_releases`, including everything `adopt` claimed. Any "does likearr own something of
  this artist" question must read `owned_releases`, never `owned_artists` alone.
- A lost or replaced state DB is detected, never repaired (#175): every run counts the Lidarr
  artists carrying the tag with no `owned_artists` row, logs them, and records the count as
  `tagged_without_state`; Status shows it and `doctor` fails on it when `owned_artists` is empty (a
  warning otherwise, since the tag can be added by hand). Nothing is claimed; restore the DB from
  backup.
- Reasons are a set. A reason is removed only when its Spotify ID leaves the source, never because a
  mapping failed this run. A `followed` reason is removed only when the artist is unfollowed; a
  release group vanishing from Lidarr's catalogue is reported, not treated as a lost reason.

## Profiles

- **Lean** (Album + EP; Studio only): default for every artist.
- **Full** (Album, EP, Single; Studio, Compilation, Soundtrack, Live; never Remix / DJ-mix):
  only for an artist whose saved album or singles-fallback release needs one of those types.
- An artist Lidarr holds **unmonitored** while a desired release lives under it is re-monitored on
  every run (`monitor_artists` in the diff, one `artist/editor` call). Lidarr never searches or
  lists as wanted an album whose artist is unmonitored, and it can apply `addOptions.monitor: none`
  to the artist after the POST, so a just-added artist is re-monitored after its refresh without
  looking at the POST response. `doctor` FAILs on the state.
- One-way ratchet: once Full, never auto-downgraded. Monitoring, not the profile, limits what's wanted.

## `monitorNewItems = none`, and catalogue gaps

likearr sets Lidarr's `monitorNewItems` ("Monitor New Albums") to `none` only on **artists holding
a release it owns** (#172): one it added, claimed or adopted. That is the artists it added or
ratcheted (`owned_artists`) and the artists of every release in `owned_releases`, which includes
the ones `adopt` claimed and the ones a `manual` reason keeps. It includes an artist whose release
it claims in the same run, so the write lands with the claim rather than a run later, and one whose
profile it ratchets to Full in the same run: the apply refreshes that artist right after widening
it, and on `all` Lidarr would monitor every release type the wider profile shows. So the apply sets
`none` first (phase c, before the ratchets in phase d), and what was already monitored stays
monitored. The same goes for an artist likearr re-monitors (`monitor_artists`, phase d2): once it
is monitored again, `all` would auto-monitor its future albums. An artist a human manages by hand still gets `none` once it holds a release likearr
owns, because likearr decides what is monitored under that artist and Lidarr auto-monitoring a new
release would monitor something no source asked for. A hand-managed artist whose wanted release is
already monitored keeps its setting: likearr claims nothing there, and a release Lidarr
auto-monitors there is never owned, so likearr never unmonitors it. likearr never removes the
artist, never unmonitors anything of theirs it does not own, and never changes the setting back.
If you want Lidarr to auto-monitor an artist's new releases, that artist must not hold a release
likearr owns - move it out of `owned_releases` (unfollow / unsave the thing that wants it, or hand
the release back with `adopt`).

The write shows wherever a plan does: a count line in `likearr run`'s plan, naming the artists, and
in its apply summary; "Artists to stop auto-monitoring" in the web review and in Status's "What
changed"; `counts.new_items_none` in the health record; and a line in the `adopt` plan naming the
artists its next run will set. A widening whose artist is also set to `none` is warned about in the
plan, the apply summary, the apply's log and the web review's "Profiles to widen" row.

The consequence is that a **followed artist's new album is only monitored once Lidarr's own
metadata has the release group**, on the run after that. Until then MusicBrainz lists it and
Lidarr has no album for it, which likearr reports as a *catalogue gap*. Those come in two kinds
and are reported, counted and treated separately:

| | `catalogue_gaps` | `catalogue_gaps_recent` |
|---|---|---|
| Release date | unknown, or older than `[rules] recent_release_days` (60) | within that window, or in the future |
| What it usually is | a promo, bootleg or non-Official pressing Lidarr's profiles never track | a real release Lidarr's metadata has not caught up with |
| What likearr does | reports it, every run, for ever | queues a `RefreshArtist` for that artist (`refresh_artists` in the diff), at most once per `[lidarr] recent_gap_refresh_hours` (24) and at most `[lidarr] max_refreshes_per_run` (10) per run |
| Health class | A - counts toward the jump | reported only - never a regression, never degrades |

An undated release group is **not** recent. MusicBrainz leaves promos undated far more often than
it leaves new albums undated, so guessing "new" would queue a refresh for the same artist on every
run for ever.

The refresh is part of the diff rather than something the apply decides on its own, so a dry run
shows it and a reviewed `--apply` carries exactly the refreshes that were reviewed. It carries
`isNewArtist: true` like every other refresh likearr sends, which keeps Lidarr's follow-up rescan
inside the artist's own folder instead of walking every root (see "Upstream quirks"). The release
itself is not monitored on the run that refreshes - the plan listed it as a gap, not as a monitor
- so it is the *next* run that picks it up.

**Rationed two ways.** A **backoff** (`recent_gap_refresh_hours`, 24) keeps an artist from being
asked again too soon: MusicBrainz dates plenty of promos and non-Official releases Lidarr will
never carry, and without it such an artist would be refreshed on every scheduled run for the whole
60-day window, which is hundreds of commands against a metadata proxy that is already a
separate failure domain. The stamp is written whether or not the refresh worked, because an artist
whose metadata is stuck is exactly the one that must not be asked every run. A **cap**
(`max_refreshes_per_run`, 10) then bounds one run, ordered by longest-since-last-asked and then
freshest release, so nobody starves behind a permanently stuck gap. An artist either ration drops
is still reported as a recent gap; only the asking is rationed. The backoff lives in the state DB
(`gap_refreshes`, schema 3).

**A failure here is not a skipped artist**, unlike the add and ratchet refreshes. Those refresh an
artist whose album rows cannot be trusted until they finish - a newly added artist has none at all
- so carrying on would monitor nothing and unmonitor everything. This one is opportunistic: the
artist was already in Lidarr with a catalogue this run has read, and the refresh only asks for one
more release. So a timeout is logged, counted in `HealthRecord.refresh_failures`, and otherwise
ignored: the artist keeps its monitors for the run, and no class-B `skipped_artists` identity is
registered - that is a sticky degrade, and it would fire every run for a request likearr did not
have to make.

## Safety

- Dry-run is the default. It writes `diff.json` and prints a summary.
- `--apply diff.json` executes exactly the reviewed diff; refuses (exit 3) if stale. The diff
  carries a digest of the source snapshot and of the affected Lidarr albums' monitored state.
- **The diff also carries the configuration it was planned under** (`config_fingerprint`: every
  `[rules]` and `[guards]` setting, `deny_releases` included), and `--apply` refuses it (exit 3,
  status `stale`) when any of them has changed since, naming the settings that moved. Spotify and
  Lidarr can both be exactly as they were while the user has denied a release the saved plan
  monitors - and that plan would monitor it anyway. `ExclusionRules.token` cannot catch this:
  it leaves `deny_releases` out on purpose. A diff written before the fingerprint existed is
  refused the same way, because it cannot vouch for its configuration; the remedy is a re-plan
  (a few minutes on a warm cache, longer after a resolver bump). `[lidarr]` is not included: it
  says how to talk to Lidarr, not what is wanted. `--force` overrides this exactly as it overrides
  the digests. The check needs only the file and the config, so it runs **before** the fresh plan,
  and its `stale` record goes to local sinks and the runs table only: it is the user's own doing,
  so it must not light Home Assistant's amber or reset the dead-man's switch. A diff refused
  because Spotify or Lidarr moved still publishes everywhere, as before.
- Scheduled runs refuse unmonitors above `max_unmonitors_scheduled` and apply the rest
  (guard `scheduled-cap`).
- Any source read error (failed page, 429 `QUOTA_EXCEEDED`, auth, failed schema canary) aborts the
  run with zero unmonitors.
- Schema canary: structural fields (`items`, `next`, `track.id`, `track.album.id`) missing → hard
  fail. `external_ids` missing across a whole page → `spotify_schema_ok=false`, status `degraded`,
  run continues with adds/monitors but NO unmonitors (guard `schema`).
- Shrink guard: a source whose count drops > `source_shrink_pct` since the last run has its
  unmonitors refused (status `guarded`, guard `source-shrink`). Same per followed artist
  (> `artist_shrink_pct` fewer Album/EP release groups than last time, guard `artist-shrink`).
  The per-artist count is the catalogue **before** the user's own filters (`[rules] deny_releases`
  and the `albums-only` tag): denying a release or tagging an artist is a choice, not a catalogue
  that shrank, and counting it would hold every scheduled run until someone accepted the shrink.
  A guarded source or artist keeps its previous baseline (a `schema` guard holds all of them), so
  the refusal repeats every run until the count recovers instead of lasting one run.
  An artist absent from the followed source is an **unfollow**, not a shrunken catalogue, so it is
  exempt from `artist-shrink` (a mass unfollow is `source-shrink`'s to catch); `artist-shrink` is
  for artists still followed whose catalogue dropped, or could not be read.
  **Accepting a shrink**: `likearr run --accept-shrink` on a hand-run plan skips the `source-shrink`
  and `artist-shrink` guards for that plan and records `accept_shrink: true` in `diff.json`; the
  reviewed `--apply` then carries it, and the new counts become the baseline. It is refused with
  `--scheduled` and with `--apply`, and a scheduled run refuses a diff that carries it. It never lifts
  `schema` or `scheduled-cap`.
- **Name-collision guard** (`name-collision`): an artist is never added when Lidarr already has
  that name (folded by `normalize_name`) under a *different* MusicBrainz id. See "Two artists,
  one name" below.
- **Projected-wanted advisory** (`projected-wanted`): more than `projected_wanted_max` desired
  releases would be monitored with no files on disk. Purely advisory: it is reported in the guards and
  the message, blocks no unmonitors, and leaves the status and exit code alone.
- Lidarr metadata outage (refresh fails): skip that artist this run; never unmonitor from it;
  `lidarr_metadata_ok=false`. That flag reports the *call*, not the run's health: it is
  chronically false because a few search terms always 503, so the status comes from whether the
  set of failing lookups and skipped artists changed. See "What makes a run degraded". The RefreshArtist wait scales with the catalogue:
  `refresh_timeout_s` (floor) + `refresh_per_album_s` x release groups, capped at
  `refresh_timeout_max_s` (defaults 300 s, 2 s, 3600 s). A wait that still runs out is a skip, not
  a failure: Lidarr keeps refreshing, so the artist is picked up on the next run.
- Search is never triggered; Lidarr's own missing-search / RSS / soularr do the downloading (see
  README's "Getting the music downloaded").
- Exit codes: 0 ok, 1 error, 2 guarded, 3 stale diff.
- Spotify token refresh is serialised by `<token_file>.lock` (`fcntl.flock`, held for the whole
  load-check-refresh-save sequence), independent of the run lock above: `explain` and the
  web UI's jobs refresh the token without ever holding the run lock, so they need their own guard
  against two processes both refreshing the same token. See "Spotify Dev Mode" for why.

## Health record

```json
{"ts": 0, "version": "", "resolver_version": 4, "exit_code": 0, "status": "ok|guarded|degraded|error|stale|skipped|paused",
 "spotify_ok": true, "spotify_schema_ok": true, "mb_ok": true, "lidarr_ok": true, "lidarr_metadata_ok": true,
 "counts": {"followed_artists": 0, "saved_albums": 0, "liked_tracks": 0, "playlist:<id>": 0,
            "intents": 0, "desired": 0, "monitored": 0, "unmonitored": 0, "added": 0,
            "new_items_none": 0},
 "unmapped": 0, "pending_album": 0, "message": "", "dry_run": true,

 "unmapped_new": 0, "unmapped_resolved": 0, "unmapped_ratio": 0.0, "regressions": 0,
 "catalogue_gaps": 0, "catalogue_gaps_new": 0,
 "catalogue_gaps_recent": 0, "catalogue_gaps_recent_new": 0, "refresh_failures": 0,
 "absent_in_lidarr": 0, "absent_in_lidarr_new": 0,
 "lidarr_metadata_errors": 0, "lidarr_metadata_errors_new": 0, "mb_errors": 0,
 "skipped_artists": 0, "skipped_artists_new": 0,
 "name_collisions": 0, "name_collisions_new": 0,
 "catalogue_too_large": 0, "catalogue_too_large_new": 0,
 "baseline": "compared", "baseline_advanced": true, "new_conditions": [],
 "changes_made": null, "changes_planned": null, "lidarr_changed": null, "tagged_without_state": 0}
```

Emitted to every configured sink on every run, success or failure. `status` is `guarded` when a
guard refused unmonitors (exit 2), `degraded` when something is **newly** wrong (exit 0, see
below), `stale` for a diff the world moved under (exit 3), and `skipped` for a *scheduled* run
that found the run lock held: another run is doing the work, so it is exit 0 and not a failure.
A hand-run that finds the lock held is `error` with its own exit code, 4 (`EXIT_BUSY`, #54): not a
fault, try again when that run ends. Every hand-run command that takes the lock (`run`, `adopt`,
`promote-save --apply`) exits 4 on it, and the web UI reads a job's "busy" from that code alone,
never from words in its log.

`status` is `paused` (issue #68) when a *scheduled* run found `[schedule] enabled = false`: it did
nothing at all - no Spotify, MusicBrainz or Lidarr call, no lock taken - and exits 0. It still
publishes, with `message` carrying the reason, so `ts` stays fresh for the HA dead-man and the
Status page's next-run card can tell "paused on purpose" from "scheduled runs stopped". It is
checked before the run lock, not inside it (`shell/run.py`, `run_command`): there is nothing to
hold the lock for, so a paused fire never waits behind a hand run that is already applying. A hand
run (`run`, `run --apply`, no `--scheduled`) never sees this status - the pause is scheduled-only:
it stops the schedule, not the user. A `paused` record advances nothing in `core.health`'s
baseline: it never reaches a plan, so the next real run compares against exactly what the last
real run left.

A scheduled run is also `paused` (issue #111), with the message `waiting for your first reviewed
apply (...)`, until a hand `run --apply` has completed its apply step once: the state database's
`first_apply` row (schema 8, `SqliteState.first_apply_at`), written by `run_command` and nowhere
else. Same check, same place, same reasons as the config pause, which it follows, and before
`plan`, so a fire before Spotify is connected is `paused` rather than `error`. With no state
database at all, the CLI decides it before building a `Context` (`run.scheduled_run_without_state`),
because opening one would create the file, and publishes to the sinks only. `adopt --apply` does
not count, and a hand run never reads the gate. It exists because the schedule is on from the
first minute, and the first apply is the largest change likearr ever makes to a library.

An apply records `changes_made` and `changes_planned` (#54): the changes to Lidarr it made and the
ones the diff asked for (artists added, releases monitored and unmonitored, profiles ratcheted,
artists whose "Monitor New Albums" was set to None). `counts.new_items_none` is that last number on
its own (#172): what the diff proposes on a dry run, the artist ids sent on an apply.
`changes_planned` is an upper bound: a release found already monitored, missing from Lidarr's
catalogue, or under an artist skipped this run is asked for and never made, so a clean apply can
make fewer - only an `error` apply is read as having stopped part-way. `lidarr_changed` says
whether it wrote to Lidarr at all, counted change or not (a tag or profile created, a profile set,
new-item monitoring changed, an artist re-monitored): every Lidarr call an apply makes that is not
a known read sets it (`_WriteWatch`). `ensure_tag` / `ensure_metadata_profile` are get-or-create and almost always only read, so they are on the read list, and the apply counts a creation itself when the plan's view lacked the tag or profile. The quality-profile check comes first, before anything could be created. An apply that fails once it has begun changing Lidarr is
still `error`, but its record says how far it got: "the apply stopped part-way: N of M changes
made: <cause>", or "... Lidarr settings may have changed, but none of the M planned changes was
made: <cause>" when only settings were written. Only one that wrote nothing records
`changes_made: 0` and `lidarr_changed: false`, "the apply failed before changing anything". A
cause that is not a Lidarr error keeps its type name, and its traceback is logged at debug. A dry
run, and a record from before the fields existed, has them null.

`tagged_without_state` (#175) counts the Lidarr artists carrying likearr's tag that have no
`owned_artists` row: the sign of a state database lost or replaced (see Ownership). A run that
finds any logs them, and `message` says so; it is report only, so it moves neither `status` nor the
exit code. 0 on a run that never reached a plan, and on a record from before the field existed.

The fields above the blank line are the core record; those below it carry change detection. Fields
are only ever added, never renamed, retyped or given a new meaning, so an existing consumer never
has to be touched.

## What makes a run degraded

**A run is `ok` when nothing is *newly* wrong. It is `degraded` when something changed for the
worse, or an external dependency actually failed this run. Chronic conditions are reported as
counts.**

This matters because the chronic conditions never clear. A handful of Lidarr search terms return
503 on every run for ever; a sizeable share of intents never map; Lidarr's catalogue will never
hold the promos MusicBrainz lists for a followed artist. A status that reacts to the *state*
therefore reads `degraded` for ever, which is indistinguishable from reading nothing at all - and
an operator who has learned to ignore it is also ignoring a MusicBrainz ban, a metadata outage and
a newly skipped artist.

So every chronic condition carries a stable **identity**, the previous apply's identities are kept
in the state database, and a run compares *sets*. Counts are never compared: 200 unmapped intents
that quietly became a different 200 is precisely the failure a count cannot see.

### The three classes

| Class | Conditions | Identity | Rule |
|---|---|---|---|
| **A** mapping shortfalls | `unmapped`, `catalogue_gaps`, `absent_in_lidarr`, `lidarr_metadata` | `intent\|release`, `artist/release`, or the search term | Only a **jump** alarms (below) |
| **B** discrete faults | `skipped_artists`, `name_collisions`, `catalogue_too_large` | artist mbid, or `wanted\|existing` | **Any** new identity alarms, every run until it clears |
| **C** dependency failures | `spotify_schema_ok`, `mb_ok`, this-run Lidarr metadata outage | none - never chronic | Always alarms, including on a first run |
| *(no class)* reported only | `catalogue_gaps_recent` | `intent\|release` | Compared and counted; **never** alarms |

Class A shares one rule because it has one consequence: a release likearr wanted is not being
monitored. A failed Lidarr metadata lookup belongs here rather than in class B for exactly that
reason - the resolver's answer to it is "unmapped" - and putting it in class B would light the
signal on every ordinary day.

**Regressions.** A class-A identity counts toward the jump only when its intent existed on the
previous run. "Something that was there last run and was fine is now not fine" is an alarm; "a
brand-new intent failed to map" is the ordinary base rate and is never an alarm however many arrive
at once, or liking 300 songs in a sitting would report a fault. Identities with no intent of their
own - a failed lookup belongs to a search term - always count: they cannot be flooded by a bulk
add, and they are the only sign of a metadata-proxy outage.

**Why a new release is in no class.** A followed artist's brand-new album is a catalogue gap until
Lidarr's metadata catches up. Its intent - the follow - existed last run, so inside class A every
ordinary new release would count toward the jump and a busy release week would report a fault for
the system working exactly as designed. It is not nothing either: a recent gap that is still here
after a refresh and an apply means Lidarr's metadata proxy is not doing its job for that artist.
So it is **reported rather than classified**, and `catalogue_gaps_recent` minus
`catalogue_gaps_recent_new` is the number to watch: those survived an apply that already refreshed
their artist. There is deliberately **no degrade rule** for it - one persistent recent gap is
common and self-healing, and a gap Lidarr never picks up ages out of the recency window on its own
and becomes an ordinary class-A `catalogue_gaps` identity, where rule 10 already sees it.

**A real Lidarr metadata outage vs. the terms that always fail (issue #18).** The chronic
`lidarr_metadata` identities are class A: a handful of search terms that 503 on `album/lookup`
every run for ever, at a scale that never moves the jump threshold and never should. A *total*
outage of `api.lidarr.audio` produces the same shape of evidence - `lidarr_metadata_ok: false` -
but the lookups one run asks of Lidarr are typically far fewer than the jump threshold, so it
cannot trip rule 10 either: the regression jump is a *rate against last run's intents*, and a failed lookup has no
intent of its own to regress against, so a wall-to-wall Lidarr outage looks exactly like an
unusually bad but still-chronic day. `core.health.lidarr_metadata_outage(attempts, failures)` is
the separate, this-run-only class-C check that catches it: `LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS`
(10) lookups actually asked of Lidarr this run, more than `LIDARR_METADATA_OUTAGE_RATIO` (half) of
them failed. A term skipped because it is still inside its negative-cache TTL is not an *attempt*,
so a library with many chronically-failing terms can never look like an outage on a day nothing
changed.

**Negative-caching a term Lidarr's metadata server always 503s on (issue #18).** Re-asking a term
that fails server-side on every run is pure waste, repeated for ever. `CompositeLookup`
skips a term still inside `[musicbrainz] negative_cache_days` (reused, not a new knob) rather than
calling Lidarr again, storing the identity in the state database's `lidarr_negative_cache` table
(schema 4). A skipped term still counts in `lidarr_metadata_errors` and keeps the *same* identity
it always had, so the chronic set stays stable - it must never read as "resolved" when the entry
is skipped and then "new" again once it expires and fails again. **What gets written, and when:**
only identities that failed a *genuine* attempt this run, and only once, at the end of the run -
never per call - and only when at least one *other* Lidarr metadata lookup succeeded. Writing
unconditionally on any failure would let a plain `api.lidarr.audio` outage poison the cache for a
week, hiding the very thing the outage rule above exists to catch. The fallback after a
MusicBrainz *error* goes through the same cache and counters as the one after a miss (#53):
during an outage every name search reaches Lidarr, so it is exactly when re-asking the terms known
to fail would cost most, and those attempts belong in the outage rule's numbers. A cached term
there is both backends failing: the intent is `error:metadata`, as it was when Lidarr was asked
and 503'd. Because that sends far more terms to Lidarr, the write has a second gate: nothing is
written on a run that `lidarr_metadata_outage` (rule 7) calls an outage, however many lookups
succeeded. One success among mostly failures is a partial outage, not a handful of terms SkyHook
chokes on, and caching every term it touched would hide the outage for a week.

### The rules, in order

| # | Condition | Status | Exit |
|---|---|---|---|
| 1 | Source read, Lidarr read, diff file or unexpected exception failed | `error` | 1 |
| 2 | `--apply` diff no longer matches Spotify and Lidarr, or its `[rules]`/`[guards]` changed | `stale` | 3 |
| 3 | The run lock was held: a scheduled run is skipped; a hand-run command is busy (`EXIT_BUSY`) | `skipped` / `error` | 0 / 4 |
| 4 | A guard blocked unmonitors | `guarded` | 2 |
| 5 | `spotify_schema_ok` is false | `degraded` | 0 |
| 6 | `mb_ok` is false | `degraded` | 0 |
| 7 | This-run Lidarr metadata outage: `LIDARR_METADATA_OUTAGE_MIN_ATTEMPTS` lookups attempted and more than `LIDARR_METADATA_OUTAGE_RATIO` failed | `degraded` | 0 |
| 8 | Baseline comparable and a new `skipped_artists` or `catalogue_too_large` identity | `degraded` | 0 |
| 9 | Baseline comparable and a new `name_collisions` identity | `degraded` | 0 |
| 10 | Baseline comparable and class-A regressions > `guards.unmapped_ratio_amber` x intents | `degraded` | 0 |
| 11 | No baseline, or its fingerprint moved | falls through | 0 |
| 12 | Anything else, including chronic sets unchanged or smaller | `ok` | 0 |

`lidarr_metadata_ok` is **not** in that list. It is still published and still false on the chronic
days; it simply does not decide the status on its own, since a signal that is false every day
says nothing.

A name collision is still `degraded` rather than `guarded`, and still exit 0: exit 2 means
unmonitors were refused, and a collision refuses an *add*, which loses nothing.

### What this does and does not detect

| Failure | Caught by |
|---|---|
| Spotify drops `external_ids` across a page | Rule 5. *Not* rule 10: resolutions are cached, so existing tracks stay mapped and only new likes fail |
| MusicBrainz ban or outage | Rule 6, when a lookup had no cached answer to fall back on. One that did is counted in `mb_errors` and does not alarm: the mapping was kept |
| A total `api.lidarr.audio` outage this run | Rule 7. *Not* rule 10: a failed lookup has no intent to regress against, and one run's failed lookups are typically far fewer than the jump threshold |
| A followed artist newly skipped for a Lidarr metadata failure | Rule 8 |
| A followed artist past MusicBrainz's browse ceiling | Rule 8 |
| A new name collision | Rule 9 |
| A followed artist's Lidarr catalogue emptying out | Rule 10 |
| A handful of search terms Lidarr's metadata server always 503s on | Negative-cached after `negative_cache_days`; not retried, but still counted and keeps its identity - rule 10's chronic set is unaffected |
| Lidarr never picking up a followed artist's new album | Nothing alarms. `catalogue_gaps_recent` stays non-zero across applies, and eventually the gap ages into `catalogue_gaps` (rule 10) |
| Scheduled runs stopped entirely | Nothing here - the `ts` dead-man's switch in `docs/DEPLOY.md` |

### The baseline

Kept in the state database (schema 5) with the **fingerprint** it was collected under:
`RESOLVER_VERSION`, `liked_track_scope`, the sorted `SourceSnapshot.counts` keys, and
`ExclusionRules.token`. A missing baseline or any fingerprint change makes rules 8-10 inapplicable
- every identity would read as new - so the run publishes `ok`, records why in `baseline`
(`first-run`, `resolver-version-changed`, `scope-changed`, `sources-changed`, `rules-changed`),
and writes a fresh one. Rules 5, 6 and 7 still apply: a real outage on a first run is still a real
outage.

The last two components are load-bearing, and for the same reason. Adding many playlists at once
can produce hundreds of new identities in a single run; without the source set in the fingerprint that is a
`degraded` caused by editing config. Turning on one of the issue #15 opt-outs does the same thing
deliberately - it unmaps mapped tracks, easily past rule 10's threshold on a library of a few thousand intents
- and a run that has been asked a different question has not got worse. The rules token is `""`
for a default configuration, so it moves only when an opt-out is actually turned on: a default
configuration never re-baselines, and a baseline written before the component existed compares
equal to one written with the defaults.

**Only an apply advances the baseline.** A dry run reports the delta and writes nothing, for the
same reason the shrink baselines are not advanced by a dry run: a hand plan that moved the line
would mean the apply actually being watched compares against itself and reports nothing.

**Class A is overwritten; class B is sticky.** A jump is a rate signal and is inherently a one-run
event, so making it sticky would rebuild the permanent `degraded`. A discrete fault is not: its
identity is only dropped from the baseline when it stops being observed, so it degrades every run
until it clears or a human accepts it with `likearr run --apply DIFF --accept-health`. A fault
that heals on its own clears on its own, because it is simply absent next run. `--accept-health`
is refused with `--scheduled` and on a dry run: acceptance is a human act, and an unattended
scheduled run carrying it would silence class B for good.

A first run does **not** absorb a discrete fault, because class B only keeps identities the
baseline already held and a first run has no baseline. A first run therefore swallows the standing
mapping shortfall, as intended, without swallowing a real collision or skip that happens to be
present at the time.

## Commands

- `likearr run [--apply [DIFF]] [--scheduled] [--force] [--accept-shrink] [--accept-health]
  [--out diff.json] [-c config.toml]`
  (`--scheduled --apply` with no file = unattended plan-and-apply; `--scheduled` alone is a
  dry-run; `--force` applies a stale diff anyway, including one whose `[rules]` or `[guards]` changed; `--accept-shrink` is for a hand-run plan only;
  `--accept-health` is for a hand-run apply only, and accepts the discrete faults it reports)
- `likearr auth [--manual] [--promote-save]` (Spotify PKCE; `--manual` prints the URL and accepts
  the pasted redirect; `--promote-save` also asks for the write access `promote-save` needs; every
  authorization records its date for the re-auth countdown - see "Web UI")
- `likearr playlists [--json]` (every Spotify playlist the account lists, which a run can read and which are configured; `--json` is
  one line, the web UI's playlist picker reads it; changes nothing)
- `likearr lidarr-files --plan DIFF [--out FILE] [--json]` (how many track files Lidarr holds for
  each release the plan unmonitors; read-only, one `load_view`; the plan review's "On disk")
- `likearr start [--host 127.0.0.1] [--port 8770]` (the service: web UI, scheduler and job runner;
  needs `LIKEARR_UI_PASSWORD` of at least 16 characters, and refuses to start without it)
- `likearr explain [--json] [--from-last-run] <artist | release title | song | mbid>` (why something
  is, or is not, monitored: a plain-language summary first - what you did on Spotify, what likearr
  matched it to, what happened in Lidarr and why, and whether the match looks wrong - then the
  detail. Live by default, which plans afresh and takes as long as a dry run; `--from-last-run`
  answers at once from what the last `run` recorded, asking nobody. `--json` is one line, which the
  web UI renders. Changes nothing)
- `likearr adopt [--keep keep.txt] [--out adopt.json]` / `likearr adopt --apply [adopt.json]`
  (plan only, then execute exactly that plan; `--keep` is a planning option)
- `likearr doctor [--no-spotify]` (config, state, Lidarr version, root folder, quality and
  metadata profiles, the tag, duplicate artist names, MusicBrainz, and the Spotify token plus one
  schema-canary page per enabled source; writes nothing. A `QUOTA_EXCEEDED` answer is one FAIL
  naming the quota and its `Retry-After`, and the Spotify pages after it are SKIP, never requested)
- `likearr setup-profiles [--apply]` (creates Lean/Full, sets root folder defaults to none/none)
- `likearr promote-save --decisions FILE --reviewed FILE [--out promote-save.json]` /
  `--apply PLAN.json [--decisions FILE] [--reviewed FILE] [--force]` (`--reviewed` is mandatory
  for any save, an artist's or a single album's)
- `likearr prune-report [--out prune.json]` (every row says whether its artist is followed on
  Spotify, `artist_followed`: true, false, or null when follows are not read; and
  `follow_unmatched` when a Spotify follow of that name never matched a MusicBrainz artist. From
  the reads `run` makes; no extra Spotify call). A protected row keeps its terminal line in
  `protected_reason` and says the same as data in `protection` (#64): `kind` (`pending_album`: the
  song waits for its album; `album_not_downloaded`: its album has no files yet), `intent_key`,
  `source` (`liked` or `playlist`), `playlist_id`, `track_id`, `song` and `song_artists` (from the
  run's snapshot; null when it did not have the track), and `album` / `album_mbid` (the album the
  song matched; null while it waits). Candidates carry `protection: null`
- `likearr prune-checks [--out FILE]` (#58; read-only): what to check in Lidarr before
  `prune-stage --apply` - import lists with automatic add (they would add back what the stage
  removes) and the commands still queued or running. A check Lidarr cannot answer is recorded with
  its error, never guessed. Needs no Spotify client.
- `likearr prune-stage --manifest prune.json --holding DIR (--artists NAME,... | --all-candidates | --decisions FILE) [--apply] [--no-mount-check] [--out FILE]`
  (moves only; emptying the holding folder is a human's job). Selection is exactly one of the
  three. The holding directory must be outside `lidarr.root_folder`, compared as written and as
  resolved (`..`, symlinks), else refused. **The mount is checked before a move is listed**, dry
  run and `--apply` alike (#58): the root folder is visible here at Lidarr's path; the holding
  folder is on its filesystem and, from `/proc/self/mountinfo` where there is one, on its mount;
  every file Lidarr lists is here at Lidarr's size. Anything else is refused with what to mount,
  and nothing moves. `--apply` renames (`os.rename`), never copies: an EXDEV stops the stage at
  that file. A file already at its destination is never replaced. Each move is appended to the
  day's `moves.jsonl` and fsynced as it happens; `manifest.json` gathers the day's moves at the
  end, or when it stops - on an error, a journal that cannot be written (a full disk), or Ctrl-C. A stage that stops part-way removes only an artist every file of whom moved, rescans the others that lost a file, and says to run the same command again: files an earlier stage moved (in a journal, gone from the library, there at their destination) are left out rather than read as a mount problem, so the re-run finishes the job - Lidarr included, even when nothing is left to move. `--no-mount-check` is for the web UI's preview, which has no library mount,
  and is refused with `--apply`. `--out` writes the preview's totals, Lidarr plan and the sha256 of
  the decisions file it read, as JSON. A preview needs no Spotify client; only `--apply`
  re-plans. The dry-run prints every move *and* the Lidarr plan below.
  `--apply` first re-plans and refuses a manifest with any selected album that has gained a Spotify
  reason or a likearr ownership record since the report was written, naming each one; the report
  is a snapshot and the dry-run does not need it fresh.

### Prune decisions file

The prune review page (a human looking through `prune-report`'s output) exports a decisions file
instead of the operator typing `--artists` by hand:

```json
{"version": 1, "trash": ["<rg mbid>", "..."], "trash_artists": ["<artist mbid>", "..."],
 "promote": ["<artist mbid>"], "save": ["<artist mbid>"], "save_releases": ["<rg mbid>"],
 "save_exclude_releases": ["<rg mbid>"], "notes": "free text"}
```

`prune-stage --decisions FILE` consumes only two fields:

- `trash`: release-group mbids to stage, matched against `prune-report`'s `candidates` rows.
- `trash_artists`: artist mbids; every candidate row for that artist is staged.

`promote`, `save`, and the optional `save_releases` and `save_exclude_releases` (#55) are the review page's answer for the
Spotify-side step, which is `promote-save` (below); `prune-stage` only ever moves files, so it
prints an INFO line naming how many of each it saw and does nothing with them.

Validation:

- An id in `trash` or `trash_artists` that matches neither a candidate nor a protected row is
  **unknown**: it is listed as a warning and skipped, never silently dropped.
- An id in `trash` that names a **protected** row (the report marks a row protected when it is
  the only local copy of a liked track - see "The rules" above) is **refused**: `prune-stage`
  exits 1 and moves nothing. Protected rows are never reachable through `trash_artists`, because
  that field only ever draws from a report's candidate rows in the first place.
- `--decisions` is mutually exclusive with `--artists` and `--all-candidates`; giving more than
  one, or none, is a usage error.

### `promote-save`: the other half of the decisions file

The only command that writes to **Spotify**. The rule it exists under is the reason for every
constraint below: **promote-save carries out decisions made by hand in a Clean up review; likearr
never saves or follows on Spotify on its own.** It is not an ongoing mirror of the library, and it
never saves an album or follows an artist automatically, on any schedule, or as a side effect of
any rule: album saves and artist follows are high-intent, high-impact actions on a personal
account. In particular, a release that likearr monitors because of a playlist or liked-track rule,
or because of the followed-artist catalogue rule, never causes an album save or an artist follow.
Those rules exist to get the required tracks into Lidarr under the strictest match, and that is
where they stop.

So: **likearr performs no automatic Spotify writes, ever.** There is no scheduled mode here, no
`--scheduled`, and nothing in `run`, `adopt` or the resolver reaches this code. `prune-stage`
applied `trash`/`trash_artists`; this applies the others:

- `promote` → **follow** that artist on Spotify. Taken from `decisions['promote']` and from
  nowhere else; it is never widened by what Lidarr monitors.
- `save` → **save** the albums of theirs **that a human reviewed and kept**: present in the
  review snapshot *with files at review time*, and still holding files in Lidarr now.
- `save_releases` (release-group MBIDs, #55) → **save that one album**, which a human chose to
  keep and save on its own. The same rule as `save`, one album at a time: the album must be in the
  review snapshot with files at review time (it is looked up under the artist it was reviewed
  under) and still hold files in Lidarr now; anything else is listed in `unmatched` with the
  reason, and nothing is looked up for it. It never widens to the artist's other albums. A
  protected album can be saved: saving moves no file. An album named both ways is saved once.
- `save_exclude_releases` (release-group MBIDs, #55) → an album of a `save` artist that a human
  chose to **keep with no change on Spotify**. It is left out of that artist's save, and the dry
  run lists it as kept by hand.

The review snapshot also carries each album's own `save` flag (#55), from the same
decisions. `promote-save` cross-checks the two: an album is saved only when the decisions file and
the flag agree (a `save_releases` album must be flagged `save: true`; an album of a `save` artist
is saved when it is flagged true and not excluded, and left alone when it is flagged false and
excluded). Any disagreement is listed in `unmatched` and nothing is saved for it - a hand-edited
or mismatched pair of files is refused, never resolved either way. A snapshot with no flags
saves an artist's `save` albums on the decisions file alone, and refuses `save_releases` (which
needs the flag).

Plan → review → apply, the same shape as `run`:

```
likearr promote-save --decisions /data/decisions.json --reviewed /data/review-data.json
likearr promote-save --apply promote-save.json                 # executes exactly that plan
```

The plan carries a digest of all three inputs - the decisions file's `promote`, `save`,
`save_releases` and `save_exclude_releases` ids (the last two, and a snapshot's `save` flags,
change the digest only when present - pinned by a test), the
review snapshot, and the artists and reviewed file-bearing albums Lidarr held when it was planned
- and `--apply` recomputes all three and exits **3** if any moved, exactly as `run --apply` does.
`--force` overrides. The decisions and reviewed paths are recorded in the plan so
`--apply PLAN.json` alone works; `--decisions` / `--reviewed` override them.

#### `--reviewed` is mandatory, and there is no fallback

Deriving the save set from "albums this artist currently has files for" is library *state*, not a
decision, and it drifts. Between a review and a plan, albums keep arriving: from the
followed-artist catalogue rule (a followed artist's back catalogue downloading), from liked and
playlist track rules, and from outside likearr altogether. Not one of them was ever a decision.

The save set is therefore intersected with the review snapshot (`review-data.json`: a top-level
`artists[]`, each with `mbid` and `releases[]` of `rg` / `files` / `title` / `type`, and `save`,
#55). Only `mbid`, `rg`, `files` and `save` are read; a release shown to the reviewer with zero
files was not a keep.

If `--reviewed` is not supplied and the decisions ask for any save - an artist's `save` or a
single album's `save_releases` - the command **refuses**
(exit 1) and says why. It never falls back to current library state - an unreviewed album must not
become saveable by accident. A decisions file with only `promote` entries needs no snapshot,
because follows never come from library state in the first place.

Every candidate that the snapshot excludes is counted and listed in the plan's
`excluded_unreviewed`, and the count is printed, so the number is visible rather than invisible.

**A decision carried over from an earlier review never asks Spotify for anything.** Clean up
pre-fills a review from the ledger (see "Web UI"), but a past follow or save is carried over as a
**keep**, with a note saying what was done before - never as a follow or a save. "Idempotent" is
not enough here: a save carried over would also save an album added to that artist since, which
nobody reviewed, and a follow carried over would re-follow an artist unfollowed on Spotify in the
meantime. So every `promote`, `save`, `save_releases` in an export comes from a click in that
review. If the same artist is chosen again, `promote-save`'s list-and-diff makes the repeat write
nothing ("already followed" / "already saved").

#### Mapping MusicBrainz to Spotify: three tiers

There is no mapping table, so each id is mapped in three tiers, best first. The plan records
**which** tier produced every match (its `step`) and the dry run prints the breakdown, so a human
can skip the authoritative tier and spot-check the weakest one. A wrong match saves a stranger's
record into a real library, so anything short of an unambiguous answer goes into the plan's
`unmatched` list with a reason and is printed.

**Tier 1 - MusicBrainz's own Spotify relationship** (`ReleaseLinkLookup`; steps `artist:mb-rel`
and `album:mb-rel`). MusicBrainz records a *free streaming* URL relationship pointing at
`open.spotify.com`. An editor asserted that identity, so there is nothing to compare and nothing
to get wrong - and it costs **no Spotify quota at all**.

- Artists: `artist/<mbid>?inc=url-rels`.
- Albums: the link lives on the group's **releases**, never on the release group itself - checked
  against the MusicBrainz API, and it follows from the style guideline that streaming links belong to
  digital releases. So `release?release-group=<mbid>&inc=url-rels+media&limit=100` is read, which
  also yields the barcodes tier 2 wants: one MusicBrainz call per album, one cache entry, both
  answers. `limit=100` rather than a handful because a browse cannot sort and the Digital Media
  release is rarely near the front (1 of 39 on the *OK Computer* group).
- The relationship *type name* is not trusted, only where the URL points. `spotify_id_from_url`
  (pure, in `core.match`) accepts the web URL - optional `intl-xx` segment, any query string - and
  the `spotify:` URI; it requires a Spotify host, a 22-character base62 id, and the entity type
  that was asked for. A track link is not a weaker album answer, it is a different question, so
  it falls through instead of being trusted.
- Several *different* Spotify ids for one entity (regional catalogue duplicates) is **no answer**.
  They are probably the same record, but "probably" is not the bar, and the tiers below can say.

**Tier 2 - a UPC search** (step `album:upc`). Barcodes from that same cached browse, Official and
digital pressings first; up to `MAX_UPC_TRIES` (2) go through `GET /search?q=upc:<code>&type=album`.
A barcode identifies a release; a title only describes one.

**Tier 3 - a title search** (steps `artist:name`, `album:name`), `q=album:"…" artist:"…"`.

Tiers 2 and 3 are judged by `core.match`, which is pure:

1. **The artist gate runs before the title test**, on both paths. A UPC hit credited to someone
   else is refused - MusicBrainz barcodes do get typo'd.
2. **Normalised equality, never similarity.** `normalize_title` / `normalize_name` fold case,
   accents, punctuation and bracketed qualifiers ("(Deluxe Edition)", "- Remastered 2011"). There
   is no edit distance and no prefix rule: *Ghosts* does not match *Ghosts I-IV*.
3. **A tie is a miss, unless one candidate is literally the title.** Folding makes "Blue" and
   "Blue (Deluxe Edition)" equal, which is the point; when several survive, the one whose raw
   title is the one asked for wins (step `…:literal`) and anything else is `ambiguous`.

#### Idempotency, quota and resuming

- Membership - "is this already followed / already saved?" - is answered by **paging the two
  library list endpoints** once and testing against the resulting sets, at plan time *and again*
  at apply time. So the plan shows only real changes and a half-finished apply is safe to simply
  run again. The writes are idempotent anyway.
  Not `GET /me/following/contains` / `GET /me/albums/contains`, which are the obvious calls and
  answer **403 Forbidden** on a Development Mode app while `GET /me`, `GET /me/following`, `GET /me/albums`
  and `GET /search` all answer 200 on the same token - a Development Mode restriction of the same
  family as non-owned playlists returning no items, and not fixable by scope or consent. There is
  no `/contains` fast path with a fallback: a fallback that never runs is never known to work.
  For a typical account this is a few pages each, a handful of requests per run.
- Every `search` response is cached on disk (`spotify_search_cache`, in the state DB on its own
  connection, like the MusicBrainz cache). A re-plan after a quota error, an interrupt or a review
  repeats no calls, which is what "resumable" means here.
- Searches are rate-limited (one per `0.5 s` by default) and counted against a budget of 600.
  Exceeding it stops the pass, keeps everything matched so far, writes the plan and marks the rest
  `unmatched` with that reason. A burst of several hundred searches can exhaust a Development Mode
  quota.
- **Tier 1 spends no quota**, so the budget is driven by how much of the library MusicBrainz
  already links. For N `promote` artists and M kept albums the worst case (no relationships
  at all) is N + up to 3M calls; every relationship hit removes 1 call for an
  artist and 1-3 for an album, and a library fully linked on MusicBrainz costs **zero** searches.
  The coverage is not knowable in advance - the first dry run's tier breakdown is the measurement,
  and it is printed for exactly that reason.

#### Scopes

`promote-save` needs `user-follow-modify` and `user-library-modify` on top of the read scopes.
Spotify grants scopes at consent time and a refresh never widens them, so the command reads the
scopes the stored token actually carries and **refuses** with an actionable message when any are
missing. It never re-authorizes on its own: that is a browser round trip only the user can make
(`likearr auth --manual`).

### Telling Lidarr after a stage

Moving files does not tell Lidarr anything, so `prune-stage --apply` follows up per artist, and
the dry-run prints the same plan with a reason per artist:

- **Remove the artist row** (never files) only when *all four* hold: no row of theirs is
  protected, every candidate of theirs was selected, likearr owns no release of theirs
  (`owned_releases`, see Ownership), and Lidarr's live count of track-file records for the artist
  (`GET /trackfile?artistId=`; never the `trackFileCount` statistic, which need not agree) equals
  the files Lidarr lists now for the albums being staged - every one of which this stage moves, or an
  earlier stage already moved - so nothing else of theirs is on disk. Counted from Lidarr now, not
  from the report, so a stage re-run after one that stopped part-way still removes the artists it
  finishes, before Lidarr's rescan of the first stop or after it. The last two guards catch
  removals the first two alone would make: followed artists whose only *candidates* were extras,
  and artists with files the report never listed.
- **Otherwise rescan** the artist's own folder: `RescanFolders` with `folders=[artist.path]` and
  `artistIds=[id]`, queued without waiting. Lidarr drops the track rows for files that left.
  A `RescanFolders` with no folders walks every root (see Upstream quirks), and Lidarr has no
  `RescanArtist` command at all, so an artist without a path is refused rather than rescanned.
- An apply should start with an **idle Lidarr command queue**: Lidarr loops forever if an artist
  is deleted while a `RefreshArtist` for it is in flight.

## Web UI

`likearr start` is one service: the web UI, the scheduler and the job runner, all in one
long-running process (#68). The UI itself is a small admin page for what would otherwise need a
terminal: is it working, change a setting, and why is this release (not) monitored.

**Shape.** Starlette, Jinja2 and a vendored htmx (2.0.11, `likearr/web/static/htmx.min.js`, sha256
`d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`, 0BSD) - all core dependencies,
since `likearr start` is the whole product. No CSS framework, no build step, no CDN: the page
renders with no internet. The same image runs it as the always-on compose service, and one-off
CLI commands in a run-once service beside it.

**The server reads; children work.** The server opens the state database per request, reads the
`runs` table and the job directories, and never builds a `Context`. Anything that talks to
Spotify, Lidarr or MusicBrainz is a `likearr` child process - exactly the command a human would
type, so the CLI stays the single implementation by construction. That is also why every Spotify
token refresh stays under the token file's lock (#28): the playlist picker is `likearr playlists
--json` as a child, never an API call from the server. The one exception is reading
`authorized_at` out of the token file for the re-auth countdown (`read_authorized_at`): a lock-free
read of one field of an atomically-replaced file, which never refreshes and never returns a token.

**Jobs** (`likearr/web/jobs.py`) are directories under `<config dir>/ui/jobs/<id>/` holding
`meta.json`, `log.txt` (stderr) and `out.txt` (stdout), so a job survives a restart and is readable
from a terminal. Ids are checked against `^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{6}$`
before any path is built; arguments are a list with no shell, and free text goes after `--`. One
job at a time, refused before spawning. A job that runs `likearr run` first takes the run lock
without waiting and lets go at once, so a scheduled run in progress is a friendly "try again"
rather than a child that publishes a lock error (for an apply, to retained MQTT). Exit codes map
to states: 0 done, 2 guarded, 3 stale, 4 busy (another run holds the lock; #54), 1 failed, and
cancelled / interrupted. A job still `running` at startup is marked `interrupted`, unless its
process is provably still that job (below), when it is re-adopted; on SIGTERM new
jobs are refused, a job marked `drain` (an apply) is waited for, and anything else is
stopped - except a `scheduled` job, whose wait-or-stop decision is its own apply phase, not the
static `drain` it started with (see "Redeploy safety", under "The scheduler"). The newest
20 job directories are kept, pruned each time one starts. The child never
inherits `LIKEARR_UI_PASSWORD`: `start` takes it out of its own environment as it reads it. The
log tail is redacted again before it is rendered, on top of likearr's own redacting log handler
(`tests/adapters/test_log_secrets.py` drives every credential-handling path at DEBUG and checks
stderr). Stdout is the answer - artist names, titles, the playlists JSON the server parses - so it
is read verbatim and shown with only the exact values of the environment's secrets removed: the
log's pattern redaction would turn "Basic Channel" into "Basic REDACTED" and break a playlist name
that looks like a header.

**Stopping a job never strands a Spotify token.** Cancel and a container stop send the child
SIGTERM. Spotify rotates the refresh token on a refresh, so a child killed between the answer and
the save would leave a used refresh token on disk and every later run failing until `likearr auth
--manual`. `SpotifyAuth` holds SIGTERM and SIGINT pending from the token request until the new
token is saved; the process then stops as asked. The token endpoint gets its own short retry
profile (2 attempts, 30 s of timeouts each, at most 5 s of backoff), so that window is at most
`TOKEN_REQUEST_WORST_CASE_S` (65 s), and the job runner waits `STOP_WAIT_S` (two minutes, derived
from it) before SIGKILL. Cancel returns at once; the job page says "Stopping" until the child goes.

**Progress** is polled, not pushed: a running job's fragment re-fetches itself every 2 s, and a
finished one answers HTTP 286, htmx's "stop polling". Polling needs nothing from the reverse proxy,
where websockets and SSE both would. The phase shown is coarse ("Reading Spotify", then resolving
after the existing `sources read:` log line) because the resolver has no finer progress yet.

**Access.** One shared password, `LIKEARR_UI_PASSWORD`, compared in constant time; `start` refuses
to start without it, or with one shorter than 16 characters counted as given (issue #170), because
the login pause is per address and cannot protect a short or dictionary password. The login pause is checked after the request body has arrived, in the same
uninterrupted step as the comparison and the recording of a failure, so requests already in
flight cannot all get past it. The login form must be urlencoded and is parsed with tight limits,
and every request body is capped at 1 MB before any route reads it: the login route is open to
anyone who can reach the port. Logout moves a server-side session generation on, so a copied
cookie stops working too; a signed cookie cannot be revoked any other way. A login sets a signed `SameSite=Strict`, `HttpOnly` session cookie, `Secure`
when the request came over https; the signing key is
made at startup, so a restart logs out. `SameSite=Strict` is never loosened for any route -
Spotify's direct-callback mode (`/spotify/callback`, issue #79) is instead exempted from the login
gate entirely and authorizes itself with a single-use, server-side PKCE `state` (see "Connect
Spotify", below), rather than depending on this cookie reaching it on a cross-site redirect. Five
failed logins from one TCP peer in a minute pause that address for a minute. No forwarded header
is trusted for any access decision (uvicorn runs with `proxy_headers` off), and there is no
"local addresses need no password" switch. Under the login: `AllowedHostMiddleware` with
`LIKEARR_ALLOWED_HOSTS` plus the loopback literals (DNS rebinding) - likearr's own small middleware rather
than Starlette's `TrustedHostMiddleware`, so a refusal can name the host it refused and the setting
that would admit it (issue #169) instead of a bare "Invalid host header" - and on every unsafe method a
cross-origin check after Go 1.25's `CrossOriginProtection` - `Sec-Fetch-Site` of `same-origin` or
`none`, or without it, `Origin` naming the same host as `Host`. Browsers send `Sec-Fetch-Site`
only over https, so on the LAN's plain http the `Origin` check is the one that runs. No GET
changes anything. Every response carries a `default-src 'self'` content security policy - 500s
included, because that middleware wraps the whole Starlette app rather than sitting inside it.
The open paths (`/login`, `/healthz`, `/static/`) are matched exactly, and websockets are refused.

**`[ui]` never stops a run.** Every command loads the same `config.toml`, so a problem in the
UI-only block, or in `LIKEARR_ALLOWED_HOSTS`, is recorded (`UiConfig.errors`), never raised: `start`
refuses to start on one, and the Status page shows one that appears later. `LIKEARR_ALLOWED_HOSTS`
takes host names and IPv4 addresses without a port, comma-separated; `AllowedHostMiddleware`
compares only what precedes the first ":" of the Host header, and only the first Host header when
a request carries more than one. Unset (issue #3), it accepts loopback and any IPv4 literal and
refuses every host name: a rebinding page always sends its own domain as the Host header, never a
bare address, so the literals do not open the rebinding path, and the login still gates the UI.
"Trust the first host seen" was rejected because a rebinding page could win that race.

**Deployment settings are environment-only (issue #3).** Where likearr runs and how it is reached -
`LIKEARR_LIDARR_URL`, `LIKEARR_ALLOWED_HOSTS`, `LIKEARR_MUSICBRAINZ_CONTACT` - come from the
environment, like the secrets; everything the web UI edits stays in `config.toml` and has no
variable. One source per setting, so nothing to reconcile. The file's old `[lidarr] url`,
`[ui] allowed_hosts` and `[musicbrainz] contact` fail the load by name. `likearr start` writes
`config.toml` from the example on a first start with none; `[lidarr] root_folder` and
`quality_profile` start unset and are picked in Settings from a Lidarr setup preview's lists (one
root folder is taken by itself), and until both are set every run refuses.

**Navigation and look.** The pages are named for what a person does, not for the CLI command behind
them: **Status** (`/`), **Review changes** (`/plan`: check for changes, review them, apply them),
**Look up** (`/explain`: why a song, album or artist is in Lidarr, or not), **Not added**
(`/unmatched`: what couldn't be added), **Clean up** (`/prune`: the prune review, shown only while
`[prune] enabled` is on - see below), **Settings**
(which also holds Doctor, a read-only check; `/doctor` redirects to `/settings#doctor`). Only the
labels are in a person's words; the URLs stay short and stable. Every page shares one stylesheet
(`likearr/web/static/style.css`, no framework): a spacing and a type scale, one accent colour, three
status colours (ok, attention, problem) with soft backgrounds, and the same buttons (one `primary`
per page, for the action the page is for; everything optional sits under a collapsed "Advanced
(optional)"), cards, tables, pills and disclosures. Light and dark follow the system, and every page
is a single column on a phone.

**Status** reads the last 20 runs (`SqliteState.run_history`, which pulls each run's guards and
`projected_wanted` out of the stored diff in SQL). The last *applied* run (an apply whose status is
ok, guarded or degraded, or one that failed part-way, having changed some of Lidarr) is shown apart
from the last run of any kind, because every dry run is recorded too. A part-way apply reads
"Stopped part-way: N of M changes made" with its cause, or "Stopped part-way: Lidarr settings may
have changed, no planned change made"; only a failed apply that wrote nothing reads "Failed, and
changed nothing" and is not the last applied (#54). A hand-run that found the lock held (exit 4)
reads "Didn't run: another run held the lock", quietly. A part-way apply also supersedes
open checks, as any apply that changed Lidarr does. It spells out what moved (the `*_new` counts and new conditions), never the chronic
totals. Projected wanted is labelled as the last plan's projection, not Lidarr's live list. The
next scheduled run comes from `[schedule] cron` in `[schedule] timezone` (`core/cron.py`, a
five-field parser), computed the same way the in-service scheduler itself does (see "The
scheduler", below) - unless `[schedule] enabled` is false, when the card reads "Paused since
&lt;time&gt; - &lt;reason&gt;" instead of a time, never both (issue #68). Beside it, "Last scheduled
fire" names the newest `scheduled`-kind job and its outcome, with a link to its job page. A "Run
now" button submits the same job the scheduler itself would, through the same lock and the same
queue (`fire_now`, see "The scheduler"). The Spotify re-auth date is
`authorized_at` plus six calendar months: since 2026-07-20 Spotify refresh tokens die six months
after authorization and a refresh does not extend that. Every authorization (`likearr auth` or
Settings) records the date; a token written before likearr recorded it shows the date as unknown
until it is re-authorized from Settings. A missing state database is reported
(unhealthy, "no state database") rather than opened: opening would create an empty one.

**At a glance** leads the Status page, above everything else, and uses only stored data. It is a
health banner (colour, icon, one sentence, its links), then four cards - the last run, the next
run, how much of what you want is downloaded, and how much couldn't be added - and, collapsed under
"Details", the tables behind them:
- **Health, in words.** It says "All good" or "Needs attention", and lists what Home Assistant's problem flag and stale sensor would show (DEPLOY.md). It reads the newest run that reached Home Assistant, however far back (`SqliteState.last_published_run`): not a dry run, and not a reviewed diff refused because the settings changed (a local-only `stale`, stored with no diff). It lists:
  - a failure or a stale refusal
  - each new condition, in `CONDITION_TEXT`'s words
  - a guard that held unmonitors back (an advisory one, which holds nothing back and leaves Home Assistant green, is a note)
  - no run for 13 hours (the example `binary_sensor.likearr_stale`)
  - no run reaching Home Assistant at all yet (it fails closed)

  Each item links to where the page explains it: a new name collision links to the collision cards.
- **Last run.** When, whether it applied or was a dry run, and what changed.
- **Next scheduled run.**
- **Coverage.** Taken from `last-run.json` and labelled with that run's time. It gives:
  - the songs, albums and followed artists read from Spotify
  - each counted once by its reason key, in exactly one of: matched to a release, a followed artist matched, could not be matched (the true misses), two different artists share the name and the title (#32), a lookup that failed this run (retried next run), left out by the settings (`is_excluded`: a remix, a compilation, a denied release), or a liked single waiting for its album - so the rows add up. Each links to its part of `/unmatched` (Not added), in Look up's words
  - the releases the matches want, and of those, how many are monitored, downloaded (files on disk as the run read them), waiting for a download, or not monitored - after a dry run, "not monitored yet", with how many the plan would monitor

  Before the first run that records one, it falls back to the health record's unmapped, pending and projected counts. It never calls Lidarr.

The re-auth date, projected wanted and the source counts move to "More", below.

**Not added** (`/unmatched`, `likearr/web/unmatched.py`, #60) lists what the last run monitors
nothing for, from the same `last-run.json`, and calls no service. It is built to be worked through:

- **Cards** first, one per group, in one row, like Status and Clean up: Couldn't be matched, Two
  artists share the name, Left out by your settings, Waiting for an album, Lookup failed this run.
  Each card's number counts songs, saved albums and follows exactly as Status's line for the group
  does (every reason key the run read once; a key with no resolution is a miss), so the two pages
  agree for the same run; the releases they are on are shown under it. A Lidarr catalogue gap is a
  match on Status, so it is counted beside the number ("and 12 releases Lidarr doesn't list"),
  never in it. Each section's heading says its card's numbers in its card's words ("Couldn't be
  matched: 40 songs, albums or follows · 30 releases, and 12 releases Lidarr doesn't list"), and
  under a filter adds how many rows match; a reason's heading counts its own rows and names the
  unit ("(58 releases)", "(3 artists)").
- **One row per release, not per song.** Rows are keyed by release group when likearr knows one
  (a Lidarr gap, a left-out release), so two releases that share an artist name and a title stay
  two rows; otherwise liked songs from one Spotify album with the same outcome are one row ("Some Artist - Live In Concert", "5 liked songs", listed on expand). A
  saved album is a row. A followed artist likearr could not find is a row of its own kind.
- **Grouped by why.** Inside a group, rows are split by the resolver's step in words (`reason_for`):
  "MusicBrainz has no release by this artist with this title", "Lidarr is missing this release", "A
  Various Artists compilation", "Left out: a remix", and so on. Each part says what can be done, if
  anything. A step the table doesn't know (a newer resolver's) reads "likearr couldn't match it" and
  never fails the page.
- **Lidarr's catalogue gaps** (a plan's `lidarr:*` unmapped: the artist is in Lidarr, the release
  group is not) are not resolver misses, so the last run records no resolution for them. They are
  worked out again from what it does record: a wanted release whose artist Lidarr holds, with no
  album, that the plan's monitors don't include. A liked or saved release is "Lidarr is missing
  this release"; one only a follow wants is "MusicBrainz has it, but Lidarr's catalogue doesn't
  yet", or "New on MusicBrainz" within `recent_release_days`. A file from before the monitors were
  recorded lists none.
- **Where it came from**: liked song, saved album, followed artist, or the playlist by its cached
  name (never its id).
- **Actions are links only**: Look up (`/explain?query=` the album or artist, cut to Look up's 200
  characters; Look up matches one name at a time, so the artist can't narrow it), MusicBrainz (the
  release group when known, else an advanced search with the quoted title and artist), Open in
  Lidarr when Lidarr holds the album or the artist, and for Left out, the Settings field that
  decides it (`/settings#field-rules.allow_remix_releases`). There is no write on the page.
- **Filters and pages** like Clean up: search (artist, album or song), a group or reason, a source
  (a kind, any playlist, or one playlist), and a sort (artist A-Z, or most songs first). Each part
  pages 25 at a time on its own over htmx (`/unmatched/part`); a filter change swaps every section
  (`/unmatched/rows`). Every filter value is checked against what the page offers.

**Settings** (`likearr/web/settings.py`) edits the live `config.toml`, which is the source of
truth - any copy kept elsewhere (a config repository, a backup) is documentation, and must never
be copied back over it. An allowlist governs reading and writing: `[rules]` liked_track_scope,
singles_fallback_days, allow_compilation_fallback, allow_remix_releases, keep_remix_only_tracks, deny_releases,
recent_release_days, albums_only_tag; all of `[guards]`; `[spotify]` playlists, followed_artists,
saved_albums, liked_tracks. Nothing else is rendered - no path, URL, contact, `[health]`, or
environment value. A save: (0) refuses if the file's sha256 differs from the one the form was
rendered with, so a hand edit made meanwhile is never reverted; (1) `tomlkit` round trip, keeping
every comment; (2) sets only the allowlisted keys whose value changed, so an implicit default stays
implicit; (3) validates with `parse_config`, the loader a run uses; (4) copies the old file to
`config.toml.bak-YYYYMMDD-HHMMSS`, keeping the newest 30 (hand-made `.bak-DATE-tag` backups never
match and are never pruned); (5) writes a temp file and `os.replace`s it. A change that re-resolves
(liked_track_scope, the two opt-outs, deny_releases) or loosens any guard (every guard is looser
when its number goes up) needs a second confirm, and so does switching a Spotify source on or adding a
playlist: the next scheduled run monitors everything it resolves to, with no cap (monitors have no
per-run cap; `projected_wanted_max` only warns). A guard must be a finite number. A confirm is
bound to exactly what it showed: its page carries a digest of the file the save would write, and
only that digest lets the save through, so neither a hand-made `confirmed` field nor a value
changed on the way back skips it. Lists compare as
sets and keep the file's own order, and an existing array is edited in place, so an untouched form
changes nothing and the comments inside `deny_releases` survive a save.

**Connect Spotify** (`likearr/web/spotify_connect.py`, #79) is the one Spotify-token write
the server process itself performs, rather than a child job like every other write in this
section. It still reuses `adapters.spotify.SpotifyAuth` end to end - `build_authorize_url` and
`exchange_code`, the same calls `likearr auth --manual` makes - so there is exactly one PKCE
implementation; only the two small calls that build a throwaway `SpotifyAuth` are its own
(`spotify_connect.build_authorize` / `.exchange`). Reasons for in-process rather than a job: the
exchange needs no Lidarr client, no MusicBrainz client and no state database - the full `Context`
a job's `likearr` process builds is unneeded weight - and, more importantly, a child job would
need the PKCE code verifier as a command-line argument, visible in `ps` to anyone on the host with
a shell for as long as the argument list survives, which the rule "never render or log the
verifier, code or tokens" rules out. It runs off the event loop (`anyio.to_thread`), behind the
same login and cross-origin gates every other route is (except `/spotify/callback`, below), and
the token file is still written under `SpotifyAuth`'s own `spotify-token.json.lock`, so a
scheduled run's own refresh cannot race it. **Trade-off**: because this write bypasses `JobRunner`,
it gets none of a job's own record for free - no `<config dir>/ui/jobs/<id>/` directory, no
`/jobs/{id}` page. It is not silent, though: one `log.info` line records the outcome (never the
code, verifier or token), and Status and Settings both read the token file's `authorized_at` and
granted scopes afterwards (`read_authorized_at`, `read_granted_scopes` - already used for the
re-authorize countdown) and show "Connected `<date>` (scopes: `...`)" - so the effect is always
visible even though there is no job history entry naming the click that caused it.

PKCE `state`, minted by `SpotifyAuth.build_authorize_url` (`secrets.token_urlsafe`, ~128 bits), is
the *entire* authorization for finishing the flow - not a value merely checked against something
else. It lives server-side, in `PendingSpotifyAuthStore` - an in-memory dict on the running server,
never the session cookie and never the database - ten minutes old at most, and consumed (removed)
the moment it is checked, matched or not, so a replay of the same pasted URL or callback is refused
the same way an unknown one is; `consume` matches it against each stored key with
`hmac.compare_digest`, not a dict lookup's hash-then-`==`, for the same reason
`auth.password_matches` does. That `state` is minted only from `spotify_connect_start`, a route
behind the login gate, is what makes it sufficient on its own: only a session that was logged in
when the flow began can ever hold a valid one, so there is nothing to gain by also binding it to
the session cookie - and binding it would have meant loosening `SameSite=Strict` for the direct
callback below, which the login gate exemption avoids instead (see "Access", above, and
`auth._OPEN_PATHS`). Two modes, chosen when the flow starts:

- **Paste-back (the default, works everywhere).** The same redirect URI the CLI uses
  (`[spotify] redirect_uri`, a loopback IP literal); the authorize link is shown, Spotify's
  redirect fails to load in the browser (expected: nothing is listening there), and the user
  pastes the address bar back. `SpotifyAuth.parse_redirect_url`, already used by `auth --manual`,
  reads the code and state out of it. `POST /settings/spotify/finish` stays behind the login gate
  like every other Settings action.
- **Direct callback**, only offered when `[ui] public_url` is a configured `https://` address:
  `build_authorize_url`'s `redirect_uri` becomes `<public_url>/spotify/callback` (also validated,
  by `_check_https_redirect`, since it is not the loopback address `_check_loopback_redirect`
  checks), and `GET /spotify/callback` finishes the exchange when Spotify redirects there. The
  Connect POST answers with a Settings page carrying a plain same-tab "Continue to Spotify" link,
  never a 303 to Spotify: every page's CSP has `form-action 'self'`, which Chromium and WebKit
  browsers check on each redirect of a form submission, so a redirected POST is silently dropped
  there (only Firefox follows it). A link click is not a form submission. The callback itself is
  a cross-site top-level GET redirect from `accounts.spotify.com`, which a `SameSite=Strict` cookie
  is never sent on - so this one route, and only this route, is exempted from the login gate
  (`auth._OPEN_PATHS`) rather than loosening the cookie that gates every other page. The handler
  never reads or writes `request.session`: with no cookie attached it would be handed a fresh,
  empty one, and writing to it would make the response set a new, unauthenticated session cookie -
  silently logging out the real session the next time the browser sent both. It renders a small
  standalone result page (`spotify_callback.html`) instead, with a plain link back to Settings; a
  click on that link is a same-site navigation, which does carry the `Strict` cookie.

A successful exchange shows the granted scopes and the new authorization date (Status's
re-authorize section and Settings' Spotify panel both read the same
`read_authorized_at`/`read_granted_scopes`, so they clear and update on their
own); a failed or abandoned attempt leaves the old token file exactly as it was, since
`SpotifyAuth._save` only ever runs after a successful token response. Every code path that could
log or render the pasted URL redacts its `code=` parameter (`logging_setup._REDACT_PATTERNS`),
which also catches uvicorn's own access-log line for `/spotify/callback?code=...`.

**Lidarr setup and Doctor** (`likearr/web/lidarr_setup.py`, `likearr/web/doctor.py`, #80)
reuse `setup-profiles`' and `doctor`'s own planning rather than a second implementation: both
commands have `--json`, and the web routes start them as ordinary `JobRunner` jobs
(`lidarr-setup-preview`, `lidarr-setup-apply`, `doctor`) and parse the one line of JSON they print.
`setup_profiles_command` builds a `SetupProfilesPlan` shared by its text and `--json` output,
including a diff for an existing metadata profile whose allowed album types do not match what
`ensure_metadata_profile` would create (`adapters.lidarr.metadata_profile_diff`, compared against
`LidarrClient.metadata_profile_details`, the raw profile resources). That reuse matters because
`ensure_metadata_profile` never edits a profile that already exists under the wanted name - it
only ever creates one that is missing - so a differing profile is surfaced in the preview and
excluded from `todo` (and so from "needs apply"): showing it truthfully means saying it will be
kept as is, not implying `Apply` would fix it. The root folder is different: `likearr` owns its
monitor defaults, so `set_root_folder_defaults` *does* overwrite an existing root
folder's `defaultMonitorOption`/`defaultNewItemMonitorOption` back to "none" - a differing root
folder is `todo`, not kept as is, and the preview says so ("defaults will be set to..."), unlike a
differing profile ("kept as is, never overwritten"). Both share the same `status` value ("differs")
for "exists but doesn't match", which is exactly why each entry the plan builds also carries
`applies: bool` - `False` for a differing profile, `True` for a differing root folder - so nothing
downstream (`web.lidarr_setup.LidarrSetupView`, its templates) has to special-case "root folder"
vs "metadata profile" to know whether `status == "differs"` means "kept" or "about to change";
`LidarrSetupView.kept_as_is` reads `applies` for exactly that reason, never `status` alone.
Applying is a second confirm (`POST
/settings/lidarr-setup/{job_id}/apply` once
without `confirmed`, once with) that spawns `lidarr_setup.APPLY_ARGV` - `["setup-profiles",
"--apply"]`, fixed in code, never built from anything a request carries - the same pattern
`scheduled_argv` uses for the scheduler. Doctor is read-only by construction: its only POST starts
a `doctor --json` job, and the section renders whatever the newest one answered.

Both are sections of Settings (`#lidarr-setup`, `#doctor`, #85), not pages of their own. Each
is one htmx fragment (`_lidarr_setup.html`, `_doctor.html`) that carries `hx-trigger="every 2s"`
only while its job runs; the poll that sees the job finish is answered 286 and stops. The whole
Lidarr setup flow swaps in place - preview, table, the inline "This writes to Lidarr:" confirm,
the apply - and the poll that sees an apply finish cleanly queues a fresh read-only preview on
`load`, the re-check; opening Settings later never starts one. Nothing runs because Settings was
opened: Doctor asks Spotify, whose quota is small. The Doctor section renders outside
the form's `config_error` branch and reads only the job store, so it is there exactly when
config.toml is broken or the state database does not exist yet. It lists failures, then
warnings, and folds passes and skips into a `<details>`; it does not group checks by area, since
that would need a name-to-area table kept in step with `doctor_command` by hand. The
`/settings/lidarr-setup/{job_id}` and `/doctor` GETs redirect to their sections.

**Pause and resume** (#68) live outside that allowlist, on their own two routes
(`POST /settings/pause`, `POST /settings/resume`), not through the general form: pausing and
resuming are actions, not field edits, and the confirm rule for each is fixed rather than derived
from what changed. Both write `[schedule]` through the same validated, backed-up, atomic
`write_config` every other save uses. Pause sets `enabled = false`, an optional one-line
`paused_reason`, and `paused_at` (a real TOML datetime, the moment the UI wrote it) and **saves at
once** - it can only ever stop an unattended apply, never start one, so it is the one write on this
page that skips the second confirm outright. Resume sets `enabled = true` and **always** needs the
second confirm, `SaveCheck.confirm`, whether or not anything else changed: it turns unattended
applies back on, the same class of change as loosening a guard. `[schedule]` sits outside `[ui]`
- `[ui]` never stops a run - and outside `Config.plan_fingerprint`, so pausing can never make a
reviewed plan stale. The in-service scheduler reads the same block.

**The scheduler** (`likearr/web/schedule.py`, #68) is a background thread started in
the app `lifespan`, one per server. Why in-service rather than host cron: the *arr apps, Cleanuparr,
Maintainerr and the rest of the ecosystem all own their own schedule; a newcomer should need
`docker compose up -d` and a browser, not a crontab line. Each loop iteration re-reads
`config.toml`, computes the next fire with `core.cron.next_fire` from a fixed reference point, and
waits for it in chunks of five minutes (`_RECHECK_S`) rather than one long sleep - so a Settings
edit to the schedule is honoured within five minutes rather than only at the old fire time, without
a restart. When it fires, it submits exactly one command, fixed in code and never touched by the
UI: `run --scheduled --apply` (`scheduled_argv`). The UI can change *when* this fires; it can never
change *what* runs.

- **Why a child process, still.** Every design rule in "Web UI"'s job runner section applies to a
  scheduled fire exactly as it does to a UI click: isolation from an OOM kill or a crash, a working
  cancel, and the CLI as the one implementation. The scheduler's only job is deciding *when* to call
  `JobRunner`, never *how* to run something itself.
- **Paused stays one code path.** `[schedule] enabled = false` is not read here. The scheduler
  fires on schedule regardless, exactly as if it were not paused, and the child
  (`run_command` in `shell/run.py`) is the one that reads the pause and publishes
  `RunStatus.PAUSED` without doing any work - the one place that handles the pause. The
  alternative -
  this module skipping the fire and publishing PAUSED itself - would need a second place that knows
  how to publish a health record, for a state that is already cheap: a paused child exits before
  touching Spotify or Lidarr. One path, chosen for boringness over saving one `Popen`.
- **The queue, not a refusal.** `JobRunner.submit_scheduled` (`likearr/web/jobs.py`) waits rather
  than refusing: a fire that lands while a UI job (a check, an apply, a Clean up preview)
  holds the one job slot queues behind it for up to an hour (`QUEUE_WAIT_S`), woken the instant the
  slot frees rather than polling, and gives up and is recorded `JobState.SKIPPED` - a job directory
  like any other, with no `pid` and no exit code, and the reason in its `log.txt` - if the hour
  runs out. The run lock itself being held by something *outside* this server (a host cron line,
  say) is not queued: `submit_scheduled` checks it once, up front, and records `skipped`
  at once if it is held - the collision the lock exists to catch, reported the way a scheduled run
  that lost the race reports itself (`RunStatus.SKIPPED` in `shell/run.py`). A UI action started
  while a scheduled job is running is refused outright with "a scheduled run is in progress".
  "Run now" (`fire_now`) goes through this exact same function, so a manual kick is subject to the
  same lock and the same queue as a real fire - the *arr "Run now" pattern.
- **Missed fires.** The scheduler's last fire time is persisted
  (`SqliteState.record_scheduled_fire` / `last_scheduled_fire`, schema 6). At startup, if the
  schedule's next fire strictly after that recorded time is already due, the service catches up
  **once**, five minutes after startup (`MISSED_FIRE_DELAY_S`) - never once per missed fire, which
  is APScheduler's coalesce behaviour. A service that
  has never fired before - no record, including a freshly upgraded state database - does not catch
  up: there is nothing to have missed.
- **Single worker.** Two uvicorn workers, or `--reload`'s own subprocess, would each start their
  own copy of this thread and fire every job twice. `likearr start` never exposes `--workers` or
  `--reload`, so `assert_single_worker` (checked once, in the lifespan, before the thread starts) is
  a tripwire against ever adding either without updating this module, not a real-world guard.
- **DST.** `core.cron.next_fire` already resolves the spring-forward gap and the repeated autumn
  hour to a real, strictly-later moment (`core/cron.py`); the scheduler adds nothing on top - it
  only ever asks `next_fire` for the answer and waits for it.

**Redeploy safety.** A scheduled run is a child of the same process a redeploy stops. These rules
make that safe.

- **Where planning ends and applying begins.** `shell.apply.apply` has a single point after which it
  is no longer safe to cancel: `plan()` returns, the diff to execute is chosen (and, for a saved
  diff, checked for staleness - never reached by `run --scheduled --apply`, which always plans and
  applies in one call), and then `_execute` runs, whose very first phase writes to Lidarr (the tag,
  then the metadata profiles: phase (a) of `apply`). Everything before that line only reads.
  Right before it, `apply` writes one line to stderr, `PHASE_MARKER_APPLY` (`likearr.models`,
  `"likearr-phase: apply"`) - never on a dry run, since a dry run never calls `apply` at all.
- **The shutdown rule.** `web.jobs.JobRunner.shutdown` (SIGTERM) does not use a `scheduled` job's
  static `drain` - the flag every scheduled job is started with, which gives it process isolation
  (`start_new_session`) and its refusal of a manual Cancel. Instead
  `JobRunner._must_drain` looks at whether that line has appeared in the job's own `log.txt`: if
  not, the job is still planning - nothing has reached Lidarr - so it is SIGTERMed and recorded
  `interrupted` with the reason `"cancelled for shutdown during planning"` in its log; if it has,
  the job is drained exactly as a UI apply is, because Lidarr may already be
  half-changed. The same check runs in `JobRunner.recover` for a job that did not survive a hard
  restart (the whole container gone, not just a graceful SIGTERM) - the simplest correct rule,
  because both cases boil down to the same question, "did this job ever reach the point of no
  return", answered the same way from the same file.
- **The missed-fire catch-up must re-fire a cancelled slot, not skip it.** The scheduler already
  persists its last fire time (`SqliteState.record_scheduled_fire`), and one more column (schema 7),
  `scheduler_state.cancelled`, is set by `SqliteState.mark_scheduled_fire_cancelled` and read by
  `scheduled_fire_cancelled`. `JobRunner`'s `scheduled` after-callback (`_after_scheduled` in
  `web/context.py`, wired for both a graceful shutdown and a hard-restart recovery) marks it when a
  scheduled job settles `interrupted` without having reached the apply phase. `Scheduler._catchup_time`
  then treats a cancelled fire as due at *its own* recorded time rather than the schedule's next
  slot after it - that exact slot never ran, so it is what is owed, not whatever comes next. A
  fresh fire (`record_scheduled_fire`) always clears the mark: only the fire it belongs to can be
  the one that was cancelled.
- **Spotify snapshot reuse.** The MusicBrainz work a plan does - up to an
  hour on a cold cache - is already saved lookup by lookup, so a re-fired run answers from cache
  with no network; only the Spotify read (`SpotifySource.read`, a few dozen requests) needs
  saving, and `shell.spotify_snapshot` saves it: a *scheduled* run's `plan()` writes its
  `SourceSnapshot` to `<data>/ui/spotify-snapshot.json` (mode 0600) the moment the read finishes,
  keyed by when it was read and a digest of `[spotify] followed_artists` / `saved_albums` /
  `liked_tracks` / `playlists` (`sources_digest`). A later *scheduled* `plan()` reuses it - skipping
  `ctx.source.read()` entirely - only while it is under `MAX_AGE_S` (30 minutes) old and that digest
  still matches; a hand run (`run`, `run --apply`, a UI plan) never reads or writes this file. It is
  deleted at the end of every `plan()` that completes, scheduled or not: a completed plan means
  Spotify and Lidarr were both just read fresh, so anything saved earlier can only be staler.
  Serialisation reuses `shell.last_run.snapshot_to_dict` / `snapshot_from_dict`, which carry
  `schema_ok` / `schema_warnings` too, so the shape is lossless for both files.
- **The deploy runbook** (docs/DEPLOY.md, "Before recreating the service") says what this means
  for a redeploy: a running scheduled *apply* delays it up to the drain window, exactly as a UI
  apply does; a scheduled run still *planning* is cancelled and re-fired on its own, so it is
  nothing to wait for.
- **The quota guard.** A *scheduled* run whose Spotify read fails with
  `QUOTA_EXCEEDED` (`ports.QuotaExceeded`, the same detection `doctor` uses, #61) exits 0 and publishes
  `RunStatus.SKIPPED` with the message `"Spotify quota exceeded"`, caught in `run_command` before
  the generic `SourceError` handler. Not `interrupted`, so neither the shutdown path nor
  `_after_scheduled` ever marks it cancelled: the job settles `JobState.DONE` (exit 0), the
  scheduler's own fire record stands as serviced, and the *next regular slot* tries again rather
  than a catch-up burning more of a quota that is already gone - a retry loop that can exhaust a
  whole day's quota. A hand run is `error`: a person asked for this, and `error` is the honest
  answer.

The playlist picker lists
playlists by name from `likearr playlists --json`, fetched only when asked ("Refresh names
from Spotify"): opening the page starts nothing. It follows a fetch already running, and
reuses an answer - a failed one too - for 10 minutes rather than starting another child. Every
successful fetch is merged into `<config dir>/ui/playlist-names.json` (id to name, newest name
wins, never expired), so a playlist is shown by name everywhere - the picker, Status's source
counts, Look up - from then on, with "names as of" the last refresh. A failed refresh leaves the
names as they were. **The names load by themselves when they are needed** - the file is missing,
or a playlist the config names is not in it: once at server start, after every check for changes,
and after a settings save (from the POST, never a GET). Once loaded they persist; refreshing names
already known is the button's job. A scheduled `run` makes no extra Spotify call for them. Until a
name is known it reads "a playlist (loading names…)" on Review changes, linked to it on
open.spotify.com, and "one of your playlists" in Look up and Not added - never a raw id. The
picker lists every `/me/playlists` entry, not owned ones included (#103): a readable
playlist - owned, or one you collaborate on once the token has `playlist-read-collaborative`
(`PlaylistEntry.readable`) - is an ordinary checkbox, and one likearr cannot read -
followed, someone else's, or one of Spotify's own algorithmic or editorial playlists - is greyed
out and disabled, so it can never be *added* by clicking it. A collaborative playlist listed by a
token granted without that scope (`PlaylistEntry.needs_reauth`) is greyed out
too, but says to re-authorize rather than to copy it, and the save refuses it with the same
words. One already in
`[spotify].playlists` is greyed out too, but stays checked and rides a hidden input rather than
the disabled checkbox: likearr never changes what you set without saying so, so an unrelated save
leaves it configured, with its own note (kept, but unreadable) and a separate, always-enabled
"Remove from settings" checkbox as the one way to take it out on purpose. The settings POST
mirrors this: it refuses a *newly added* not-owned id, but a save that posts one already
configured back unchanged - the hidden input's job - goes through, and "Remove from settings"
strips an id from the posted selection before either check runs. A selected id the last listing
does not know at all (deleted, or never seen) keeps a plain warning instead, since there
is nothing to grey out. The selection travels with the picker, so what the form holds - not the
file - is what every swap renders, and while a fetch is in flight the selection is listed rather
than offered, so nothing clicked can be lost to the next swap.

**Look up** (`/explain`) answers at once, in the server, from what the last run recorded
(`likearr explain --from-last-run`'s path: no job, no request to anyone), labelled "as of the last
run" with its time and whether it applied. Each match is one card: the plain summary sentence,
a status pill (Downloaded, Waiting for download, Monitored, Not monitored yet, Not in Lidarr yet,
Skipped (name collision), Couldn't match, Left out by your settings, and a few rarer ones), an
amber "Looks like a wrong match" pill beside it when the match is doubtful, three or four labelled
facts (On Spotify, Matched to, In Lidarr, and for a followed artist What likearr wants), small
buttons (MusicBrainz, Open in Lidarr when `[ui] lidarr_url` resolves, "Not this one" on a matched
release, which goes through the same settings confirm as the plan review's), and everything else
(the ids, the resolver's step, what it looked at) in the card's own collapsed "Details". No bare
MBID is in the visible text. "Check against live Spotify and Lidarr" starts
`likearr explain --json -- <query>` as a job for the slow, current answer, rendered as the same
cards, without "Not this one" (the refusal is checked against the last run). The collision card's
Look up button asks the fast way.

**The last run's facts.** Look up's inputs are mostly what a run already computed, but the state
database keeps only Spotify ids and resolved matches. So every `likearr run` that plans ends by
writing `last-run.json` beside the state database (`shell/last_run.py`): the Spotify intents with
their names and years, every resolution (unmatched ones too), the desired state, the slice of
Lidarr's view the desired and owned releases touch, the name collisions, and the plan's unmonitors
and guards - so a release nothing wants any more is said to be unmonitored only when the plan does
it, and a guard that held it back is named. A stale refusal is recorded as one, never as a dry
run. The web server keeps one parsed copy, read again only when the file changes. After an apply, the
view carries what it changed, less what Lidarr refused, so "as of the last run" means after it.
Ownership is read from the state database as it stands, and disambiguations from the MusicBrainz
cache alone. The write comes last and is best-effort: a failure is a warning in the log and
changes nothing else - not the exit code, the health record or the state database. A few MB,
replaced each run; answering from it takes a fraction of a second.

**Plan** (#30) starts a dry run from the browser: `likearr run --out <its job
dir>/diff.json`, with the `--accept-shrink` checkbox beside the shrink guards that fired at the last
plan. It is a run job, so the run lock is checked before it starts. The page shows the next
scheduled run and warns when the dry run - timed by the last one, or 15 minutes - may still be
running then, since the scheduled run would skip itself and Home Assistant's retained record would
show that skip until the next apply. When a check finishes, its own page shows the review inline -
the same content as the review page, with "Apply these changes" at the end and the CLI's raw output
under a collapsed "Technical log"; a check with nothing to change says so and offers only a quiet
"Apply anyway". The review page (`/plan/<job>`) reads the plan's `diff.json` from
its job directory. It shows a summary, then every section of the diff in plain language: adds,
monitors, unmonitors, profile widenings, re-monitored artists, artists whose "Monitor New Albums"
goes to None ("Artists to stop auto-monitoring", #172), refreshed artists, reason-only updates,
guards, name collisions, pending singles and what could not be matched. A widening whose artist is
also on that list says in its row that the release types it shows are not monitored. Each section
is filtered and paged (50 rows) by htmx. Artists are named from the resolver's cached resolutions
(read-only), so no Lidarr call is made to label a row.

A plan's **lifecycle** is computed when it is read, not stored. A plan is *reviewable* until an
apply that changed something has landed since (a scheduled run's included; its health record's
monitored, unmonitored, added or new_items_none count is above zero), `[rules]`/`[guards]` no
longer match what the plan recorded, or it was made by an older resolver. After any of these it is
*superseded*: the same comparisons `apply` makes, so a settings save or a hand edit supersedes it
alike. A scheduled apply with nothing to do leaves Lidarr as the plan saw it and does not; ratchets and
re-monitored artists are not in the health record, so an apply that did only those leaves the plan
reviewable, and `apply`'s digest check remains the backstop. A plan left for a week is *expired*. A superseded or
expired check offers "Check again", carrying its own `--accept-shrink` choice as a ticked box. `/plan`
reads only the health records for this, plus the newest plan's guards and shrink choice
(`SqliteState.last_plan`): no stored diff is parsed. A reason-only update row names its release
from the cached resolutions, or links its MBID to MusicBrainz.

**Why a release is no longer needed.** An unmonitor row says what happened to each reason it lost,
not just which reason it was: the song or album now matches another release in this plan ("your
saved album now matches Tease Me (album, 1992) instead", in plain text), matches one already
wanted, can no longer be matched (see Not added), or is gone from Spotify ("you no longer have this
album saved"). It reads that from the plan itself (its monitors, reason updates and unmatched) and
from the last run's facts. Every monitor and unmonitor row shows the release's type and year, so
two same-titled releases are told apart, and the summary counts the unmonitors that were replaced
rather than dropped.

**What stays on disk.** Each unmonitor row has an "On disk" column: "12 files on disk (stay
where they are)", "no files", or "file count unavailable" when Lidarr could not be asked. The
counts come from `likearr lidarr-files --plan <diff.json> --out <its job dir>/files.json`, a
read-only child job (`GET /artist`, then `GET /album?artistId=` for the unmonitored releases'
artists) that the server starts when a check started from the browser finishes with something
to unmonitor, recorded against that plan (`plan_id`) and kept as long as it. The server never
asks Lidarr itself, and a scheduled `run` never starts it. While it counts, the rows say
"counting files..." and refresh themselves every two seconds until the answer is in. Playlist
names, when needed, are fetched after the count, since one job runs at a time.

**Applying** a reviewable plan goes through a confirm page (`/plan/<job>/apply`) that restates what
it will do, the guards that hold, and the exact command. The apply job runs exactly
`likearr run --apply <the plan's own diff.json>`, plus `--accept-health` when its box is ticked,
and nothing else: the argument list is fixed in the server, the form's other fields are ignored,
and there is no path to `--force` or `--scheduled`. It takes the run lock (a scheduled run in
progress is "try again"), and it is a draining job: cancel is refused (409, in the route and in
`JobRunner.cancel`), shutdown waits 28 minutes for it (`STOP_GRACE_PERIOD_S`, 30, less 2 for the
server itself, matching the compose `stop_grace_period`), and it runs in its own session, so a
Ctrl-C at a terminal running `start` by hand cannot reach it. It records the plan it applies
(`JobMeta.plan_id`). The job store never prunes that plan, the running job, or any plan younger
than `EXPIRE_AFTER` (which covers every reviewable one), and it prunes only after the new child has
started. The browser carries only the job id and the plan's token: `sha256(job id | sha256 of
diff.json's bytes | source_digest | lidarr_digest | resolver_version | accept_shrink |
config_fingerprint)`, recorded when the plan job finishes (a finish hook) and compared, at the
POST, with the token of the file on disk now, so a diff replaced or edited since review - a monitor
cut, a guard deleted, an artist added - is refused. That is an integrity check; the safety check stays `apply`'s own, which re-plans and exits
stale (3) if Spotify, Lidarr or the settings moved. A stale apply is shown as a message - what
moved, from the run's health record - with "Check again", which keeps the plan's shrink choice. It
is published like any stale apply, so the page says Home Assistant shows "stale" until the next
scheduled run.

**A job that outlives the server (#30).** In the container `start` is PID 1 and every child dies
with the container, so a job left `running` is interrupted. Run by hand, a child can outlive the
server: an apply runs in its own session (`start_new_session`), so the Ctrl-C that stops `start`
never reaches it, and a server killed outright (`kill`, systemd with `KillMode=process`) leaves
any child behind, since the others share its process group but not its fate. So every job records
its child's PID and the kernel's start time for it (`/proc/<pid>/stat`, field 22) as it starts, and
at startup a job still `running` whose process is alive, started at that same moment and runs the
same argv (`/proc/<pid>/cmdline`) - never a reused PID - is re-adopted rather than marked
interrupted: the job page says "Still running from before likearr restarted" and offers no Cancel
(it is not the server's child), no other job starts meanwhile, and when the process goes its
outcome is read from the health record it printed (`exit_code`), or it is recorded interrupted if
it printed none. A server shutting down stops watching and leaves it `running` for the next start
to look at again. Without `/proc` (macOS) nothing can be proved and such a job is marked
interrupted as before. Stopping a job signals the child's own PID, which is enough: a `likearr`
child starts no processes of its own. An adopted apply still cannot overlap a second one: it
holds the run lock, and the server takes the lock before starting one.
**"Not this one"** on a monitor row adds that release group to `[rules] deny_releases` through the
settings confirm (the same validated, backed-up write, and its second confirm); only a release the
plan monitors is accepted, and only one every reason of which a deny can stop: a liked or playlist
song, or a followed artist's catalogue (#153). A row a saved album wants, or one kept by hand, has
no button; it says where the choice lives instead ("Unsave the album on Spotify to stop this."), and
a post for it anyway is refused with the same words. Look up cards and the collision card follow
the same rule. It never re-plans by itself: after the save the browser goes to /plan,
where the old plan reads as superseded.

**Clean up is optional and off by default** (#148). `[prune] enabled` (default `false`, checked
but never fatal like the rest of `[prune]`) turns it on; Settings sets it from a collapsed
"Advanced" section at the bottom of the page, through the same backed-up write as every other
save, with a paragraph on what Clean up needs. While it is off the nav has no Clean up link,
`GET /prune` says what Clean up does and how to turn it on, every other `/prune/...` route answers
that page with a 404, and a finished "Find unneeded albums" job opens its plain job page.
promote-save follows the same switch: Settings offers its write-access box only while Clean up is
on, and ignores a posted one while it is off (a token that already has write access keeps it on a
re-auth). The CLI's `prune-report`, `prune-stage`, `prune-checks` and `promote-save` run either
way, with one warning line when it is off. Nothing is deleted or rewritten by the switch: the
ledger and old prune jobs stay as they are.

**Clean up** (`/prune`, #31; the page is "Clean up your library"). "Find unneeded albums" runs
`likearr prune-report --out <job dir>/prune.json` as a job - it reads what a check reads and changes
nothing. When it finishes, the job page takes the browser straight to the review (an `HX-Redirect`
from the poll, a 303 for a later visit), with the CLI's output under "Technical log"; a search that
failed stays on its job page. The review page lists the report per artist, largest first, filterable by artist or album
and by decision, 25 artists a page.

**The choices say what they ask of Spotify** (#55). Keeping with no change,
saving an album and following an artist are three different things, and each choice names its
own:

- per artist: Undecided / **Keep - no change on Spotify** / **Keep and follow the artist on
  Spotify** (`promote`; not offered when the artist is followed already) / **Keep and save all
  their albums on Spotify** (`save`) / **Trash all listed albums** (`trash`);
- per album: **Same as artist: ...** spelled out for what it means right now ("Same as artist: not
  decided yet", "Same as artist: keep", "Same as artist: trash", "Same as artist: keep and save on
  Spotify") / **Keep - no change on Spotify** / **Keep and save this album on Spotify** (exported
  in `save_releases`) / **Trash**.

**The model:** every album defaults to "Same as artist", and a choice made on an
album always beats the artist's. An album that defers to an undecided artist is simply not
actioned yet. An artist's choice acts only on the albums listed in Clean up - the page says so -
and never on anything else the artist has. A carried-over album's earlier decision is information
only ("You kept this on 15 Jan 2026."), never a choice.

**The net effect** of an artist's choice is spelled out under it once one is picked (re-rendered
with the card), with real counts and only the lines that apply: a trashed followed artist - "Trashes
the 2 albums listed here (1 compilation and 1 live album). You follow Queen on Spotify, so their
studio albums and EPs aren't listed and stay."; a kept followed artist - following brings in studio
albums and EPs only, so likearr won't fetch more like these; protected albums ("1 album holds the
only copy of a song you liked and is always kept.", or "a song in your playlists" when that is where
it came from); albums with their own choice ("N albums you
set individually keep their own choice."); and under "save all their albums", the albums set to
Keep that aren't saved. When the report cannot say whether the artist is followed, no line about
following is shown (`web/prune.py::net_effect`).

An album's own "Keep - no change on Spotify" also takes it out of its artist's "save all their
albums" (exported in `save_exclude_releases`, and `promote-save` honours it). One line on the page
says what the two Spotify actions mean: following an artist makes likearr monitor their studio
albums and EPs from now on, but saves nothing; saving puts the album in your Spotify library.
"Trash" is the page's word for what `prune-stage` does, and the page says it next to the choices
and the export: trash goes to the holding folder, and nothing is deleted until you empty it
yourself. A protected album, the only local copy of a liked song, is always kept; asking to trash
it is refused, and it can still be saved, because saving moves no file.

**Always kept, in words** (#64). A protected album's row has an "Always kept" pill
and says why in a callout of the same tone, not the grey note the history and reason lines use:
"Only copy of "Think", a song in your playlist "Road trip". Its album, Respect, isn't downloaded
yet." (or "a song you liked"; or "likearr is waiting for its album to be released." for a song
still waiting). No id appears in it: the playlist is named from the playlist-name cache ("one of
your playlists" when it is not there), the song from the report or else the last run's snapshot
("a song" when neither has it), and the album's title links to MusicBrainz. The artist's card
counts "N always kept". `web/prune.py::kept_why` writes the line from the row's `protection`; a
report from before that field is read back from its `protected_reason` line (the intent key, the
kind and the album's title, `legacy_protection`), and a line that will not read gets "Only copy of
a song from your liked songs or playlists." What is protected, trashed or exported does not change.

Every choice is saved as it is made, to `prune-draft.json` beside the report (only the report's
own ids and the fixed choices are accepted), and such a job is kept 30 days past the job store's
newest twenty. **Export** writes the two files the terminal steps take, beside the report: the
decisions file ("Prune decisions file" above: every album to trash is spelled out in `trash`, and
`trash_artists` stays empty) and the review snapshot `promote-save --reviewed` requires
(`review-data.json`: every artist and album looked at, its file count at review time, what was
decided, and whether it is to be saved). Both download too. It exports and never executes: moving
files and every Spotify write stay commands a person runs.

**Finish the clean up** (#58; `likearr/web/cleanup.py`). After an export the page walks the
reviewer through carrying it out, for any standard install. It first previews what
it can, as read-only child jobs chained from the export (never in the server process): the move
(`prune-stage --no-mount-check --out`, no `--apply`), the Spotify changes when the export asks for
any (the `promote-save` plan: Spotify reads only), and Lidarr (`prune-checks`). Then a numbered
checklist, each step saying what it does and whether it changes anything:

1. the move: files, size and albums against the Clean up cards, and the artists Lidarr removes (the
   row only) or rescans, with a reason each. The mount is not checked here, and the page says so;
2. Lidarr: import lists with automatic add (linked to Lidarr's import-list settings through
   `[ui] lidarr_url`), and whether the command queue is idle; "Check again" re-runs it;
3. the terminal preview, which checks the mount;
4. `prune-stage --apply`, once the queue is idle;
5. the Spotify changes: follows, saves, what could not be matched, what was not in the review;
   and, when the token file lacks `user-follow-modify` / `user-library-modify` (read from the
   token file like the re-auth date: scope names only, no refresh), the `auth --manual` command;
6. `promote-save --apply` of the plan step 5 made, and a fresh plan to apply if that one is stale;
7. afterwards: where the holding folder is, and that nothing is deleted until the reviewer empties
   it. No delete command is ever shown.

The commands paste and run as they are: `[ui] cli_command` in front (default `docker compose run
--rm likearr-cli`), `-c` the config the server was started with, `--holding` from `[prune]
holding_dir` (default `_likearr-holding` beside the root folder, never inside it), and the files by
their paths in the job store, which the documented install's one-shot container sees at the same
`/data` paths. Every path is shell-quoted. No command carries `--force`. Both keys are checked when
the config loads and never stop a run: a bad `cli_command` (not one line, or carrying a Unicode
control or format character) is a `[ui]` problem, a bad `holding_dir` (relative, with `..`, or
inside the root folder) falls back to the default and the checklist shows why. The page says the
prefix is `[ui] cli_command`, and that it must run the image with the library mounted and pass
the command through unchanged.

A preview is **bound to the export** it previewed: `previews.json` beside the report records the
sha256 of `decisions.json` and the three preview jobs. After any of them finishes, the next step
the export still lacks starts - only for a job the binding names, and only while the export is
the one the chain began with - so "Check again" between two steps delays the chain, never ends it. The page shows a preview only for the
current export; any change after it removes the exported files and the checklist with them, and a
new export starts a new chain. A preview that failed says so and links its job's output. Exporting
starts the previews only in `likearr start` (`auto_preview_prune`); tests start them where they
test them, so a job they start next never finds the slot taken.

**Why each album is there** (#55). Every candidate row says why nothing asks for it. A followed
artist brings their studio albums and EPs and nothing else, so a followed artist's compilation,
live album, single or remix says so: "Compilation: following Queen brings studio albums and EPs
only". An artist nobody follows says that no liked song, saved album or playlist track is matched
to the release. This needs `prune-report` to know who is followed, so each row carries
`artist_followed`, taken from the follow set `run` builds (`DesiredState.followed_artists`: a
Spotify follow resolved to that MusicBrainz artist) - the same read, no new Spotify call. When
likearr cannot know, it does not guess: with `[spotify] followed_artists = false` the field is null
and the row says nothing about following (and "follow" is still offered); when a Spotify follow of
that name never matched a MusicBrainz artist (`follow_unmatched`), the row says exactly that. A
followed artist gets a "Followed on Spotify" tag and no "follow" choice (`decide` refuses one); a
follow chosen earlier in the same review for an artist followed now shows and exports as a keep. A
report from before the field renders as before: no reasons, every choice shown.

**Decisions carried over** (#55). A report is rebuilt from scratch each time and on its own
remembers nothing: a report can list over a thousand albums, most of them kept in an earlier
review. So every export also records what it decided in a **ledger**,
`<config dir>/ui/prune-ledger.json` (`likearr/prune_ledger.py`): per release group `keep` (kept, no
change on Spotify), `save` (kept and saved, on its own or with its artist) or `trash`, and per
artist only the Spotify intents `promote` / `save`, each with the day and where it came from (a
Clean up export signs `Clean up <job id>`). An album left undecided records nothing; a protected
album records only a choice made about it ("always kept" is the report's rule, not a decision);
recording the same decision again keeps its first date and source; an artist decided otherwise
loses an earlier follow or save. An artist-level keep or trash is not recorded, because it says
nothing about that artist's next new album.

A review's draft is **pre-filled** from the ledger (`prefill`) on every read, from the entries it
has not seen yet and never from its own export. So nothing is used up: a review opened before
another review's export picks that export up on its next read, and an entry is applied once, so a
choice changed since stays changed. The rules:

- an album kept before is marked **carried over**, and says so under its title: "You kept this on
  15 Jan 2026." (or "You kept this and saved it on Spotify on ..."). That is information, not a
  choice: the album stays "Same as artist" - "not decided yet", with nothing happening to it, while
  the artist is undecided, and going with the artist once the artist has a decision (a later "save
  all their albums" saves it, "Trash all listed albums" trashes it, and its control says so). It
  becomes a hand choice only when its own control is changed, and choosing Keep is then a real
  change; only a hand "Keep - no change on Spotify" takes an album out of its artist's save. So the
  result is the same whether the ledger arrives all at once or across several reads;
- an album **trashed** before and on disk again is never pre-filled as a trash. It stays
  undecided with a note - "You trashed this on 15 Jan 2026 - it's on disk again (often a second copy
  in a differently spelled folder)" - because it is usually a spare the first move missed, and a
  pre-filled trash would move it on an export nobody looked at;
- **a past follow or save is never pre-filled as one**, for an artist or an album: it becomes a
  keep with a note ("Earlier, on 15 Jan 2026: followed on Spotify. That is not asked of Spotify
  again unless you choose it here."). See `promote-save` above for why;
- an artist whose every candidate was kept before gets an artist decision as well: keep. An artist
  with anything new stays undecided, so a new album is decided by a person, not by an old answer
  about other albums;
- this review's own choices win. An artist already decided in it gets nothing filled in, not even
  per album - "Trash all listed albums" or a follow made here is never narrowed by old keeps; an artist set
  back to Undecided by hand is never given a pre-filled decision again in that report; and an
  album already decided keeps its choice. Every artist the pre-fill changes gets a new revision (a tab
  opened before cannot write over it), and exported files are removed, and the page says export
  again, only when the pre-fill actually changed a choice.

A ledger that will not read (broken JSON, a newer version, the wrong shape) is shown on the page
as a warning, nothing is pre-filled from it, and it is **never written over**: an export still
writes its two files but says its decisions were not remembered. A missing ledger is simply
empty. Every export takes a file lock beside the ledger (`prune-ledger.json.lock`, a bounded
wait) in a worker thread, so a wait for another writer never stalls the server. Under the lock it
reads the report's draft again and records nothing if the draft has changed since that export:
two exports finishing out of order can never leave the older answer in the ledger.

The list opens on **Needs a decision**: the artists with at least one candidate album not kept in
an earlier review (new, or back after a trash). It changes only when the ledger brings something
new, never as albums are decided, so paging through it does not shift. The summary says how many
albums were carried over and how many need a decision. The ledger only ever pre-fills a draft:
`prune-stage` and `promote-save` never read it, so nothing in it moves a file or reaches Spotify.
A ledger may also hold an `imports` list, which nothing writes or uses: it reads, and an export
keeps it as it is.

**Name collisions** get a card each on Status, from the newest run that planned (so one fixed
since disappears). It names both artists with their MusicBrainz disambiguations, the Lidarr id of
the one Lidarr holds and how many releases the skip left unmonitored. It links both MusicBrainz
pages, the existing artist in Lidarr (`/artist/<mbid>`: Lidarr's SPA keys the artist page by the
MusicBrainz id) and Lidarr's add search opened on the wanted artist (`/add/search?term=lidarr:<mbid>`,
an id lookup). All of these are routes read from Lidarr's own source at v3.1.0.4875, not guessed.
Links go to `[ui] lidarr_url`, the browser-facing address, which defaults to `LIKEARR_LIDARR_URL`. Either
must be http(s) with a host name and no query or fragment; a fallback that is not gives no Lidarr
links, and a value written in `[ui]` that is not is a recorded problem. An
Look up button comes first, because a collision is often likearr picking the wrong artist (#32),
and Look up is how you see that. The card says what the fix is, and what it is not: Lidarr takes an
artist's name from MusicBrainz (`Artist.ApplyChanges` never copies it) and finds artists by name
on import (`ArtistRepository.FindByName` throws on two), so "add it under a distinct name" is not
possible. The honest options are to keep one, or to add both and import the second one's
downloads by hand with Manual Import, which may work.

The card acts on it too (#54), all through flows that already exist:

- **Not this one**, for each release the last run wanted of the skipped artist: the Look up deny
  flow (`/explain/deny`, which checks the last run wanted it, then the settings confirm). A wrong
  match refused is the usual fix: once nothing of theirs is wanted, the skip goes. A release a saved
  album wants is not listed, since refusing it would change nothing (#153).
- **Check again** starts a check (`POST /plan`). The card is drawn from the newest run that
  planned, so it goes as soon as a check no longer reports the collision.
- **Accept as known** opens the apply confirm of the newest check that is still reviewable and
  reports this collision, with "stop flagging these as new problems" (`--accept-health`) ticked
  and the reason spelled out: the skip becomes part of the health baseline and stops lighting Home
  Assistant amber. The page also says what the box really does: it accepts everything the apply's
  own re-plan reports - any other collision, skipped artist or oversized catalogue, including ones
  that appeared after the check. It is still one more click to apply, and the box can be unticked. With no such
  check, the card says to check again first. Never `--force`.

`--force`, `adopt`, `auth`, `prune-stage --apply`, `promote-save --apply` and every Spotify
write stay terminal-only.

## Upstream quirks the code assumes

### Resolving a followed artist

A followed Spotify artist becomes a MusicBrainz artist through **MusicBrainz's own Spotify URL
relationship**, not a name search:

```
GET /ws/2/url?resource=https://open.spotify.com/artist/<spotify_id>&inc=artist-rels
```
then `relations[].artist`. An editor asserted that this Spotify page *is* that artist, which is
an identity claim; a name search is a guess, and it cannot tell two artists apart who share a
name.

**A name search can guess wrong, and the cost is invisible.** Take a followed "Lawrence", the
New York sibling band: a name search can resolve it to `819a9744`, a German DJ, and want dozens of
that DJ's releases. A followed "Evangeline", the L.A. singer-songwriter, can resolve to a Seattle
alt-country band. The artists the user follows are then *starved* - the right Evangeline can sit
in Lidarr with nothing monitored and no files - while likearr tracks strangers. A wrong guess
also surfaces as a name collision (next section) when the right artist is already in Lidarr.

**Liked and playlist tracks do not take this path.** They resolve through an ISRC to a
*recording*, which is an identifier, not a name.

The URL lookup answers what it answers, so:

- **One linked artist** → resolved, step `artist:spotify-url`.
- **Several** (MusicBrainz carries bad links too: the "Lawrence" page also points at an unrelated
  eurobeat artist, while the German DJ is not linked at all, which is exactly how this method
  excludes it) → prefer the one already in the Lidarr library, the artist the user demonstrably
  has; step `artist:spotify-url:in-library`.
- **Several and still undecided** → **UNMAPPED**, step `artist:ambiguous-link`, naming every
  candidate with its MusicBrainz disambiguation, printed in its own run-summary section. A wrong
  artist pulls a whole catalogue into Lidarr, so not guessing is cheap by comparison.
- **No link at all** → fall back to the name search, which is conservative (exact
  normalised-name equality only). Several exact-name artists are namesakes the search score
  cannot tell apart, so the answer is **UNMAPPED** at step `artist:ambiguous-name`, naming every
  candidate. Taking the top-scored one would add a stranger and monitor their catalogue. Unlike
  several links, the library does not break this tie: one of them being in Lidarr may be a namesake
  added by hand, and preferring it would monitor that artist's whole catalogue.

One cached, rate-limited lookup per followed artist, on the same `mb_cache` path as everything
else. It arrived with `RESOLVER_VERSION` 3.

**Re-identification does not leave orphans.** When an artist's MBID changes, the `followed:<id>`
reason moves to the new artist's releases; `core.diff` sees that reason resolved *somewhere else*
this run and releases the old rows (`resolved_at` in the unmonitor loop), so the releases
recorded against a wrongly identified artist are unmonitored rather than lingering. The conservative half still
holds: a reason that resolved *nowhere* this run - an ambiguous link, a MusicBrainz outage - is
treated as a failure to decide, not a decision, and nothing is unmonitored. And a release that is
still desired this run is never unmonitored, whatever reasons it lost: its new reason set is an
`update_reasons` entry only (#69).

### Two artists, one name

Lidarr matches an incoming download to an artist **by name**. Give it two artists with the same
name and it declines to guess: `artistId` and `albumId` come back null with *"Unable to import
automatically, found multiple artists"*, and the queue item can never import. Cleanuparr cannot
rescue it either - with no album id there is nothing to re-search after a blocklist, so it logs
`skip | item is missing the content id` and correctly abstains. Neither tool misbehaves. Two
same-named artists simply cannot coexist in Lidarr.

Without a guard, likearr could create such a pair, and downloads would sit unimportable on every
protocol until someone noticed.

**They are usually not duplicates.** Two real pairs from MusicBrainz:

| Name | MBID | Who |
|---|---|---|
| Lawrence | `b6e422c0` | Clyde Lawrence and Gracie Lawrence, New York group |
| Lawrence | `819a9744` | Germany DJ & producer, a person |
| Evangeline | `6b57ce8f` | L.A. indie singer-songwriter, fka Eva B. Ross |
| Evangeline | `5be82d49` | 00s Seattle alt-country band |

Four distinct artists sharing two names. When Lidarr holds one of a pair and a user really does
want the other, likearr is **right** to want it; the constraint is Lidarr's, not a likearr policy and not an accusation of duplication. So the guard
stays, and it reports loudly rather than quietly:

- **The guard.** `core.diff.build_diff` skips an add whose normalised name already exists under a
  different MBID, records a `NameCollision` (both MBIDs, the existing Lidarr id, and how many
  releases went unmonitored as a result) plus a matching `name-collision` guard, and drops that
  artist's monitors too - nothing of theirs could ever be found in Lidarr. Re-adding the *same*
  MBID is not a collision; that is just an artist already present.
- **The report.** The run summary gives collisions their own section: per artist, the shared
  name, both MBIDs with **MusicBrainz's disambiguation strings** ("Germany DJ & producer" against
  "Clyde Lawrence and Gracie Lawrence"), the existing Lidarr id, and the release count the skip
  cost. That is what makes it a decision rather than a cryptic line. The disambiguations come
  from the existing cached, rate-limited `artist/<mbid>?inc=url-rels` fetch - `shell.run` fills
  them in after `build_diff`, keeping the core pure - and an artist with none degrades to the
  MBID alone. The same fields go into `diff.json` under `name_collisions`, so later tooling reads
  exactly what the console showed.
- **The audit.** `doctor` groups Lidarr's artists by normalised name and **FAILs** on any group
  larger than one, naming each artist and MBID. It is one API call the command already makes,
  and it catches this on the day.

A collision makes the run **degraded** (status and message in the health record, exit **0**)
rather than guarded. Exit 2 means "unmonitors were refused" and must keep meaning exactly that;
a skipped *add* loses nothing that was already there. `doctor` is the check that fails outright.

**It fires whatever the existing artist's file count is.** The ambiguity is in the name and
Lidarr's matcher never looks at files - an artist with zero files still stalls downloads.

**If you do want the skipped artist**, there is no renaming your way out: Lidarr takes an
artist's name from MusicBrainz (`Artist.ApplyChanges` keeps only the path, profiles, tags and
monitoring, so a refresh puts MusicBrainz's name back), and its importer finds artists by name
(`ArtistRepository.FindByName` throws `MultipleArtistsFoundException` on two). So adding the
second artist "under a distinct name" does not work, and neither the run summary nor the health
message suggests it (#32). The real options are to **keep
one**, or to **add both and import the second one's downloads by hand** with Lidarr's Manual
Import, which may work. And first check the collision is real at all: a collision is often
likearr having matched the wrong same-named artist, which `likearr explain <name>` shows - the
run summary and the guard's message both say so.

### Lidarr (3.1)

- `POST /command {"name": "RefreshArtist"}` without `isNewArtist: true` rescans **every root
  folder** when *Rescan after refresh* is `Always` (the default) and metadata changed; API pushes
  are always `trigger=manual`, so the *After manual refresh* setting does not suppress it either.
  likearr always sends `isNewArtist: true`, which limits the rescan to the artist's folder.
- `addOptions.monitor: "none"` on an artist add also unmonitors the **artist**; likearr re-monitors
  it with `PUT /artist/editor` right after the add. A duplicate add is a 400 whose body says
  "This artist has already been added"; likearr looks the existing artist up instead of failing.
- `PUT /album/monitor` does per-album work: 100 ids took over 60 s. Batches of 25 with a 300 s
  read timeout.
- Lidarr's metadata proxy (`api.lidarr.audio`) is a separate failure domain: a 503 there skips the
  artist for the run and never unmonitors from it. `album/lookup` accepts `lidarr:<mbid>`.
- Adding an artist makes Lidarr fetch it from that proxy. When the proxy is down, `POST /artist`
  answers 5xx; when the proxy does not know the MBID yet (an artist new to MusicBrainz), it is a
  400 whose body says "An artist with this ID was not found". Either way likearr skips that artist
  for the run and carries on with everyone else; the next plan asks for the add again. Only the
  5xx counts as a metadata failure: a not-found can last weeks, so it is named in the run's message
  and apply summary but does not degrade the run (#173). Any other 400 on an add is about what
  likearr sent (a root folder, a profile) and still stops the apply.
- Narrowing a metadata profile drops album rows without files and keeps rows with files.
- Deleting an artist while its `RefreshArtist` is in flight puts Lidarr into a loop that never
  ends; drain the command queue first. Queued commands cancel with `DELETE /api/v1/command/{id}`;
  a started one runs to completion.

### MusicBrainz

- `/isrc/<isrc>` rejects `inc=release-groups` (400); use a recording search `query=isrc:` and
  cached `release-group/<id>` lookups instead.
- Titles use U+2010 hyphens and other non-ASCII punctuation; the normaliser keeps punctuation as
  spaces rather than deleting it, or "Le Fabuleux Destin" stops matching itself.
- The Various Artists catalogue is millions of release groups. It is never browsed; a liked track
  on a compilation resolves through the track's own artist, and a browse is capped at 30 pages.
- `Retry-After: 0` on a 503 is a floor, not permission to retry instantly, and the rate limiter
  runs before every attempt including retries.
- **MusicBrainz data is edited, so a cached positive answer goes wrong.** A link corrected, an
  artist merged into another: after a merge the old id stops carrying the releases, and that
  artist's catalogue quietly stops being monitored. Positive cache entries therefore expire after
  `[musicbrainz] positive_cache_days` (90), each entry jittered deterministically by up to 25% of
  that from a hash of its key, so the cache does not fall due all at once - on a first run every
  entry is written on the same day, and expiring them together is hours of refetching at 1
  request/second and a plausible way to earn a ban. An artist's release-group browse keeps its own
  TTL of 0 (always refetched, because it is the question a followed artist exists to ask), and
  negative entries expire after `negative_cache_days`, jittered the same way (#166: most of an
  install's negative rows can be written in one run and would otherwise fall due together every
  week after).
  A refetch that fails still serves the stale entry - a failed lookup never drops a mapping - and
  is counted in `HealthRecord.mb_errors`, though not in `mb_ok`: nothing was lost, so alarming
  would be the always-on signal all over again.
- **A cached answer expires too (#165).** Resolutions sit above `mb_cache`: a reused track or
  saved-album answer makes no lookup at all, so the cache's expiry alone would never reach it and
  an editor's fix would wait for the next `RESOLVER_VERSION`. Each answer records when it was last worked
  out (`Resolution.checked_at`; the `resolutions.resolved_at` column is rewritten every run and
  cannot say), and is looked up again at 4/3 of `positive_cache_days` (120 days by default),
  jittered by intent key the same way, so an install cached in one run falls due over a month.
  Longer than `mb_cache` on purpose: re-resolved sooner, it could read the same unexpired entries
  and learn nothing. A row from before the field is reused and its clock starts on that run, so an
  upgrade re-resolves nothing. A due answer is still a valid one, so its re-check can only improve
  it: when the re-check fails or reaches only a provisional answer, the cached one is kept for that
  run and stays due, as `mb_cache` serves a stale entry; and a song settled on its single by the
  fallback window (whose clock was cleared when it settled) is re-checked with that window already
  run out, and kept on the single rather than sent back to waiting. An album that has appeared
  since still wins. Answers do not change here, only how long one is trusted.
- URL relationships come from `inc=url-rels` (available on every entity type). Spotify **artist**
  links sit on the artist; Spotify **album** links sit on a *release*, not on the release group -
  a release-group lookup returns Discogs, AllMusic and Genius but no streaming service. The
  linked release is the Digital Media one, which an unsorted `release?release-group=` browse
  rarely returns in its first page unless the page is large.
- Artist-artist relationships come from `inc=artist-rels`, which is a different payload from
  `inc=url-rels`: an `artist-urls:` cache entry holds none of them, so they get their own
  `artist-rels:` entry. A `member of band` relationship is person -> group, so on the group's
  page it reads `backward`; the resolver accepts either direction.

### Spotify Dev Mode (Feb 2026+)

- Playlist items are returned only for playlists the user owns or collaborates on, per Spotify's
  own reference. likearr requests `playlist-read-collaborative` (#103), so a
  collaborative playlist someone else owns is a source like an owned one. A token granted
  without it keeps working for everything else: a refresh sends no scope and keeps what was granted,
  nothing but the picker checks for the new scope (`can_read_collaborative`), and a scheduled run
  never reads a collaborative playlist the picker did not offer. Settings and Status note the
  re-auth; a hand-added one fails the run with the "not owned by you" message plus a re-auth hint.
  **Not yet verified against Spotify:** that a collaborator really gets items is still to be
  confirmed.
- `GET /playlists/{id}/items` (not `/tracks`); response field `items`, nested `items[].item`. A
  playlist the user does not own answers **200 with zero items** (detected by comparing against
  the playlist's own `tracks.total`/`items.total`) or, per the Get Playlist Items reference, a
  straight **403** when the user is neither owner nor collaborator - both map to the same "not
  owned by you" `SourceError`, never the generic "Spotify refused the request" (#103;
  `_not_owned_message` in `likearr/adapters/spotify.py`).
- **Writes go through `PUT /me/library?uris=`, which the public reference does not document.**
  The reference's `PUT /me/following?type=artist` and `PUT /me/albums` both answer **403** for
  a Development Mode app: the per-type library writes are deprecated and blocked outright in development mode.
  Measured against a Development Mode app, not inferred - `PUT /me/library?uris=spotify:album:<id>`
  and `…spotify:artist:<id>` each answered **200**, and re-reading `GET /me/albums` /
  `GET /me/following` confirmed the write really landed. One endpoint serves both, told apart by
  the `spotify:album:` / `spotify:artist:` prefix, so a follow and a save are one code path.
  The URIs go in the **query string**; a body-only call is 400 *"Missing required field: uris"*.
  `GET /me/library` is 405, so it is write-only and reads stay on the list endpoints.
  Batch **20** URIs per call: verified against Spotify, and the endpoint has no published maximum to read.

  Do not "fix" this back to the published per-type endpoints: the reference documents them, but
  the real API refuses them, and the measurement wins.
- **The two `/contains` endpoints are 403 on a Development Mode app** (verified on a
  token with all five scopes present). Membership comes from paging `GET /me/following`
  (`type=artist`, `limit` max **50**, cursor-paged: the page is nested under `artists`, and
  `artists.next` is an absolute URL carrying the `after` cursor) and `GET /me/albums` (`limit`
  max **50**, offset-paged, top level, with the album nested under `items[].album`).
- `GET /search` takes `limit` in the range **0-10** (default 5), and the `upc:` filter works only
  when searching albums.
- `external_ids` may disappear again: never the only mapping path.
- Quota is per developer account; honour `Retry-After`; treat `QUOTA_EXCEEDED` as a source error.
  It is small enough that a burst of several hundred `search` calls can exhaust it on a fresh app.
- Playlists are a source only for playlists the user owns or collaborates on (the latter with
  `playlist-read-collaborative`, #103). Followed and editorial playlists are invisible to an
  own-app token, so nothing in them protects a release from pruning.
- Redirect URIs must be a loopback IP literal (`http://127.0.0.1:PORT/...`), not `localhost`.
- Refresh tokens rotate, and Spotify reportedly tolerates one reuse of an already-used one before
  answering `invalid_grant`. Two processes that both load the same refresh token, both refresh,
  and both save would leave the second save's (already-used) token on disk - the next refresh is
  then permanently dead until `likearr auth --manual`. `SpotifyAuth` closes this with a lock
  (`<token_file>.lock`) around load-check-refresh-save: see "Safety".
