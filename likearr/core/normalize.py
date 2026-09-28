"""Deterministic title and name normalisation.

The resolver compares Spotify's strings with MusicBrainz's strings. Neither side is
authoritative about punctuation, case, qualifiers or featured credits, so both sides are
folded through the same pure functions before any equality test. Everything here is
deterministic and depends on nothing but its argument.

Two vocabularies:

- :func:`normalize_title` for release and track titles. It removes *qualifiers* - the
  bracketed or dash-separated trailing junk that distinguishes pressings of the same song
  ("- Radio Edit", "(Remastered 2011)", "feat. Someone") - and then folds what is left.
- :func:`normalize_name` for artist names. It folds and drops a leading "the ".

Folding means: NFKD decomposition with combining marks dropped (so "Bjork" == "Björk"),
lowercase, the Latin letters that do not decompose spelled out ("ß" as "ss", "ø" as "o"), "&"
read as "and", and every run of characters that are not letters or digits - in any script -
collapsed to one space. A title whose characters all disappear in that fold (punctuation only)
keeps a casefolded whitespace-collapsed form instead, so two such titles never both normalise
to the empty string and compare equal.

Qualifier vocabulary (a bracketed or " - " separated segment is a qualifier when it):

- starts with a featured-credit or provenance head: ``feat.``, ``ft.``, ``featuring``,
  ``with``, ``live at/from/in``, ``from``, ``recorded at/live``; or
- ends with a version word: ``version``, ``edit``, ``mix``, ``remix``, ``remaster(ed)``,
  ``master(ed)``, ``cut``, ``take``, ``edition``, ``reissue``, ``re-recording``; or
- begins with ``remaster``/``remastered`` (catches "Remastered 2011", where the last word is
  a year); or
- is one of a small set of standalone words: ``live``, ``mono``, ``stereo``, ``explicit``,
  ``clean``, ``bonus``, ``bonus track``, ``demo``, ``acoustic``, ``instrumental``,
  ``deluxe``, ``expanded``, ``single``, ``original``.

Anything else inside brackets is kept, because it is probably part of the title.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "credits_match",
    "fold_title",
    "has_remix_marker",
    "normalize_name",
    "normalize_title",
    "strip_bare_featuring",
    "strip_release_qualifiers",
]

_QUALIFIER_HEADS: tuple[str, ...] = (
    "feat.",
    "feat ",
    "ft.",
    "ft ",
    "featuring ",
    "with ",
    "live at",
    "live from",
    "live in",
    "from ",
    "recorded at",
    "recorded live",
)

_QUALIFIER_TAILS: frozenset[str] = frozenset(
    {
        "version",
        "versions",
        "edit",
        "edits",
        "mix",
        "remix",
        "remaster",
        "remastered",
        "master",
        "mastered",
        "cut",
        "take",
        "edition",
        "reissue",
        "rerecording",
        "re-recording",
    }
)

_QUALIFIER_EXACT: frozenset[str] = frozenset(
    {
        "live",
        "mono",
        "stereo",
        "explicit",
        "clean",
        "bonus",
        "bonus track",
        "demo",
        "acoustic",
        "instrumental",
        "deluxe",
        "expanded",
        "single",
        "original",
    }
)

# :func:`strip_release_qualifiers` targets whole release (album) titles, which carry a few
# qualifier phrases :func:`normalize_title`'s vocabulary never needed for tracks - "EP" is a
# release format, not a pressing detail, and "Non-PA Release" / "In Progress" are Spotify's own
# house style for a clean/explicit split and a work-in-progress release respectively.
_RELEASE_QUALIFIER_EXACT: frozenset[str] = _QUALIFIER_EXACT | frozenset(
    {
        "ep",
        "anniversary",
        "anniversary edition",
        "in progress",
        "non-pa",
        "non-pa release",
        "original soundtrack",
        "original motion picture soundtrack",
        "music from the major motion picture",
    }
)

# "Editon" is a recorded Spotify misspelling of "Edition" (Jack Johnson - "Face Value (Deluxe
# Editon)", sic). It is listed explicitly rather than matched by a prefix heuristic, which would
# risk stripping real title words that happen to start the same way.
_RELEASE_QUALIFIER_TAILS: frozenset[str] = _QUALIFIER_TAILS | frozenset({"editon"})

_REMASTER_HEAD = re.compile(r"^re-?master(ed|ing)?\b")
_BRACKETED = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_BARE_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def _is_qualifier(segment: str) -> bool:
    """True when a bracketed or dash-separated segment is disposable pressing metadata."""
    seg = _SPACES.sub(" ", segment.strip().strip("\"'.,;:")).lower()
    if not seg:
        return False
    if seg in _QUALIFIER_EXACT:
        return True
    if seg.startswith(_QUALIFIER_HEADS):
        return True
    if _REMASTER_HEAD.match(seg):
        return True
    return seg.rsplit(" ", 1)[-1] in _QUALIFIER_TAILS


def _drop_bracketed_qualifiers(title: str) -> str:
    """Remove every ``(...)``/``[...]`` group whose content is a qualifier."""

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        return " " if _is_qualifier(inner) else m.group(0)

    previous = None
    out = title
    while out != previous:  # a second pass catches nesting such as "((Remastered))"
        previous = out
        out = _BRACKETED.sub(repl, out)
    return out


def _drop_dash_qualifiers(title: str) -> str:
    """Remove trailing ``" - <qualifier>"`` segments, right to left."""
    out = title
    while True:
        head, sep, tail = out.rpartition(" - ")
        if not sep or not _is_qualifier(tail):
            return out
        out = head


_LATIN_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "ß": "ss", "þ": "th", "ð": "d", "đ": "d"})
"""Latin letters NFKD does not decompose, spelled the way an English catalogue writes them, so
"Straße" still equals "Strasse" and "Ærø" equals "Aero" once letters outside ``[a-z]`` are kept."""


def _fold(text: str) -> str:
    """Lowercase, strip diacritics and punctuation, collapse whitespace; keep every letter.

    Any character `str.isalnum` accepts survives, in any script: an ASCII-only fold dropped the
    letters that do not decompose (``ø``, ``ł``) and the non-Latin half of a mixed-script string,
    so "MØ" equalled "M" and "Часть 1" equalled "Глава 1". The MusicBrainz adapter's
    own normaliser folds through `fold_title` too, so the two sides cannot drift apart.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.lower().translate(_LATIN_LETTERS).replace("&", " and ")
    kept = "".join(c if c.isalnum() else " " for c in lowered)
    folded = _SPACES.sub(" ", kept).strip()
    if folded:
        return folded
    # Nothing survived (punctuation only, "!!!"). Keep a casefolded form rather than returning
    # "", which would make every such title compare equal to every other.
    return _SPACES.sub(" ", stripped.casefold()).strip()


