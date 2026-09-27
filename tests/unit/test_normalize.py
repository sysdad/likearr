"""Table-driven tests for the title and name folding the resolver compares strings with."""

from __future__ import annotations

import pytest

from likearr.core.normalize import (
    credits_match,
    has_remix_marker,
    normalize_name,
    normalize_title,
    strip_bare_featuring,
    strip_release_qualifiers,
)

TITLES = [
    # (input, expected)
    ("Karma Police", "karma police"),
    ("KARMA POLICE", "karma police"),
    ("  Karma   Police  ", "karma police"),
    # dash-separated qualifiers, the Spotify house style
    ("Get Lucky - Radio Edit", "get lucky"),
    ("Blinding Lights - Single Version", "blinding lights"),
    ("Something - Edit", "something"),
    ("Karma Police - Remastered 2011", "karma police"),
    ("Let It Be - 2009 Remaster", "let it be"),
    ("Song - Extended Mix", "song"),
    ("Song - Live at Wembley", "song"),
    ("All Too Well - Taylor's Version", "all too well"),
    # stacked qualifiers strip right to left
    ("Song - Remastered 2011 - Radio Edit", "song"),
    # bracketed qualifiers
    ("Karma Police (Remastered)", "karma police"),
    ("Karma Police [Remastered 2011]", "karma police"),
    ("Uptown Funk (feat. Bruno Mars)", "uptown funk"),
    ("Song (Live)", "song"),
    ("Song (Deluxe Edition)", "song"),
    ("Song (Bonus Track)", "song"),
    ("Song (feat. X) [Remastered]", "song"),
    # bare featured credits
    ("Uptown Funk feat. Bruno Mars", "uptown funk"),
    ("Uptown Funk ft. Bruno Mars", "uptown funk"),
    ("Uptown Funk featuring Bruno Mars", "uptown funk"),
    ("Uptown Funk FEAT. Bruno Mars", "uptown funk"),
    # punctuation, diacritics, ampersands
    ("Uptown Funk!", "uptown funk"),
    ("Hey, Jude", "hey jude"),
    ("Déjà Vu", "deja vu"),
    ("Rock & Roll", "rock and roll"),
    ("Rock and Roll", "rock and roll"),
    ("O.K. Computer", "o k computer"),
    ("Sgt. Pepper's Lonely Hearts Club Band", "sgt pepper s lonely hearts club band"),
    # brackets that are NOT qualifiers survive, because they are probably the title
    ("Jeremy (Parking Lot)", "jeremy parking lot"),
    ("Everything In Its Right Place", "everything in its right place"),
    # a title that is entirely non-Latin keeps a distinguishing form rather than folding to ""
    ("こんにちは", "こんにちは"),
]

NAMES = [
    ("Radiohead", "radiohead"),
    ("RADIOHEAD", "radiohead"),
    ("The Weeknd", "weeknd"),
    ("the beatles", "beatles"),
    ("Björk", "bjork"),
    ("Simon & Garfunkel", "simon and garfunkel"),
    ("Simon and Garfunkel", "simon and garfunkel"),
    ("Sigur Rós", "sigur ros"),
    ("  Daft   Punk ", "daft punk"),
    ("AC/DC", "ac dc"),
    ("Panic! At The Disco", "panic at the disco"),
]


@pytest.mark.parametrize(("raw", "expected"), TITLES)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), NAMES)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(("raw", "_expected"), TITLES)
def test_normalize_title_is_idempotent(raw: str, _expected: str) -> None:
    once = normalize_title(raw)
    assert normalize_title(once) == once


@pytest.mark.parametrize(("raw", "_expected"), NAMES)
def test_normalize_name_is_idempotent(raw: str, _expected: str) -> None:
    once = normalize_name(raw)
    assert normalize_name(once) == once


def test_two_non_latin_titles_do_not_collide() -> None:
    """The empty-fold guard exists so unrelated non-Latin titles never compare equal."""
    assert normalize_title("こんにちは") != normalize_title("さようなら")


