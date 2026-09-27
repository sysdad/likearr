"""`core.match`: what counts as a confident Spotify match, and what must be refused.

The refusals carry the weight here. A wrong match saves a stranger's album into a real library,
so every near miss in this file is deliberate and every one of them must come back unmatched.
"""

from __future__ import annotations

import pytest

from likearr.core.match import match_album, match_artist, spotify_id_from_url
from likearr.models import SpotifyAlbumRef, SpotifyArtistRef

RADIOHEAD = "4Z8W4fKeB5YxbusRsdQVPb"
"""A real Spotify artist id shape: 22 base62 characters."""

OK_COMPUTER = "6dVIqQ8qmQ5GBnJ9shOYGE"


def artist(spotify_id: str, name: str) -> SpotifyArtistRef:
    return SpotifyArtistRef(spotify_id=spotify_id, name=name)


def album(spotify_id: str, name: str, *artists: str) -> SpotifyAlbumRef:
    return SpotifyAlbumRef(
        spotify_id=spotify_id,
        name=name,
        artist_names=artists or ("Test Artist",),
        upc=None,
        album_type="album",
        release_date=None,
    )


# --------------------------------------------------------------------------- relationship URLs


@pytest.mark.parametrize(
    ("url", "kind", "expected"),
    [
        (f"https://open.spotify.com/artist/{RADIOHEAD}", "artist", RADIOHEAD),
        (f"http://open.spotify.com/artist/{RADIOHEAD}", "artist", RADIOHEAD),
        (f"https://open.spotify.com/album/{OK_COMPUTER}", "album", OK_COMPUTER),
        (f"https://open.spotify.com/intl-de/album/{OK_COMPUTER}", "album", OK_COMPUTER),
        (f"https://open.spotify.com/album/{OK_COMPUTER}?si=abc123", "album", OK_COMPUTER),
        (f"https://play.spotify.com/album/{OK_COMPUTER}", "album", OK_COMPUTER),
        (f"spotify:artist:{RADIOHEAD}", "artist", RADIOHEAD),
        (f"  https://open.spotify.com/artist/{RADIOHEAD}  ", "artist", RADIOHEAD),
    ],
)
def test_a_spotify_relationship_url_yields_its_id(url: str, kind: str, expected: str) -> None:
    assert spotify_id_from_url(url, kind) == expected


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (f"https://open.spotify.com/track/{RADIOHEAD}", "album"),  # wrong entity type
        (f"https://open.spotify.com/artist/{RADIOHEAD}", "album"),  # artist link, album wanted
        (f"https://open.spotify.com/album/{OK_COMPUTER}", "artist"),  # album link, artist wanted
        (f"https://open.spotify.com/playlist/{OK_COMPUTER}", "album"),
        (f"spotify:track:{OK_COMPUTER}", "album"),
        ("https://open.spotify.com/album/short", "album"),  # not a 22-char base62 id
        (f"https://open.spotify.com/album/{OK_COMPUTER}x", "album"),  # 23 characters
        (f"https://open.spotify.com/album/{OK_COMPUTER[:-1]}-", "album"),  # base62 only
        ("https://open.spotify.com/album/", "album"),  # no id at all
        ("https://open.spotify.com/", "album"),
        (f"https://not-spotify.example/album/{OK_COMPUTER}", "album"),  # lookalike host
        (f"https://openspotify.com.evil.test/album/{OK_COMPUTER}", "album"),
        (f"ftp://open.spotify.com/album/{OK_COMPUTER}", "album"),  # not http(s)
        ("", "album"),
        ("not a url at all", "album"),
    ],
)
def test_a_url_that_is_not_a_plausible_link_falls_through(url: str, kind: str) -> None:
    """Falling through costs a search. Trusting one of these would save the wrong record."""
    assert spotify_id_from_url(url, kind) is None


# --------------------------------------------------------------------------- artists


def test_an_exact_artist_name_matches() -> None:
    result = match_artist("Radiohead", [artist("sp-1", "Radiohead")])
    assert result.matched
    assert result.spotify_id == "sp-1"
    assert result.step == "artist:name"


def test_artist_matching_folds_case_accents_and_punctuation() -> None:
    assert match_artist("Bjork", [artist("sp-1", "Björk")]).spotify_id == "sp-1"
    assert match_artist("The Weeknd", [artist("sp-1", "Weeknd")]).spotify_id == "sp-1"
    assert match_artist("Simon & Garfunkel", [artist("sp-1", "Simon and Garfunkel")]).spotify_id == "sp-1"