def normalize_title(title: str) -> str:
    """Normalise a release or track title for equality comparison.

    Strips bracketed and ``" - "`` separated qualifiers, drops a bare ``feat.``/``ft.`` credit
    and everything after it, then folds. See the module docstring for the exact qualifier
    vocabulary.

    >>> normalize_title("Get Lucky - Radio Edit")
    'get lucky'
    >>> normalize_title("Karma Police (Remastered 2011)")
    'karma police'
    >>> normalize_title("Uptown Funk feat. Bruno Mars")
    'uptown funk'
    """
    out = _drop_bracketed_qualifiers(title)
    out = _drop_dash_qualifiers(out)
    head = _BARE_FEAT.split(out, maxsplit=1)[0]
    if head != out and not _too_short_for_a_credit(head):
        out = head
    return _fold(out)


def _too_short_for_a_credit(head: str) -> bool:
    """True when what precedes a bare ``feat``/``ft`` is one short word or a number.

    Then the word is part of the title, not a featured credit: "A Feat of Clay" and "50 Ft
    Queenie" are whole titles, and splitting them left "a" and "50", which equal any track titled
    that. "Uptown Funk feat. Bruno Mars" still splits.
    """
    words = _fold(head).split()
    return len(words) == 1 and (len(words[0]) <= 3 or words[0].isdigit())


def fold_title(title: str) -> str:
    """Fold a title - case, diacritics, punctuation, ``&`` - and remove **nothing** else.

    The literal comparison :func:`normalize_title` is too generous for: it drops every qualifier,
    so ``"All Over (Bear//Face Remix)"`` and ``"All Over"`` both become ``"all over"``, which is
    right for "is this the same release" and wrong for "which of these did Spotify name". The
    resolver uses this to prefer a release whose title is the one Spotify printed.

    >>> fold_title("All Over (Bear//Face Remix)")
    'all over bear face remix'
    >>> fold_title("Björk & Friends")
    'bjork and friends'
    """
    return _fold(title)


