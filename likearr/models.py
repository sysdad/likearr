"""Core domain models.

Everything here is plain data: frozen dataclasses and enums, no I/O. The pure core
(likearr.core.*) operates only on these; adapters (likearr.adapters.*) produce and consume them.

Vocabulary
----------
- *Intent*: something a source says the user wants (a followed artist, a saved album, a liked track).
- *Resolution*: what the resolver made of an intent - a concrete MusicBrainz release group, a
  pending state (album not out yet), or unmapped (could not resolve).
- *Reason*: why a release is monitored. Reasons are a SET on each desired/owned release; a release
  stays monitored while any reason remains.
- *Desired state*: the full set of (artist, release group) → reasons the sources imply right now.
- *Owned*: releases this tool itself monitored (recorded in state). Only owned releases are ever
  unmonitored by this tool.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

RESOLVER_VERSION = 12
"""The version of the resolution rules. Every cached resolution records the version that made it.
A bump re-resolves every cached answer on the next run, so bump it whenever a change can alter
what a song, album or artist resolves to."""

LIKED_TRACK_SCOPE_ALBUM = "album"
"""`[rules] liked_track_scope`: a liked track resolves to the studio Album/EP holding the song."""

LIKED_TRACK_SCOPE_SMALLEST = "smallest"
"""`[rules] liked_track_scope`: a liked track resolves to the smallest official release holding it."""

LIKED_TRACK_SCOPES = frozenset({LIKED_TRACK_SCOPE_ALBUM, LIKED_TRACK_SCOPE_SMALLEST})
"""Every accepted value of `[rules] liked_track_scope`. Validated once, in `config.parse_config`."""


@dataclass(frozen=True, slots=True)
class ExclusionRules:
    """Releases a liked or playlist track may never resolve to.

    Every field defaults to the behaviour that shipped before them, so a deployment that changes
    no configuration changes no answer - see `token`.

    These are *opt-outs*, and they are the first rules in likearr that deliberately resolve
    **less** than the resolver could. A liked song whose only home is a 3-CD box set is a real
    song the user really liked; refusing it costs them the music, and the honest exchange is that
    it is reported (`core.resolver.EXCLUDED_STEP_PREFIX`) rather than dropped, and that saving the
    album on Spotify is always an escape hatch, because a saved album is monitored whatever its
    type.
    """

    allow_compilation_fallback: bool = True
    """When False, a track whose only home is a **Compilation**-typed release group is excluded.

    Compilation-typed only, never every non-studio type. The rule exists for box sets - *The
    Complete Dinah Washington on Mercury, Volume 2*, *The Complete Hot Five & Hot Seven
    Recordings, Volume 3* - and a live album such as Cannonball Adderley's *Mercy, Mercy, Mercy!
    Live at "The Club"* is the canonical record of that song rather than a collection of it.
    Widening this to `Live` or `Soundtrack` would cost music the user wants for no case anyone has
    measured.
    """
    allow_remix_releases: bool = True
    """When False, a remix release is never a candidate - unless the liked track is itself a remix.

    "Remix" means the MusicBrainz `Remix` secondary type **or**
    `core.normalize.has_remix_marker` on the title. The type alone is not enough: real examples
    are typed `EP` with no secondary types, so the type check that `is_studio` already
    performs never saw them.
    """
    keep_remix_only_tracks: bool = True
    """When `allow_remix_releases` is False, still monitor a remix release for a liked or playlist
    track when **every** release that could hold it is refused, each only for being a remix
    (``track:remix-only``). Read nowhere while remixes are allowed.

    The remix rule is there to stop a remix EP beating the real album, not to cost a song its only
    home: The Knocks' "Learn To Fly" exists on MusicBrainz only on *The Feeling (Remixes)*, an EP
    that carries the original recording. Default on, because a user who switched remixes off to
    stop the first never asked for the second. The deny list and the compilation rule are never
    overridden: a release refused for anything but being a remix is never kept by this.

    Named for tracks, not "saved" songs, because a *saved* album in likearr's vocabulary is
    `ReasonKind.SAVED`, which is monitored whatever its type and never reaches this rule.
    """
    deny_releases: frozenset[str] = frozenset()
    """Release group MBIDs that are never chosen, whatever the rules above say.

    Applied as a **candidate filter** rather than as a veto on the answer, so the resolver falls
    through to its next-best candidate instead of giving up. That is the whole point of it: some
    cases cannot be told apart by any rule, because MusicBrainz's data does not distinguish them.
    Cannonball Adderley's *Live in Concert* carries no secondary types at all, so it reads as a
    studio album and wins the title fallback for one liked copy of "Mercy, Mercy, Mercy" while
    four other copies resolve, correctly, to the `Live`-typed *Mercy, Mercy, Mercy! Live at "The
    Club"*. Denying the one release group is the only way to say so.
    """

    @property
    def token(self) -> str:
        """A stable short token for the three switches, or `""` when they are all at their default.

        Recorded on every track `Resolution` and carried in `Fingerprint`, so that flipping a
        switch re-resolves every like and re-baselines health, exactly as changing
        `liked_track_scope` already does. The **empty string for the defaults** is what makes
        deploying this code a genuine no-op: a resolution cached before these fields existed also
        carries `""`, so it still matches and nothing is recomputed until a switch actually moves.

        `deny_releases` is deliberately **not** in the token. Adding one MBID would otherwise
        re-resolve thousands of liked tracks to change a handful of them; instead a cached
        resolution is checked against the list directly (`core.resolver._reusable`), so only the
        intents that landed on a denied release are recomputed.

        `keep_remix_only_tracks` adds ``k0`` only when it is **off**, so the defaults
        are still `""` and a library running ``c1r0`` keeps ``c1r0``: keeping is the default, and
        the RESOLVER_VERSION 9 bump, not the token, is what re-resolves that library once. It moves
        the token whatever `allow_remix_releases` says, even though it changes no answer while
        remixes are allowed: one pointless re-resolve on an odd flip is cheaper than a Settings
        confirm that says "re-resolves" and sometimes does not.
        """
        if self.allow_compilation_fallback and self.allow_remix_releases and self.keep_remix_only_tracks:
            return ""
        keep = "" if self.keep_remix_only_tracks else "k0"
        return f"c{int(self.allow_compilation_fallback)}r{int(self.allow_remix_releases)}{keep}"


NO_EXCLUSIONS = ExclusionRules()
"""Every opt-out at its default, i.e. the behaviour that shipped before these rules existed.