DIFFERENT_OUTSIDE_PLAIN_LATIN = [
    # #166: letters that do not decompose, and the non-Latin half of a mixed-script string, used
    # to vanish from the fold, so each of these pairs compared equal
    ("MØ", "M"),
    ("Łona", "Ona"),
    ("Часть 1", "Глава 1"),
    ("爱 Love", "恨 Love"),
]

SAME_OUTSIDE_PLAIN_LATIN = [
    ("Straße", "Strasse"),
    ("Björk", "Bjork"),
    ("Sigur Rós", "Sigur Ros"),
    ("Ærø", "Aero"),
    ("Þór", "Thor"),
]


@pytest.mark.parametrize(("a", "b"), DIFFERENT_OUTSIDE_PLAIN_LATIN)
def test_letters_outside_plain_latin_tell_titles_and_credits_apart(a: str, b: str) -> None:
    from likearr.adapters.musicbrainz import _normalize

    assert normalize_title(a) != normalize_title(b)
    assert not credits_match(a, b)
    assert _normalize(a) != _normalize(b), "the adapter folds the same way"


@pytest.mark.parametrize(("a", "b"), SAME_OUTSIDE_PLAIN_LATIN)
def test_letters_with_a_plain_latin_spelling_still_match_it(a: str, b: str) -> None:
    from likearr.adapters.musicbrainz import _normalize

    assert normalize_title(a) == normalize_title(b)
    assert credits_match(a, b)
    assert _normalize(a) == _normalize(b)