def normalize_name(name: str) -> str:
    """Normalise an artist name for equality comparison.

    Folds (see the module docstring) and then drops a leading ``the``.

    >>> normalize_name("The Weeknd")
    'weeknd'
    >>> normalize_name("Bj\N{LATIN SMALL LETTER O WITH DIAERESIS}rk")
    'bjork'
    """
    folded = _fold(name)
    if folded.startswith("the "):
        folded = folded[4:]
    return folded


_TRAILING_BRACKET = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]\s*$")


def _is_release_qualifier(segment: str) -> bool:
    """Like :func:`_is_qualifier`, extended with release-only vocabulary (see above)."""
    seg = _SPACES.sub(" ", segment.strip().strip("\"'.,;:")).lower()
    if not seg:
        return False
    if seg in _RELEASE_QUALIFIER_EXACT:
        return True
    if seg.startswith(_QUALIFIER_HEADS):
        return True
    if _REMASTER_HEAD.match(seg):
        return True
    return seg.rsplit(" ", 1)[-1] in _RELEASE_QUALIFIER_TAILS


_BARE_TRAILING_QUALIFIERS: frozenset[str] = frozenset({"ep"})
"""Words :func:`_strip_one_release_qualifier` will strip bare, with no bracket or `` - `` at all.

Deliberately just the one measured case (Kyle Andrews - "Kangaroo" vs MusicBrainz's "Kangaroo
EP"), not the whole of `_RELEASE_QUALIFIER_EXACT`. A bracket or a dash is real evidence that a
trailing word is decoration; a bare trailing word is not - "Some Album Live" and "Some Album" are
different records, and stripping every bare "Live", "Deluxe", "Acoustic", "Version" or "Edition"
on the strength of nothing but the word itself would conflate them. Widen this set only against
another measured case, named alongside it.
"""


def _strip_one_release_qualifier(title: str) -> str | None:
    """Remove one trailing qualifier - a ``(...)``/``[...]`` group, a `` - ...`` tail, or a bare
    trailing ``"EP"`` with no separator at all (``"Kangaroo EP"`` -> ``"Kangaroo"``) - or None.

    The bare form is narrow on purpose - see `_BARE_TRAILING_QUALIFIERS`.
    """
    bracket = _TRAILING_BRACKET.search(title)
    if bracket is not None and _is_release_qualifier(bracket.group(1)):
        return title[: bracket.start()].rstrip()
    head, sep, tail = title.rpartition(" - ")
    if sep and _is_release_qualifier(tail):
        return head.rstrip()
    head, sep, tail = title.rstrip().rpartition(" ")
    if sep and _SPACES.sub(" ", tail.strip().strip("\"'.,;:")).lower() in _BARE_TRAILING_QUALIFIERS:
        return head.rstrip()
    return None


def strip_release_qualifiers(title: str) -> str:
    """Strip Spotify's trailing release-title decorations, for a retry release-group search.

    MusicBrainz stores a release's plain title; Spotify often decorates it with a trailing
    qualifier MusicBrainz does not carry, e.g. ``"The Beatles (Remastered)"``, ``"Know-It-All
    (Deluxe)"``, ``"I'm Ready - EP"`` or ``"Break Our Fall (In Progress)"``. A lucene phrase
    search for the decorated title then misses the plain one MusicBrainz actually has.

    Only a **trailing** ``(...)``/``[...]`` group or a trailing `` - <qualifier>`` segment is
    removed - never one in the middle of the title - so a title that merely *starts* with a
    parenthetical, such as ``"(What's the Story) Morning Glory?"``, is left alone. Repeated until
    stable, so ``"Game Winner - EP (Deluxe Edition)"`` loses both decorations. Unlike
    :func:`normalize_title`, this keeps case and punctuation intact, because the result feeds a
    search query rather than an equality test.

    A title that is nothing but a qualifier once folded (or that a bug strips to nothing) falls
    back to the original input: this function must never hand the caller an empty query.

    A title such as ``"Live at Leeds"`` is left untouched even though it starts with a qualifier
    word - it has no trailing ``(...)``/`` - `` segment to remove, so there is nothing to strip,
    which is the judgement call: a leading or mid-title qualifier-looking word is treated as part
    of the title, never as decoration, because only Spotify's *trailing* decorations are the
    problem this function fixes.

    >>> strip_release_qualifiers("The Beatles (Remastered)")
    'The Beatles'
    >>> strip_release_qualifiers("I'm Ready - EP")
    "I'm Ready"
    >>> strip_release_qualifiers("Game Winner - EP (Deluxe Edition)")
    'Game Winner'
    >>> strip_release_qualifiers("Live at Leeds")
    'Live at Leeds'
    """
    out = title
    while True:
        stripped = _strip_one_release_qualifier(out)
        if stripped is None or stripped == out:
            break
        out = stripped
    out = out.strip()
    return out if out else title