def test_a_near_miss_artist_is_refused_rather_than_guessed() -> None:
    result = match_artist("Ghost", [artist("sp-1", "Ghost B.C."), artist("sp-2", "Ghostface Killah")])
    assert not result.matched
    assert "no Spotify artist named 'Ghost'" in result.reason


def test_two_artists_with_the_same_name_are_ambiguous() -> None:
    result = match_artist("Low", [artist("sp-1", "Low"), artist("sp-2", "LOW")])
    assert not result.matched, "two real artists share this name; picking one would be a coin flip"
    assert "ambiguous: 2" in result.reason


def test_an_artist_with_no_hits_says_so() -> None:
    assert "0 search hits" in match_artist("Nobody At All", []).reason


def test_an_empty_artist_name_is_never_searched_into_a_match() -> None:
    assert not match_artist("   ", [artist("sp-1", "")]).matched


# --------------------------------------------------------------------------- albums


def test_an_exact_album_matches_and_keeps_its_step() -> None:
    result = match_album("Radiohead", "In Rainbows", [album("sp-a", "In Rainbows", "Radiohead")], step="album:upc")
    assert result.spotify_id == "sp-a"
    assert result.step == "album:upc", "the plan has to say whether a UPC or a title made the match"


def test_a_bracketed_edition_suffix_still_matches_the_album() -> None:
    result = match_album(
        "Radiohead", "In Rainbows", [album("sp-a", "In Rainbows (Deluxe Edition)", "Radiohead")], step="album:name"
    )
    assert result.spotify_id == "sp-a"


def test_the_plain_edition_wins_over_a_deluxe_one() -> None:
    hits = [
        album("sp-deluxe", "In Rainbows (Deluxe Edition)", "Radiohead"),
        album("sp-plain", "In Rainbows", "Radiohead"),
    ]
    result = match_album("Radiohead", "In Rainbows", hits, step="album:name")
    assert result.spotify_id == "sp-plain"
    assert result.step == "album:name:literal"


def test_two_editions_and_no_plain_one_is_ambiguous_not_a_coin_flip() -> None:
    hits = [
        album("sp-deluxe", "In Rainbows (Deluxe Edition)", "Radiohead"),
        album("sp-expanded", "In Rainbows (Expanded)", "Radiohead"),
    ]
    result = match_album("Radiohead", "In Rainbows", hits, step="album:name")
    assert not result.matched
    assert "ambiguous: 2" in result.reason


def test_a_deliberate_near_miss_is_unmatched() -> None:
    """'Ghosts' is not 'Ghosts I-IV'. There is no prefix rule and no edit distance on purpose."""
    hits = [
        album("sp-1", "Ghosts I-IV", "Nine Inch Nails"),
        album("sp-2", "Ghosts V: Together", "Nine Inch Nails"),
    ]
    result = match_album("Nine Inch Nails", "Ghosts", hits, step="album:name")
    assert not result.matched
    assert "no Spotify album titled 'Ghosts'" in result.reason
    assert "Ghosts I-IV" in result.reason, "the reason names what it saw, so a human can judge it"


def test_the_right_title_by_the_wrong_artist_is_refused() -> None:
    """The artist gate runs first: a covers act's identical title must never win."""
    result = match_album("Radiohead", "In Rainbows", [album("sp-x", "In Rainbows", "Karaoke Allstars")], step="s")
    assert not result.matched
    assert "credited to 'Radiohead'" in result.reason


def test_a_upc_hit_for_a_stranger_is_still_refused() -> None:
    """A barcode typo in MusicBrainz must not be able to save someone else's record."""
    result = match_album("Radiohead", "In Rainbows", [album("sp-x", "Greatest Hits", "Someone Else")], step="album:upc")
    assert not result.matched


def test_a_featured_credit_on_the_album_still_matches_the_primary_artist() -> None:
    hits = [album("sp-a", "Watch the Throne", "JAY-Z", "Kanye West")]
    assert match_album("JAY-Z", "Watch the Throne", hits, step="album:name").spotify_id == "sp-a"


def test_no_hits_at_all_says_so() -> None:
    result = match_album("Radiohead", "In Rainbows", [], step="album:name")
    assert not result.matched
    assert "no Spotify album found" in result.reason


def test_an_empty_title_is_never_matched() -> None:
    assert not match_album("Radiohead", "", [album("sp-a", "", "Radiohead")], step="album:name").matched