A module-level singleton because it is the default argument of `core.resolver.resolve_track` and
`resolve_all`, and a frozen dataclass constructed in a signature is both a lint error and a new
object per call for no reason."""


class ReasonKind(enum.StrEnum):
    FOLLOWED = "followed"
    SAVED = "saved"
    LIKED = "liked"
    PLAYLIST = "playlist"
    MANUAL = "manual"  # kept by the user at adoption time; never removed by the tool


@dataclass(frozen=True, slots=True, order=True)
class Reason:
    kind: ReasonKind
    source_id: str
    """The Spotify ID (artist, album or track) that produced this reason."""
    playlist_id: str | None = None
    """Set only for ReasonKind.PLAYLIST."""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.playlist_id + ':' if self.playlist_id else ''}{self.source_id}"


class PrimaryType(enum.StrEnum):
    ALBUM = "Album"
    SINGLE = "Single"
    EP = "EP"
    BROADCAST = "Broadcast"
    OTHER = "Other"


class SecondaryType(enum.StrEnum):
    COMPILATION = "Compilation"
    SOUNDTRACK = "Soundtrack"
    SPOKENWORD = "Spokenword"
    INTERVIEW = "Interview"
    AUDIOBOOK = "Audiobook"
    AUDIO_DRAMA = "Audio drama"
    LIVE = "Live"
    REMIX = "Remix"
    DJ_MIX = "DJ-mix"
    MIXTAPE = "Mixtape/Street"
    DEMO = "Demo"
    FIELD_RECORDING = "Field recording"


@dataclass(frozen=True, slots=True)
class ReleaseGroup:
    """A MusicBrainz release group as the resolver sees it."""

    mbid: str
    title: str
    artist_mbid: str
    """Primary artist credit (first credited artist). Featured artists are ignored."""
    artist_name: str
    primary_type: PrimaryType | None
    secondary_types: frozenset[SecondaryType] = frozenset()
    first_release_date: date | None = None
    main_artist_mbids: tuple[str, ...] = field(default=(), compare=False)
    """The MBIDs of the release group's main credited artists, in credit order.

    Every credit except the featured guests: a credit joined by a featuring phrase ("feat.", "ft.",
    "featuring") and every credit after it are left out, so "Dirty Projectors feat. Dawn Richard"
    is just Dirty Projectors. `artist_mbid` is the first. Classical release groups credit the
    composer first ("Jean Sibelius; London Philharmonic Orchestra, Paavo Berglund"), so the first
    credit alone cannot tell two performances of a work apart. Empty when unknown: a release group
    from Lidarr's metadata, or one read back from the state database, which does not store it.
    Left out of equality, so a stored copy and a fresh one of the same release group still compare
    equal."""

    @property
    def is_studio(self) -> bool:
        return not self.secondary_types

    @property
    def is_studio_album_or_ep(self) -> bool:
        return self.is_studio and self.primary_type in (PrimaryType.ALBUM, PrimaryType.EP)

    @property
    def is_single(self) -> bool:
        return self.primary_type == PrimaryType.SINGLE

    @property
    def is_various_artists(self) -> bool:
        return self.artist_mbid == VARIOUS_ARTISTS_MBID


@dataclass(frozen=True, slots=True)
class BarcodeMatch:
    """A release group holding a release whose barcode is the one asked for.

    One per distinct release group. `official` is True when at least one of those releases is
    Official, which is the last tie-break when one barcode spans several release groups.
    """

    release_group: ReleaseGroup
    official: bool


@dataclass(frozen=True, slots=True)
class IsrcRecording:
    """One MusicBrainz recording carrying an ISRC, with the release groups it is on.

    MusicBrainz sometimes files one ISRC on two different songs by the same artist, so the
    recording's own title is kept with its release groups: the resolver drops a recording that is
    clearly a different song from the one liked. ``""`` when MusicBrainz gives no title.
    """

    title: str
    release_groups: tuple[ReleaseGroup, ...]


VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


# ---------------------------------------------------------------- intents (what a source says)


class SourceKind(enum.StrEnum):
    FOLLOWED_ARTISTS = "followed_artists"
    SAVED_ALBUMS = "saved_albums"
    LIKED_TRACKS = "liked_tracks"
    PLAYLIST = "playlist"


@dataclass(frozen=True, slots=True)
class ArtistIntent:
    spotify_id: str
    name: str
    reason: Reason


@dataclass(frozen=True, slots=True)
class SpotifyArtistRef:
    """A Spotify artist as a search result carries it. Only what matching and following need."""

    spotify_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SpotifyAlbumRef:
    spotify_id: str
    name: str
    artist_names: tuple[str, ...]
    upc: str | None
    album_type: str
    """Spotify's own type ('album' | 'single' | 'compilation'). Advisory only: Spotify files EPs as 'single'."""
    release_date: date | None


