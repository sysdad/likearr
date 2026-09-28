"""`shell.setup_commands`: doctor and setup-profiles."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from likearr import __version__, build_info
from likearr.adapters.spotify import SpotifyAuth
from likearr.config import PLACEHOLDER_CONTACT, MusicBrainzConfig
from likearr.models import EXIT_ERROR, EXIT_OK, OwnedArtist, Profile, ReasonKind
from likearr.shell import setup_commands
from likearr.shell.context import Context
from tests.shell.commands_shared import ALBUM, QUOTA_BODY, FakeSpotify, followed_world, token_file_data, with_real_auth
from tests.shell.conftest import TAG_ID, CapturingSink, make_config, make_context
from tests.unit.fakes import lidarr_artist, owned, reason

# --------------------------------------------------------------------------- doctor


def test_doctor_names_the_version_and_commit(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_COMMIT", "abc1234")
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert f"PASS  version: likearr {__version__}, commit abc1234" in out


def test_doctor_names_the_commit_as_unknown_without_the_env_var(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIKEARR_COMMIT", raising=False)
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert f"PASS  version: likearr {__version__}, commit unknown" in out


def test_doctor_json_has_a_top_level_version_key(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIKEARR_COMMIT", "abc1234")
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["version"] == build_info()
    assert payload["version"] == f"{__version__} (abc1234)"


def test_doctor_warns_rather_than_crashing_without_a_token(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.auth = None
        ctx.spotify_error = "LIKEARR_SPOTIFY_CLIENT_ID is not set"
        code = setup_commands.doctor_command(ctx)

    out = capsys.readouterr().out
    assert code == EXIT_OK, "a missing token is a WARN, not a FAIL"
    assert "WARN  spotify:" in out
    assert "PASS  lidarr:" in out
    assert lidarr.writes() == [], "doctor writes nothing"


def test_doctor_warns_about_the_example_contact_placeholder(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load accepts the example's `you@example.com`, Doctor names it."""
    source, lookup, lidarr = followed_world()
    config = make_config(tmp_path, musicbrainz=MusicBrainzConfig(contact=PLACEHOLDER_CONTACT))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)
    out = capsys.readouterr().out

    assert code == EXIT_OK, "a placeholder contact is a WARN, not a FAIL"
    assert "WARN  musicbrainz contact: LIKEARR_MUSICBRAINZ_CONTACT is the old example's you@example.com" in out