_REMIX_MARKER = re.compile(r"(?<![0-9a-z])re-?mix(e[sd])?(?![0-9a-z])", re.IGNORECASE)
"""A whole-word ``remix`` / ``remixes`` / ``remixed`` anywhere in a title.

Deliberately *not* restricted to a bracketed or `` - `` separated segment, unlike every other
vocabulary in this module, and the asymmetry is the reason. Elsewhere the question is "may I
*delete* this word", where a bare trailing word is not enough evidence, because "Some Album Live"
and "Some Album" are different records and conflating them monitors the wrong one. Here the
question is "may I *refuse* this release", and the cost of over-matching is that a track is
reported excluded in the unmapped report, where a human sees it and can allow it back with one
deny-list edit or by saving the album on Spotify. Refusing too much is visible and reversible;
deleting a real title word is silent and wrong. So this one matches the word wherever it appears.

The lookaround is character-class rather than ``\\b`` so that a title glued to the word without a
space ("Remixes!", "[Remix]") still matches while "remixture" does not.
"""


def has_remix_marker(text: str) -> bool:
    """True when a release or track title announces itself as a remix.

    Used on both sides of the same question by ``[rules] allow_remix_releases``: a release group
    whose title carries the marker is refused, *unless* the liked track's own title carries it
    too, because then the remix is what the user actually liked.

    MusicBrainz's ``Remix`` secondary type is the primary signal and is checked separately; this
    exists because the type is very often simply not set. Real examples - "Grease
    (The Remix EP)", "The Feeling (Remixes)" - are typed ``EP`` with **no** secondary types at
    all, which is exactly why they beat the real album on size and why the type check alone
    changes nothing.

    >>> has_remix_marker("Grease (The Remix EP)")
    True
    >>> has_remix_marker("The Feeling (Remixes)")
    True
    >>> has_remix_marker("Uptown Special")
    False
    >>> has_remix_marker("Remixture")
    False
    """
    return _REMIX_MARKER.search(text) is not None


def strip_bare_featuring(text: str) -> str:
    """Drop a bare ``feat.``/``ft.``/``featuring`` credit and everything after it.

    :func:`normalize_title` already does this for track titles via the same vocabulary
    (``_BARE_FEAT``); this exposes it standalone so an artist *credit* string can have Spotify's
    ``"featuring"`` decoration removed before an equality comparison too - e.g. ``"The Marty
    Paich Quartet featuring Art Pepper"`` -> ``"The Marty Paich Quartet"``. This carries no
    identity claim of its own: the credit still has to compare equal afterwards.

    >>> strip_bare_featuring("The Marty Paich Quartet featuring Art Pepper")
    'The Marty Paich Quartet'
    """
    return _BARE_FEAT.split(text, maxsplit=1)[0]


def credits_match(a: str, b: str) -> bool:
    """True when two artist credits name the same artist: :func:`normalize_name` equality after
    :func:`strip_bare_featuring` on each side.

    Full equality, never containment: ``"John Mayer"`` does not match ``"John Mayer Trio"``. The
    MusicBrainz adapter's name-search credit gate and the resolver's relationship join
    both use this one definition, so the two cannot drift apart.

    >>> credits_match("Branford Marsalis Quartet", "The Branford Marsalis Quartet")
    True
    >>> credits_match("John Mayer", "John Mayer Trio")
    False
    """
    return normalize_name(strip_bare_featuring(a)) == normalize_name(strip_bare_featuring(b))