@dataclass(frozen=True, slots=True)
class AlbumIntent:
    album: SpotifyAlbumRef
    reason: Reason


@dataclass(frozen=True, slots=True)
class TrackIntent:
    spotify_id: str
    name: str
    isrc: str | None
    artist_names: tuple[str, ...]
    album: SpotifyAlbumRef
    added_at: datetime | None
    reason: Reason


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Everything read from the sources in one run. Immutable; all-or-nothing."""

    fetched_at: datetime
    artists: tuple[ArtistIntent, ...]
    albums: tuple[AlbumIntent, ...]
    tracks: tuple[TrackIntent, ...]
    counts: dict[str, int]
    """Per-source item counts keyed by SourceKind value (playlists keyed 'playlist:<id>')."""
    schema_ok: bool = True
    schema_warnings: tuple[str, ...] = ()

    def digest(self) -> str:
        """Stable hash of the snapshot's IDs, used to detect a stale diff."""
        import hashlib

        h = hashlib.sha256()
        for a in sorted(x.spotify_id for x in self.artists):
            h.update(b"a" + a.encode())
        for a in sorted(x.album.spotify_id for x in self.albums):
            h.update(b"b" + a.encode())
        for t in sorted(x.reason.key + "|" + x.spotify_id for x in self.tracks):
            h.update(b"t" + t.encode())
        return h.hexdigest()


# ---------------------------------------------------------------- resolutions (what the resolver made of it)


class ResolutionStatus(enum.StrEnum):
    RESOLVED = "resolved"
    PENDING_ALBUM = "pending_album"
    UNMAPPED = "unmapped"


@dataclass(frozen=True, slots=True)
class Resolution:
    intent_key: str
    """Reason.key of the intent this resolves (one resolution per reason)."""
    status: ResolutionStatus
    release_group: ReleaseGroup | None = None
    """Set when RESOLVED. For followed artists this is None; see ArtistResolution."""
    step: str = ""
    """Human-readable resolver step that produced this, e.g. 'album:upc', 'track:isrc->album'."""
    detail: str = ""
    """Extra context for `explain` and the unmapped report."""
    single_release_date: date | None = None
    """For PENDING_ALBUM: when the single came out; drives the fallback window."""
    single_release_group: ReleaseGroup | None = None
    """For PENDING_ALBUM: the single itself, monitored once the fallback window passes."""
    resolver_version: int = RESOLVER_VERSION
    source_release_group: ReleaseGroup | None = None
    """The release Spotify named for this intent, which is not always the release monitored.
    `prune` uses it to protect the only local copy of a liked track."""
    scope: str = "album"
    """The `liked_track_scope` this track resolution was made under. A cached resolution is reused
    only when the configured scope still matches, so flipping the setting re-resolves every like."""
    followed: bool | None = None
    """Whether this track's artist was followed when the `smallest` scope chose this release.

    The `smallest` dedupe rule reads the follow set: a song on a followed artist's studio Album/EP
    resolves to that release rather than to the single, because the follow monitors it anyway. That
    made the answer depend on something that changes, while the cache treated every resolution as
    permanent - so following an artist later never swapped their already-resolved single for the
    album. A cached `track:smallest:*` resolution is reused only while this still
    matches.

    **Three states, not two.** ``None`` means "not recorded", which is every row written before
    this field existed and every resolution made under the `album` scope, where the follow set is
    never consulted. It must not be read as ``False``: on a `smallest` library most cached
    resolutions are `covered-by-follow` for artists who *are* followed, and treating them as
    "was not followed" would re-resolve the whole library on the first run after the upgrade. A
    ``None`` whose step already agrees with today's follow state is back-filled in place instead
    (see `core.resolver._reusable`)."""
    rules: str = ""
    """`ExclusionRules.token` this track resolution was made under; `""` for the defaults.

    Compared in `core.resolver._reusable` exactly as `scope` is, so flipping `[rules]
    allow_compilation_fallback`, `allow_remix_releases` or `keep_remix_only_tracks` re-resolves
    every liked and playlist track on the next run. Because the defaults token is `""`, every row
    written before this field existed already agrees with a default configuration and nothing is
    recomputed on upgrade."""
    denied_skipped: frozenset[str] = frozenset()
    """Release group MBIDs on `[rules] deny_releases` that the resolver refused on the way to this
    track answer. Empty when it met none, and for every row written before the field.

    The deny list is a candidate filter, so a refused release is invisible in the answer: a song
    kept off denied X lands on Y, and Y alone cannot say X was ever in play. `core.resolver.
    _reusable` re-resolves the answer once any of these is no longer denied, which is what makes
    removing an entry move a song back. Adding an entry is checked against the answer the
    resolution chose, as before."""
    checked_at: datetime | None = None
    """When this answer was last worked out from lookups; ``None`` for a row written
    before the field existed. Reusing the answer does not move it, re-resolving it does.

    `core.resolver._due` looks the answer up again once it is older than the max age the shell
    passes (derived from `[musicbrainz] positive_cache_days`), so a MusicBrainz correction reaches
    an intent that already resolved. It lives on the answer, not on the `resolutions` row, because
    the row's `resolved_at` column is rewritten on every run, reused answers included. A ``None``
    is not read as "old": the clock starts on the next run instead, so an upgrade re-resolves
    nothing."""