def test_a_title_of_punctuation_alone_still_keeps_a_distinguishing_form() -> None:
    assert normalize_name("!!!") != normalize_name("???")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # #166: a bare "feat"/"ft" word inside a title is not a credit when all that precedes it
        # is one short word or a number
        ("A Feat of Clay", "a feat of clay"),
        ("50 Ft Queenie", "50 ft queenie"),
        ("Uptown Funk feat. Bruno Mars", "uptown funk"),
        ("Crazy In Love feat. Jay-Z", "crazy in love"),
        ("Feat of Strength", "feat of strength"),
    ],
)
def test_a_bare_featuring_word_splits_only_in_credit_position(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_credits_match() -> None:
    """The contract both the adapter's credit gate and the resolver's relationship join rely on."""
    assert credits_match("Branford Marsalis Quartet", "The Branford Marsalis Quartet")
    assert credits_match("The Marty Paich Quartet featuring Art Pepper", "The Marty Paich Quartet")
    assert credits_match("Björk", "Bjork")
    assert credits_match("Simon & Garfunkel", "Simon and Garfunkel")
    assert not credits_match("John Mayer", "John Mayer Trio")
    assert not credits_match("Lawrence", "Clyde Lawrence")


def test_empty_input_is_empty() -> None:
    assert normalize_title("") == ""
    assert normalize_name("") == ""


def test_the_is_only_stripped_from_the_front_of_a_name() -> None:
    assert normalize_name("The The") == "the"
    assert normalize_title("The Beatles") == "the beatles"


# --------------------------------------------------------------------------- strip_release_qualifiers

RELEASE_QUALIFIERS = [
    # (input, expected)
    ("The Beatles (Remastered)", "The Beatles"),
    ("Know-It-All (Deluxe)", "Know-It-All"),
    ("Face Value (Deluxe Editon)", "Face Value"),  # sic: a recorded Spotify misspelling
    ("Mercy, Mercy, Mercy (Live)", "Mercy, Mercy, Mercy"),
    ("I'm Ready - EP", "I'm Ready"),
    ("My Type - Single", "My Type"),
    ("Game Winner - EP (Deluxe Edition)", "Game Winner"),
    ("Everything In Transit (Non-PA Release)", "Everything In Transit"),
    ("Break Our Fall (In Progress)", "Break Our Fall"),
    # issue #21: a bare trailing qualifier with no bracket or " - " separator at all - the shape
    # MusicBrainz stores "Kangaroo EP" in, undecorated.
    ("Kangaroo EP", "Kangaroo"),
    # issue #21: added to the release-qualifier vocabulary for the Noelle/Superfly/Elf cases.
    ("Noelle (Original Motion Picture Soundtrack)", "Noelle"),
    ("Superfly (Original Soundtrack)", "Superfly"),
    ("Elf (Music from the Major Motion Picture)", "Elf"),
]

RELEASE_QUALIFIER_NEGATIVES = [
    # trailing decorations only: a leading parenthetical is part of the title
    "(What's the Story) Morning Glory?",
    "Sgt. Pepper's Lonely Hearts Club Band",
    "Songs in the Key of Life",
    # a judgement call: "Live" is the first word of the title, not a trailing qualifier segment,
    # so there is nothing at the end to strip and the title is left exactly as it is
    "Live at Leeds",
    # the bare trailing form (no bracket, no " - ") only ever strips "EP" - the one measured
    # case. A bare trailing "Live" (or "Deluxe", "Acoustic", "Version", "Edition", ...) is far
    # weaker evidence of decoration than a bracketed one, and "Some Album Live" is a different
    # record from "Some Album" - so it is left exactly as it is.
    "Some Album Live",
]


@pytest.mark.parametrize(("raw", "expected"), RELEASE_QUALIFIERS)
def test_strip_release_qualifiers(raw: str, expected: str) -> None:
    assert strip_release_qualifiers(raw) == expected


@pytest.mark.parametrize("raw", RELEASE_QUALIFIER_NEGATIVES)
def test_strip_release_qualifiers_leaves_non_trailing_text_alone(raw: str) -> None:
    assert strip_release_qualifiers(raw) == raw


@pytest.mark.parametrize(("raw", "_expected"), RELEASE_QUALIFIERS)
def test_strip_release_qualifiers_is_idempotent(raw: str, _expected: str) -> None:
    once = strip_release_qualifiers(raw)
    assert strip_release_qualifiers(once) == once


def test_strip_release_qualifiers_never_returns_empty() -> None:
    assert strip_release_qualifiers("(Live)") == "(Live)"
    assert strip_release_qualifiers("") == ""


def test_strip_release_qualifiers_preserves_case_and_punctuation() -> None:
    """Unlike normalize_title, the result feeds a search query, not an equality test."""
    assert strip_release_qualifiers("KARMA POLICE (Remastered)") == "KARMA POLICE"


# --------------------------------------------------------------------------- strip_bare_featuring


def test_strip_bare_featuring_drops_the_credit_and_everything_after() -> None:
    assert strip_bare_featuring("The Marty Paich Quartet featuring Art Pepper") == "The Marty Paich Quartet"
    assert strip_bare_featuring("Mark Ronson feat. Bruno Mars") == "Mark Ronson"
    assert strip_bare_featuring("Mark Ronson ft. Bruno Mars") == "Mark Ronson"


def test_strip_bare_featuring_leaves_a_credit_without_one_alone() -> None:
    assert strip_bare_featuring("The Marty Paich Quartet") == "The Marty Paich Quartet"


# --------------------------------------------------------------------------- has_remix_marker


@pytest.mark.parametrize(
    "title",
    [
        "Grease (The Remix EP)",  # issue #15, MBID 2f26958e-b86d-3b3c-8a15-57253046ea58
        "The Feeling (Remixes)",  # issue #15, MBID d83c4c9b-e79a-4f6d-97f3-cda0045bd993
        "Uptown Funk (feat. Bruno Mars) [The Remixes]",
        "Blinding Lights - Chromatics Remix",
        "Some Song (Remixed)",
        "Remixes",
        "REMIX",
    ],
)
def test_has_remix_marker_finds_the_word_wherever_it_sits(title: str) -> None:
    """Unlike the stripping vocabulary, this matches outside brackets too - see its docstring."""
    assert has_remix_marker(title)


@pytest.mark.parametrize(
    "title",
    [
        "Uptown Special",
        "Remixture",  # the word is a prefix of a real word, not the word
        "Premixed",
        "OK Computer",
        "",
    ],
)
def test_has_remix_marker_leaves_ordinary_titles_alone(title: str) -> None:
    assert not has_remix_marker(title)