def test_doctor_says_nothing_about_a_real_contact(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.doctor_command(ctx, no_spotify=True)

    assert "musicbrainz contact" not in capsys.readouterr().out


def test_doctor_names_the_browser_path_when_no_token_file_exists(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinct from `test_doctor_warns_rather_than_crashing_without_a_token` (Spotify not
    configured at all) - here Spotify *is* configured, but `likearr auth` was never run. A new
    user reads this before they know the CLI exists, so it names Settings, not just the CLI."""
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        with_real_auth(ctx, token=None)
        code = setup_commands.doctor_command(ctx)

    out = capsys.readouterr().out
    assert code == EXIT_OK, "a missing token is a WARN, not a FAIL"
    assert "WARN  spotify:" in out
    assert "no token file" in out
    assert "Connect Spotify in Settings" in out
    assert "likearr auth" in out


def test_doctor_fails_on_a_missing_quality_profile(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.quality_profiles = {"Lossless": 2}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "FAIL  quality profile" in out


def test_doctor_warns_when_the_metadata_profiles_are_missing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profiles = {}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "setup-profiles" in out


def test_doctor_fails_on_a_missing_root_folder(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.root_folder_paths = ["/elsewhere"]
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    assert code == EXIT_ERROR
    assert "FAIL  root folder" in capsys.readouterr().out


def _unset_library(tmp_path: Path, **unset: str):
    """A config as a first start leaves it: no root folder, no quality profile, or neither."""
    config = make_config(tmp_path)
    return make_config(tmp_path, lidarr=replace(config.lidarr, **unset))


def test_doctor_fails_while_the_root_folder_and_quality_profile_are_unset_and_lists_lidarrs_choices(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.root_folder_paths = ["/music", "/audiobooks"]
    config = _unset_library(tmp_path, root_folder="", quality_profile="")
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "FAIL  root folder: not set: no run plans or applies until it is." in out
    assert "(Lidarr has: /music, /audiobooks)" in out
    assert "FAIL  quality profile: not set" in out
    assert "(Lidarr has: Standard)" in out


# --------------------------------------------------------------------------- setup-profiles


def test_setup_profiles_dry_run_changes_nothing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profiles = {}
    lidarr.tags = {}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "would create metadata profile 'Lean'" in out
    assert lidarr.writes() == []


def test_setup_profiles_apply_creates_everything(tmp_path: Path, sink: CapturingSink) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profiles = {}
    lidarr.tags = {}
    lidarr.root_folder_paths = []
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=True)

    assert code == EXIT_OK
    assert set(lidarr.metadata_profiles) == {"Lean", "Full"}
    assert "likearr" in lidarr.tags


def test_setup_profiles_json_preview_nothing_set_up(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profiles = {}
    lidarr.tags = {}
    lidarr.root_folder_paths = []
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["needs_apply"] is True
    assert {p["name"]: p["status"] for p in payload["profiles"]} == {"Lean": "missing", "Full": "missing"}
    assert payload["tag"] == {"name": "likearr", "status": "missing", "applies": True}
    assert payload["root_folder"]["status"] == "missing"
    assert payload["root_folder"]["applies"] is True
    assert lidarr.writes() == []


def test_setup_profiles_json_preview_fully_set_up_offers_nothing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()  # the fixture's defaults already have everything
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["needs_apply"] is False
    assert payload["todo"] == []
    assert {p["name"]: p["status"] for p in payload["profiles"]} == {"Lean": "ok", "Full": "ok"}


def test_setup_profiles_json_preview_partially_set_up(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.tags = {}  # only the tag is missing
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["needs_apply"] is True
    assert payload["todo"] == ["create tag 'likearr'"]
    assert {p["name"]: p["status"] for p in payload["profiles"]} == {"Lean": "ok", "Full": "ok"}


def test_setup_profiles_a_differing_profile_is_kept_and_reported_not_applied(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing "Lean" profile whose allowed types differ from what likearr would create is
    surfaced in the preview, but `--apply` never edits it - `ensure_metadata_profile` only ever
    reuses an existing name's id - so it is not in `todo` and applying does not touch it."""
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profile_details_override["Lean"] = {
        "primaryAlbumTypes": [{"albumType": {"name": "Album"}, "allowed": True}],
        "secondaryAlbumTypes": [
            {"albumType": {"name": "Studio"}, "allowed": True},
            {"albumType": {"name": "Live"}, "allowed": True},
        ],
    }
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    lean = next(p for p in payload["profiles"] if p["name"] == "Lean")
    assert lean["status"] == "differs"
    assert lean["applies"] is False, "status and applies are independent: differs does not mean applies"
    assert lean["diff"]["expected_primary"] == ["Album", "EP"]
    assert lean["diff"]["actual_primary"] == ["Album"]
    assert "differs" not in payload["todo"]
    assert payload["needs_apply"] is False, "the tag and root folder are already set up by the fixture"
    assert lidarr.writes() == []

    # Applying (were there something else to apply) must never touch the differing profile.
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.setup_profiles_command(ctx, do_apply=True, as_json=True)
    assert "ensure_metadata_profile" not in lidarr.names()


def test_setup_profiles_a_differing_root_folder_is_reported_as_applying(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unlike a metadata profile, an existing root folder whose monitor defaults differ from
    "none"/"none" really is overwritten by `--apply` (`set_root_folder_defaults`), so its
    `status: "differs"` carries `applies: True` and it is named in `todo` - the opposite of a
    differing profile, which is kept as is. `status` alone cannot say which; `applies` can."""
    source, lookup, lidarr = followed_world()  # root_folder_paths defaults to ["/music"], already there
    lidarr.root_folder_defaults = ("all", "none")  # changed by hand since likearr set it up

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["root_folder"]["status"] == "differs"
    assert payload["root_folder"]["applies"] is True
    assert any("root folder" in item for item in payload["todo"])
    assert payload["needs_apply"] is True


def test_doctor_json_output(tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]) -> None:
    source, lookup, lidarr = followed_world()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_OK
    assert payload["summary"]["failed"] == 0
    assert any(c["name"] == "lidarr" and c["level"] == "PASS" for c in payload["checks"])


def test_doctor_json_output_reports_failures(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.quality_profiles = {"Lossless": 2}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == EXIT_ERROR
    assert payload["summary"]["failed"] >= 1
    assert any(c["level"] == "FAIL" and "quality profile" in c["name"] for c in payload["checks"])
    assert lidarr.root_folder_paths == ["/music"]


# --------------------------------------------------------------------------- doctor: duplicates


def test_doctor_fails_on_two_artists_sharing_a_name(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """One API call the shell already makes, and it would have caught the live defect that day."""
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("mbid-lawrence-old", id=9001, name="Lawrence"))
    lidarr.seed(lidarr_artist("mbid-lawrence-new", id=9002, name="Lawrence"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR, "a duplicate name breaks imports right now; that is a FAIL, not a WARN"
    assert "FAIL  duplicate artists" in out
    assert "'Lawrence' exists 2 times" in out
    assert "id 9001 (mbid-lawrence-old)" in out and "id 9002 (mbid-lawrence-new)" in out
    assert lidarr.writes() == [], "doctor writes nothing"


def test_doctor_passes_on_a_library_with_unique_names(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist"))
    lidarr.seed(lidarr_artist("artist-2", id=2, name="Someone Else"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.doctor_command(ctx, no_spotify=True)

    assert "PASS  duplicate artists: 2 artists, every name unique" in capsys.readouterr().out


def test_doctor_folds_case_and_punctuation_before_comparing(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("mbid-a", id=1, name="Evangeline"))
    lidarr.seed(lidarr_artist("mbid-b", id=2, name="evangeline!"))

    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        assert setup_commands.doctor_command(ctx, no_spotify=True) == EXIT_ERROR

    assert "exists 2 times" in capsys.readouterr().out


# --------------------------------------------------------------------------- doctor: unmonitored artists


def test_doctor_fails_on_an_unmonitored_artist_holding_a_monitored_release(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lidarr never searches or lists as wanted an album whose artist is unmonitored."""
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist", monitored=False))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored([owned(ALBUM, reason(ReasonKind.SAVED, "al1"))[1]])
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "FAIL  unmonitored artists" in out
    assert "'Test Artist'" in out
    assert lidarr.writes() == [], "doctor writes nothing"


def test_doctor_passes_when_every_artist_holding_a_monitored_release_is_monitored(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_monitored([owned(ALBUM, reason(ReasonKind.SAVED, "al1"))[1]])
        setup_commands.doctor_command(ctx, no_spotify=True)

    assert "PASS  unmonitored artists" in capsys.readouterr().out


# --------------------------------------------------------------------------- doctor: lost state


def test_doctor_fails_when_lidarr_has_tagged_artists_and_the_state_database_has_none(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh state database beside a Lidarr likearr has already tagged artists in: lost state."""
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-9", id=9, name="Wet Leg", tags=[TAG_ID]))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)
        owned_artists = ctx.state.owned_artists()

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert (
        "FAIL  state matches lidarr: 1 artist(s) in Lidarr carry the 'likearr' tag, but the state database "
        "records no artist at all: it was lost or replaced. Restore it from backup (docs/DEPLOY.md, "
        "Backup and restore); until you do, nothing likearr monitored before is ever unmonitored. "
        "Wet Leg (artist-9)"
    ) in out
    assert lidarr.writes() == [] and owned_artists == {}, "doctor writes nothing and claims nothing"


def test_doctor_warns_about_a_tagged_artist_with_no_row_beside_artists_that_have_one(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Other rows exist, so this may be a tag someone added by hand: a WARN, not a FAIL."""
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist", tags=[TAG_ID]))
    lidarr.seed(lidarr_artist("artist-9", id=9, name="Wet Leg", tags=[TAG_ID]))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_artist(OwnedArtist("artist-1", 1, added_by_us=True, profile=Profile.LEAN))
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_OK, "a WARN is not a failure"
    assert (
        "WARN  state matches lidarr: 1 artist(s) in Lidarr carry the 'likearr' tag with no record in the "
        "state database: Wet Leg (artist-9). If you lost or replaced the database, restore it from backup; "
        "if you added the tag by hand, remove it from them"
    ) in out


def test_doctor_passes_when_every_tagged_artist_has_a_row(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-1", id=1, name="Test Artist", tags=[TAG_ID]))
    lidarr.seed(lidarr_artist("artist-2", id=2, name="Untagged"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        ctx.state.record_artist(OwnedArtist("artist-1", 1, added_by_us=True, profile=Profile.LEAN))
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "PASS  state matches lidarr: every artist tagged 'likearr' has a record in the state database" in out


def test_doctor_passes_on_a_fresh_install_with_no_tagged_artists(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.seed(lidarr_artist("artist-2", id=2, name="Untagged"))
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = setup_commands.doctor_command(ctx, no_spotify=True)

    assert code == EXIT_OK
    assert "PASS  state matches lidarr" in capsys.readouterr().out


def doctor_against(
    ctx: Context, spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch, *, playlists: tuple[str, ...] = ()
) -> int:
    """Run the whole of `doctor` with `ctx.auth` and every Spotify page served by `spotify`."""
    monkeypatch.setenv("LIKEARR_SPOTIFY_CLIENT_ID", "fake-client-id")
    object.__setattr__(ctx.config.spotify, "playlists", playlists)
    ctx.config.spotify.token_file.write_text(json.dumps(token_file_data()))
    transport = httpx.MockTransport(spotify.handler)
    ctx.auth = SpotifyAuth(ctx.config.spotify, httpx.Client(transport=transport), sleep=lambda _s: None)
    monkeypatch.setattr(setup_commands, "build_client", lambda: httpx.Client(transport=transport))
    return setup_commands.doctor_command(ctx)


def test_doctor_names_the_quota_and_stops_asking_spotify(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real failure: a playlist page answered QUOTA_EXCEEDED and doctor crashed with
    "unexpected error: HttpError". It is one FAIL now, and nothing after it is requested."""
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify({"/me/albums": httpx.Response(429, headers={"Retry-After": "3600"}, json=QUOTA_BODY)})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch, playlists=("pl-1",))

    out = capsys.readouterr().out
    assert code == EXIT_ERROR, "the normal doctor failure, not a crash"
    assert "unexpected error" not in out
    assert "PASS  spotify followed artists" in out, "the checks before the quota ran as usual"
    [line] = [x for x in out.splitlines() if x.startswith("FAIL  spotify quota:")]
    assert "saved albums" in line
    assert "QUOTA_EXCEEDED" in line
    assert "Retry-After: 3600 s (about 60 min)" in line
    assert "zero unmonitors" in line
    assert "SKIP  spotify liked tracks: not requested" in out
    assert "SKIP  spotify playlist pl-1: not requested" in out
    assert spotify.api_calls() == ["/v1/me/following", "/v1/me/albums"], "one quota answer, never retried"
    assert "1 failed" in out and "2 skipped" in out
    assert lidarr.writes() == [], "doctor writes nothing"


def test_doctor_says_when_spotify_sent_no_retry_after(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify({"/me/following": httpx.Response(429, json=QUOTA_BODY)})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "Spotify sent no Retry-After" in out
    assert spotify.api_calls() == ["/v1/me/following"]
    assert out.count("SKIP  spotify") == 2


def test_doctor_skips_every_page_when_the_token_refresh_meets_the_quota(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify({"/api/token": httpx.Response(429, headers={"Retry-After": "60"}, json=QUOTA_BODY)})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "FAIL  spotify quota: Spotify answered the token refresh request" in out
    assert "Retry-After: 60 s" in out
    assert out.count("SKIP  spotify") == 3
    assert spotify.api_calls() == [], "no page is asked for after the quota answer"
    assert len(spotify.requests) == 1, "the quota answer is not retried"


def test_doctor_promises_no_skips_when_the_quota_hits_the_last_page(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify({"/me/tracks": httpx.Response(429, json=QUOTA_BODY)})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "FAIL  spotify quota:" in out
    assert "remaining Spotify checks" not in out and "SKIP" not in out


def test_doctor_reports_a_failed_spotify_page_and_keeps_checking(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other HTTP failure on a canary is that canary's FAIL, never an escaped `HttpError`."""
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify({"/playlists/pl-gone": httpx.Response(404, json={"error": {"status": 404}})})
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch, playlists=("pl-gone", "pl-ok"))

    out = capsys.readouterr().out
    assert code == EXIT_ERROR
    assert "unexpected error" not in out
    assert "FAIL  spotify playlist pl-gone:" in out and "HTTP 404" in out
    assert "PASS  spotify playlist pl-ok: 5 total" in out, "one bad page does not stop the others"
    assert "SKIP" not in out


def test_doctor_passes_every_spotify_page_that_answers(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lookup, lidarr = followed_world()
    spotify = FakeSpotify()
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        code = doctor_against(ctx, spotify, monkeypatch)

    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "PASS  spotify token" in out
    assert "PASS  spotify followed artists: 3 total" in out
    assert "skipped" not in out, "the summary only counts skips when there are some"


def test_setup_profiles_json_lists_lidarrs_root_folders_and_quality_profiles(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settings picks `[lidarr] root_folder` and `quality_profile` from these lists."""
    source, lookup, lidarr = followed_world()
    lidarr.root_folder_paths = ["/music", "/audiobooks"]
    lidarr.quality_profiles = {"Standard": 1, "Lossless": 2}
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink) as ctx:
        setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["root_folders"] == ["/music", "/audiobooks"]
    assert payload["quality_profiles"] == ["Lossless", "Standard"]


def test_setup_profiles_with_no_root_folder_chosen_plans_and_applies_nothing_for_one(
    tmp_path: Path, sink: CapturingSink, capsys: pytest.CaptureFixture[str]
) -> None:
    source, lookup, lidarr = followed_world()
    lidarr.metadata_profiles = {}
    config = _unset_library(tmp_path, root_folder="")
    with make_context(tmp_path, source=source, lookup=lookup, lidarr=lidarr, sink=sink, config=config) as ctx:
        setup_commands.setup_profiles_command(ctx, do_apply=False, as_json=True)
        payload = json.loads(capsys.readouterr().out.strip())
        code = setup_commands.setup_profiles_command(ctx, do_apply=True)

    assert payload["root_folder"] == {"path": "", "status": "unset", "applies": False}
    assert not any("root folder" in item for item in payload["todo"])
    assert code == EXIT_OK
    assert set(lidarr.metadata_profiles) == {"Lean", "Full"}
    assert not [w for w in lidarr.writes() if "root_folder" in w]