@dataclass(frozen=True, slots=True)
class ArtistCandidate:
    """One MusicBrainz artist a Spotify artist page is linked to.

    The disambiguation is what tells two same-named artists apart, both when choosing between
    candidates and when reporting that the choice could not be made.
    """

    mbid: str
    name: str
    disambiguation: str = ""

    def describe(self) -> str:
        """``'Name' (mbid) - disambiguation``, for a report a human has to act on."""
        suffix = f" - {self.disambiguation}" if self.disambiguation else ""
        return f"{self.name!r} ({self.mbid}){suffix}"


@dataclass(frozen=True, slots=True)
class ArtistRelation:
    """One artist-artist relationship MusicBrainz records, seen from the artist it was asked about.

    The resolver reads two of MusicBrainz's relationship types - ``member of band`` and
    ``collaboration`` - as evidence that two differently-credited artists are one act for the
    purpose of filing a release. Every other type is carried as it comes and ignored
    there: a ``sibling`` or a ``tribute`` joins two names, not one act.
    """

    relationship: str
    """MusicBrainz's relationship type name, as it sends it, e.g. ``"member of band"``."""
    artist_mbid: str
    """The artist at the other end."""
    artist_name: str


@dataclass(frozen=True, slots=True)
class ArtistResolution:
    intent_key: str
    status: ResolutionStatus
    artist_mbid: str | None = None
    artist_name: str = ""
    step: str = ""
    detail: str = ""
    resolver_version: int = RESOLVER_VERSION


# ---------------------------------------------------------------- desired state


class Profile(enum.StrEnum):
    LEAN = "lean"  # Album + EP, studio only
    FULL = "full"  # + Single, Compilation, Soundtrack, Live (never Remix / DJ-mix)


@dataclass(frozen=True, slots=True)
class ReleaseKey:
    artist_mbid: str
    rg_mbid: str


@dataclass(slots=True)
class DesiredRelease:
    key: ReleaseKey
    release_group: ReleaseGroup
    reasons: set[Reason] = field(default_factory=set)
    steps: dict[str, str] = field(default_factory=dict)
    """reason.key → resolver step, for explain."""

    @property
    def needs_full_profile(self) -> bool:
        return not self.release_group.is_studio_album_or_ep


@dataclass(slots=True)
class DesiredState:
    releases: dict[ReleaseKey, DesiredRelease]
    artists: dict[str, str]
    """artist_mbid → artist name, for every artist that must exist in Lidarr."""
    followed_artists: set[str]
    """artist_mbids whose whole studio Album/EP catalogue is wanted."""
    pending: list[Resolution]
    unmapped: list[Resolution | ArtistResolution]
    profile_needs: dict[str, Profile]
    """artist_mbid → minimum profile required by the desired releases."""
    followed_counts: dict[str, int] = field(default_factory=dict)
    """artist_mbid → number of studio Album/EP release groups wanted from that followed artist."""
    catalogue_counts: dict[str, int] = field(default_factory=dict)
    """artist_mbid → the followed artist's studio Album/EP count *before* the user's own filters
    (`[rules] deny_releases` and the `albums-only` tag), for the per-artist shrink guard. That
    guard is for a catalogue MusicBrainz shrank; a release the user left out is not one."""


# ---------------------------------------------------------------- Lidarr view


@dataclass(frozen=True, slots=True)
class LidarrArtist:
    id: int
    mbid: str
    name: str
    monitored: bool
    monitor_new_items: str
    metadata_profile_id: int
    quality_profile_id: int
    tags: frozenset[int]
    path: str = ""


@dataclass(frozen=True, slots=True)
class LidarrAlbum:
    id: int
    rg_mbid: str
    artist_id: int
    artist_mbid: str
    title: str
    monitored: bool
    primary_type: PrimaryType | None
    secondary_types: frozenset[SecondaryType]
    release_date: date | None
    track_file_count: int = 0
    size_on_disk: int = 0

    @property
    def has_files(self) -> bool:
        return self.track_file_count > 0


@dataclass(slots=True)
class LidarrView:
    """A read of the parts of Lidarr the diff needs. Albums are loaded per artist, never in bulk."""

    artists: dict[str, LidarrArtist]
    """by artist mbid"""
    albums: dict[str, dict[str, LidarrAlbum]]
    """artist_mbid → rg_mbid → album; only artists that were loaded appear here."""
    metadata_profiles: dict[str, int]
    """name → id"""
    quality_profiles: dict[str, int]
    tags: dict[str, int]
    version: str = ""

    def album(self, key: ReleaseKey) -> LidarrAlbum | None:
        return self.albums.get(key.artist_mbid, {}).get(key.rg_mbid)

    def lacks_album(self, key: ReleaseKey) -> bool:
        """The artist is in Lidarr and its albums were read, and this release group is not among
        them. A missing artist, or one whose albums were not loaded, says nothing about the album."""
        albums = self.albums.get(key.artist_mbid)
        return albums is not None and key.artist_mbid in self.artists and key.rg_mbid not in albums


# ---------------------------------------------------------------- state (what likearr owns)


@dataclass(frozen=True, slots=True)
class OwnedRelease:
    key: ReleaseKey
    reasons: frozenset[Reason]
    step: str
    resolver_version: int
    monitored_at: datetime
    lidarr_album_id: int | None = None

    @property
    def is_manual(self) -> bool:
        return any(r.kind == ReasonKind.MANUAL for r in self.reasons)


@dataclass(frozen=True, slots=True)
class OwnedArtist:
    artist_mbid: str
    lidarr_artist_id: int | None
    added_by_us: bool
    profile: Profile
    ratcheted_at: datetime | None = None


# ---------------------------------------------------------------- diff (what apply would do)


@dataclass(frozen=True, slots=True)
class AddArtist:
    artist_mbid: str
    name: str
    profile: Profile


@dataclass(frozen=True, slots=True)
class MonitorRelease:
    key: ReleaseKey
    title: str
    reasons: frozenset[Reason]
    step: str


@dataclass(frozen=True, slots=True)
class UnmonitorRelease:
    key: ReleaseKey
    title: str
    lost_reasons: frozenset[Reason]


@dataclass(frozen=True, slots=True)
class ProfileRatchet:
    artist_mbid: str
    name: str
    to_profile: Profile
    because: str


@dataclass(frozen=True, slots=True)
class Guard:
    code: str
    message: str
    blocked_unmonitors: int = 0
    subject: str = ""
    """What the guard held: the `SourceSnapshot.counts` key for `source-shrink`, the artist MBID for
    `artist-shrink`. Its baseline is not advanced while the guard fires."""


@dataclass(frozen=True, slots=True)
class NameCollision:
    """An artist likearr wanted, skipped because Lidarr already holds that *name*, or because
    another artist of that name is new in the same run (see `in_lidarr`).

    Almost always a genuinely different artist: "Lawrence" is both a New York band (Clyde and
    Gracie Lawrence) and a German DJ; "Evangeline" is both an L.A. singer-songwriter and a Seattle
    alt-country band. likearr is usually right to want the second one. It is **Lidarr** that
    cannot hold two artists under one name - its importer matches on name, goes ambiguous, and
    leaves downloads with a null `artistId`/`albumId`, which is how imports get stranded.

    So this is a reported cost, not an error and not an accusation of duplication.
    `dropped_releases` says what the skip cost, and the disambiguations say which artist is which,
    so a human can decide whether they want that music enough to act on it.
    """

    name: str
    """The name both artists share, as the wanted one spells it."""
    wanted_mbid: str
    existing_mbid: str = ""
    existing_lidarr_id: int = 0
    existing_name: str = ""
    """How Lidarr spells it, which is not always how the wanted artist spells it."""
    wanted_disambiguation: str = ""
    """MusicBrainz's one-line disambiguation, e.g. "Germany DJ & producer". Often absent."""
    existing_disambiguation: str = ""
    dropped_releases: int = 0
    """Releases that would have been monitored for the wanted artist, and now are not."""

    @property
    def in_lidarr(self) -> bool:
        """Whether the other artist is one Lidarr holds. False when both artists were new in the
        same run: then `existing_*` names the other new one, which was skipped as well, and there
        is no Lidarr id to point at."""
        return self.existing_lidarr_id > 0


@dataclass(slots=True)
class Diff:
    created_at: datetime
    source_digest: str
    lidarr_digest: str
    """Hash of (album id, monitored) for every album the diff touches; stale if it changed."""
    add_artists: list[AddArtist]
    monitor: list[MonitorRelease]
    unmonitor: list[UnmonitorRelease]
    ratchets: list[ProfileRatchet]
    set_new_items_none: list[str]
    """Artist MBIDs whose monitorNewItems ("Monitor New Albums") must be set to 'none': only artists
    holding a release likearr owns (added, claimed or adopted), including one whose release it
    claims, whose profile it widens or which it re-monitors this run. A hand-managed artist
    whose wanted release is already monitored keeps its own setting."""
    guards: list[Guard]
    pending: list[Resolution]
    unmapped: list[Resolution | ArtistResolution]
    projected_wanted: int
    resolver_version: int = RESOLVER_VERSION
    update_reasons: list[tuple[ReleaseKey, frozenset[Reason]]] = field(default_factory=list)
    """Releases that stay monitored but whose reason set changed; state-only, no Lidarr call."""
    name_collisions: list[NameCollision] = field(default_factory=list)
    """Artists skipped because Lidarr already holds their name, with what each skip cost.

    The matching `name-collision` guards carry the same facts as prose; this carries them as
    data, so the run summary and any later tooling read the same thing."""
    accept_shrink: bool = False
    """The plan was made with ``--accept-shrink``: the source and artist shrink guards were not
    applied, because a human reviewed the shrink and accepts it. Recorded so the reviewed apply
    carries it, and refused for a scheduled run."""
    monitor_artists: list[str] = field(default_factory=list)
    """Artist MBIDs Lidarr holds unmonitored although a desired release lives under them.

    Lidarr never searches, and never lists as wanted, an album whose artist is unmonitored, so
    such a release is invisible however its own flag reads. Corrected every run, not once at add
    time, because Lidarr can apply `addOptions.monitor: none` to the artist *after* the POST."""
    refresh_artists: list[str] = field(default_factory=list)
    """Followed artists with a recent or future release Lidarr's catalogue does not hold yet.

    likearr sets `monitorNewItems=none` on the artists it holds releases of (`set_new_items_none`),
    so a followed artist's new album is monitored only once Lidarr's own scheduled refresh has
    picked it up - and if that never happens for the artist, the album is never monitored and
    nothing said so. This asks Lidarr for the metadata instead of waiting on it. Capped
    per run by `[lidarr] max_refreshes_per_run`, and in the diff rather than done silently, so a dry
    run shows it and a reviewed apply carries it."""
    config_fingerprint: Mapping[str, Mapping[str, object]] | None = None
    """`Config.plan_fingerprint` at planning time, so `--apply` can refuse a diff whose `[rules]` or
    `[guards]` changed since - a release denied after the plan was reviewed, say. ``None`` for a
    diff written before it was recorded, which `apply` treats as stale: it cannot vouch for the
    configuration it was planned under."""
    claim: list[OwnedRelease] = field(default_factory=list)
    """Wanted releases Lidarr already monitors that likearr does not own yet, to own from now on
    (`[rules] manage_monitored`). State only, no Lidarr call."""
    disown: list[ReleaseKey] = field(default_factory=list)
    """Owned, non-manual releases no longer wanted that Lidarr already shows unmonitored, or no
    longer has at all, to stop owning. State only, no Lidarr call."""

    @property
    def is_empty(self) -> bool:
        return not (
            self.add_artists
            or self.monitor
            or self.unmonitor
            or self.ratchets
            or self.set_new_items_none
            or self.monitor_artists
            or self.refresh_artists
        )

    @property
    def guarded(self) -> bool:
        return any(g.blocked_unmonitors for g in self.guards)


# ---------------------------------------------------------------- promote / save (Spotify writes)

SPOTIFY_COLLABORATIVE_SCOPE = "playlist-read-collaborative"
"""Lets ``GET /me/playlists`` list, and ``GET /playlists/{id}/items`` read, a playlist the user
collaborates on but does not own. A token granted before likearr asked for it
keeps working for everything else: it simply cannot read those playlists until re-authorized, so
the picker offers a collaborative playlist only once the stored token has this scope."""

SPOTIFY_READ_SCOPES = (
    "user-follow-read",
    "user-library-read",
    "playlist-read-private",
    SPOTIFY_COLLABORATIVE_SCOPE,
)
"""What every read path needs: follows, the saved library, and playlists the user owns or
collaborates on."""

SPOTIFY_WRITE_SCOPES = ("user-follow-modify", "user-library-modify")
"""What `promote-save` needs on top: PUT /me/library for both artists and albums (see
`SpotifyLibraryPort.follow_artists` / `.save_albums` in ports.py - not the per-type endpoints
Spotify's public reference documents for these scopes, which answer 403 for a Development Mode
app).

Exactly the scope strings Spotify's own reference names for those two endpoints. A sign-in asks
for them only on opt-in (`likearr auth --promote-save`) or when the token it replaces already
has them, so a token without them has to be re-authorized by hand with that flag.
"""

PROMOTE_SAVE_PLAN_VERSION = 2
"""Bump when the promote-save plan file's shape changes; `--apply` refuses an older one.

2: the save set is constrained to the albums a human actually reviewed, and the plan carries
that snapshot's digest. A version 1 plan was built from current library state and has to be
re-planned rather than applied.
"""


@dataclass(frozen=True, slots=True)
class FollowArtist:
    """One `promote` artist, matched to a Spotify artist that `--apply` would follow."""

    artist_mbid: str
    name: str
    spotify_id: str
    step: str
    """How the match was made, e.g. ``artist:name``."""


@dataclass(frozen=True, slots=True)
class SaveAlbum:
    """One kept album of a `save` artist, matched to a Spotify album `--apply` would save."""

    key: ReleaseKey
    artist_name: str
    title: str
    spotify_id: str
    step: str
    """How the match was made, e.g. ``album:upc`` or ``album:name``."""


@dataclass(frozen=True, slots=True)
class Unmatched:
    """Something a decision asked for that could not be matched *confidently*.

    Never a silent drop: every one of these is written into the plan and printed, because a wrong
    match writes the wrong record into the user's Spotify library, which is worse than a miss.
    """

    kind: str
    """``artist`` or ``album``."""
    artist_mbid: str
    rg_mbid: str
    """Empty for an artist."""
    name: str
    """``Artist`` or ``Artist - Title``, for the human reading the report."""
    reason: str


@dataclass(slots=True)
class PromoteSavePlan:
    """What `promote-save --apply` would do, and the world it was computed against.

    `decisions_digest`, `reviewed_digest` and `lidarr_digest` are this command's equivalent of a
    `Diff`'s `source_digest` / `lidarr_digest`: `--apply` recomputes all three and refuses
    (exit 3) a plan whose inputs have moved, exactly as `run --apply` does.
    """

    created_at: datetime
    decisions_path: str
    decisions_digest: str
    lidarr_digest: str
    follow: list[FollowArtist]
    save: list[SaveAlbum]
    already_followed: list[FollowArtist]
    """Matched, but Spotify says the artist is already followed - nothing to write."""
    already_saved: list[SaveAlbum]
    """Matched, but Spotify says the album is already saved - nothing to write."""
    unmatched: list[Unmatched]
    version: int = PROMOTE_SAVE_PLAN_VERSION
    searches_used: int = 0
    """Spotify `search` calls this plan cost, for the quota budget."""
    budget_exhausted: bool = False
    """True when the search budget ran out mid-plan; re-planning resumes from the search cache."""
    reviewed_path: str = ""
    """The review snapshot the save set was constrained to."""
    reviewed_digest: str = ""
    excluded_unreviewed: list[Unmatched] = field(default_factory=list)
    """Albums with files that no human reviewed, and which are therefore **not** saveable.

    Listed rather than counted silently: these are the albums a rule put on disk (a followed
    artist's catalogue, a liked or playlist track) since the review, and likearr is never allowed
    to turn one of those into a Spotify save on its own.
    """

    @property
    def is_empty(self) -> bool:
        return not (self.follow or self.save)


# ---------------------------------------------------------------- health


class RunStatus(enum.StrEnum):
    OK = "ok"
    GUARDED = "guarded"
    DEGRADED = "degraded"
    ERROR = "error"
    STALE = "stale"
    SKIPPED = "skipped"
    """A scheduled run that found the lock held: another run is doing the work. Not a failure."""
    PAUSED = "paused"
    """A scheduled run that did nothing because `[schedule] enabled` is false, or because
    no hand apply has completed yet (the message then says it is waiting for the first
    reviewed apply). Not a failure and not `skipped`: nothing is "in progress" here, the schedule
    itself is holding. A hand run never gets this status - both holds are scheduled-only."""


APPLIED_STATUSES = frozenset({RunStatus.OK, RunStatus.GUARDED, RunStatus.DEGRADED})
"""Statuses of an apply that actually ran. A stale, skipped or failed one changed nothing."""


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GUARDED = 2
EXIT_STALE = 3
EXIT_BUSY = 4
"""A hand-run command found another run holding the run lock, and did nothing. Not a fault:
try again when that run ends. A *scheduled* run that finds the lock held exits 0 (`skipped`)."""

PHASE_MARKER_APPLY = "likearr-phase: apply"
"""Printed to stderr, on its own line, by `shell.apply.apply` at the single point where planning
ends and the first Lidarr write begins: right before `_execute` is called.
Never printed on a dry run. `web.jobs.JobRunner` looks for this exact line in a scheduled job's
`log.txt` to tell a redeploy whether the child is still safe to cancel (nothing written to Lidarr
yet) or must be drained (it may have started writing)."""

PROGRESS_MARKER_POST_RESOLVE = "progress: reading Lidarr and building the plan"
"""Logged by `shell.plan.plan` the moment resolving ends, before Lidarr's album-bearing view is
read and the diff is built. Resolving already logs throttled `progress:` lines while
it runs, and the job page shows the newest one for as long as the job is in progress; without this
marker the page kept showing the last resolve line - stale ETA included - all through the
Lidarr-read-and-diff work that follows, which can itself take minutes on a big library.
`web.app._phase` and `_progress_line` pick it up the same way they already pick up a resolve line:
a plain substring scan, newest line wins."""


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """What must hold for two runs' identities to mean the same thing.

    A resolver bump moves every resolution, a `liked_track_scope` change re-resolves every like,
    and a source added or removed changes which intents exist at all. Comparing across any of
    those would read the whole world as new, so the run re-baselines silently instead.
    """

    resolver_version: int
    liked_track_scope: str
    source_set: tuple[str, ...]
    """Sorted `SourceSnapshot.counts` keys: the enabled sources and the playlists, by id."""
    rules: str = ""
    """`ExclusionRules.token`: which opt-outs were in force. `""` for the defaults.

    Turning an opt-out on converts mapped tracks into excluded ones by design, which is a rate of
    mapping shortfall the jump rule would otherwise read as a fault - and on a library of a
    few thousand intents one switch can easily clear the 5% threshold. It belongs here for the same reason
    `liked_track_scope` does: the run has not got worse, it has been asked a different question.

    Defaulting to `""` is what keeps deploying the code silent. An existing baseline was written
    with no `rules` at all, so it compares equal to a default configuration and the first
    post-deploy run still compares against it normally."""

    def mismatch(self, other: Fingerprint) -> str:
        """Why these two cannot be compared, or `""` when they can."""
        if self.resolver_version != other.resolver_version:
            return "resolver-version-changed"
        if self.liked_track_scope != other.liked_track_scope:
            return "scope-changed"
        if self.source_set != other.source_set:
            return "sources-changed"
        if self.rules != other.rules:
            return "rules-changed"
        return ""


@dataclass(frozen=True, slots=True)
class HealthBaseline:
    """The previous run's identities, with the fingerprint they were collected under."""

    fingerprint: Fingerprint
    identities: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def of(self, dimension: str) -> frozenset[str]:
        return self.identities.get(dimension, frozenset())


@dataclass(slots=True)
class HealthRecord:
    ts: int
    version: str
    resolver_version: int
    exit_code: int
    status: RunStatus
    spotify_ok: bool
    spotify_schema_ok: bool
    mb_ok: bool
    lidarr_ok: bool
    lidarr_metadata_ok: bool
    counts: dict[str, int]
    unmapped: int
    pending_album: int
    message: str = ""
    dry_run: bool = True

    # ---- change detection (additive; every field above keeps its name and meaning) ----
    #
    # `unmapped`, `lidarr_metadata_ok` and friends describe the run's *state*, which on a real
    # library is often the same bad state every run. These describe what *moved*,
    # which is what a health signal has to carry if it is to mean anything. See `core.health`.
    unmapped_new: int = 0
    """Unmapped intents that were not unmapped last run."""
    unmapped_resolved: int = 0
    """Unmapped last run, mapped (or gone) now. Informational."""
    unmapped_ratio: float = 0.0
    """`unmapped / intents`. The standing ratio that `guards.unmapped_ratio_amber` was written for."""
    regressions: int = 0
    """Newly-shortfalling releases whose intent existed last run - the only input to the jump rule.

    Distinct from the `*_new` counts, which include brand-new intents that never mapped. Liking
    300 songs at once adds ~60 of those and is not a fault; 60 releases that worked yesterday and
    do not today is.
    """
    catalogue_gaps: int = 0
    """Followed-catalogue releases Lidarr does not track (promos, bootlegs, non-Official).

    Excludes the recent ones below: those are a different condition with a different fix."""
    catalogue_gaps_new: int = 0
    catalogue_gaps_recent: int = 0
    """Followed-catalogue releases dated within `[rules] recent_release_days`, or still to come.

    Lidarr's metadata simply has not caught up yet, and likearr queues a scoped RefreshArtist for
    each one's artist. A handful on any given run is normal. `catalogue_gaps_recent` minus
    `catalogue_gaps_recent_new` is the number that were already here at the last apply - a
    refresh was queued for them and Lidarr still does not have the release. That is the number
    worth watching, and the one that says Lidarr's metadata proxy is not doing its job."""
    catalogue_gaps_recent_new: int = 0
    refresh_failures: int = 0
    """Freshness refreshes that failed or timed out this run (apply only).

    Deliberately not a skipped artist: the artist was already in Lidarr with a catalogue this run
    read, so it keeps its monitors and this costs only the one release it was chasing. It is a
    count rather than a condition for the same reason - a sticky class-B degrade for an
    opportunistic request would be the permanent amber again."""
    absent_in_lidarr: int = 0
    """Apply only: wanted releases Lidarr had no album for, retried next run."""
    absent_in_lidarr_new: int = 0
    lidarr_metadata_errors: int = 0
    """Lidarr metadata lookups that failed this run. Chronically non-zero: some terms always 503."""
    lidarr_metadata_errors_new: int = 0
    mb_errors: int = 0
    """MusicBrainz lookups that went wrong this run: intents abandoned, plus lookups whose refetch
    failed and were answered from an expired cache entry instead. The second kind lost nothing, so
    it is counted here but is deliberately not part of `mb_ok`."""
    skipped_artists: int = 0
    """Artists skipped because Lidarr's metadata failed on them."""
    skipped_artists_new: int = 0
    name_collisions: int = 0
    name_collisions_new: int = 0
    catalogue_too_large: int = 0
    """Followed artists past MusicBrainz's browse ceiling."""
    catalogue_too_large_new: int = 0
    baseline: str = ""
    """`compared`, or why this run could not compare: `first-run`, `resolver-version-changed`,
    `scope-changed`, `sources-changed`, `rules-changed`. Empty on a run that never got as far as
    a plan."""
    baseline_advanced: bool = False
    """True when this run wrote the baseline. Only an apply does."""
    new_conditions: list[str] = field(default_factory=list)
    """Short codes for what is newly wrong. Empty on an `ok` run; this is what HA should read."""
    changes_made: int | None = None
    """An apply's changes to Lidarr that were made: artists added, releases monitored and
    unmonitored, profiles ratcheted. With `changes_planned`, it tells an
    apply that stopped part-way from one that changed nothing. ``None`` on a dry run, and on a
    record from before it was kept."""
    changes_planned: int | None = None
    """The changes the applied diff asked for; ``None`` when it never got as far as knowing. An
    upper bound: a release found already monitored, missing from Lidarr's catalogue, or under an
    artist skipped this run is asked for and never made, so a clean apply can make fewer. Only an
    `error` apply is read against it: as having stopped part-way when `changes_made` falls short,
    or as having landed everything but lost the confirmation when it does not."""
    lidarr_changed: bool | None = None
    """Whether the apply wrote to Lidarr at all - the counted changes, or anything else: a tag or
    profile created, a profile set, new-item monitoring changed, an artist re-monitored. What makes
    "changed nothing" true. ``None`` on a dry run and on older records."""
    tagged_without_state: int = 0
    """Lidarr artists carrying the configured likearr tag that have no `owned_artists` row:
    the sign of a state database lost or replaced. Report only, so it moves neither the status nor
    the exit code. 0 on a run that never got as far as a plan, and on older records."""

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        d = asdict(self)
        d["status"] = str(self.status)
        return d
