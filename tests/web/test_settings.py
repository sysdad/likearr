"""Settings editing: an allowlist, a comment-preserving round trip, validation, backups, atomic writes."""

from __future__ import annotations

import os
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from likearr.config import load_config, parse_config
from likearr.web.settings import (
    BACKUP_KEEP,
    FIELDS,
    Change,
    SaveConflict,
    _confirmations,
    current_values,
    describe_changes,
    file_hash,
    parse_form,
    plan_cleanup,
    plan_pause,
    plan_resume,
    plan_save,
    preview_schedule,
    write_config,
)

CONFIG = """\
# likearr configuration - the comments must survive a browser save.

[lidarr]
root_folder = "/music"
quality_profile = "Standard"

[spotify]
token_file = "spotify-token.json"
# Playlists I own.
playlists = ["pl1", "pl2"]
liked_tracks = true

[state]
db = "state.sqlite"

[rules]
# Keep this at album.
liked_track_scope = "album"
singles_fallback_days = 180  # half a year
"""

MBID = "0f0f0f0f-1111-2222-3333-444444444444"
UTC_NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def load_config_from_text(text: str, base_dir: Path):
    return parse_config(tomllib.loads(text), base_dir=base_dir)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


def _form(config_path: Path, **changes: object) -> dict[str, list[str]]:
    """The form a browser would post for the current values, with `changes` applied."""
    values = current_values(load_config(config_path))
    values.update({tuple(k.split("__")): v for k, v in changes.items()})  # type: ignore[misc]
    form: dict[str, list[str]] = {}
    for f in FIELDS:
        value = values[(f.section, f.key)]
        name = f.name
        if f.kind == "bool":
            form[name] = ["on"] if value else []
        elif f.kind == "list" and f.key == "playlists":
            form[name] = list(value)  # type: ignore[arg-type]
        elif f.kind == "list":
            form[name] = ["\n".join(value)]  # type: ignore[arg-type]
        else:
            form[name] = [str(value)]
    return form


# ---------------------------------------------------------------- the allowlist


def test_the_allowlist_is_exactly_the_approved_keys() -> None:
    assert {(f.section, f.key) for f in FIELDS} == {
        ("rules", "liked_track_scope"),
        ("rules", "singles_fallback_days"),
        ("rules", "allow_compilation_fallback"),
        ("rules", "allow_remix_releases"),
        ("rules", "keep_remix_only_tracks"),
        ("rules", "deny_releases"),
        ("rules", "recent_release_days"),
        ("rules", "albums_only_tag"),
        ("guards", "max_unmonitors_scheduled"),
        ("guards", "source_shrink_pct"),
        ("guards", "artist_shrink_pct"),
        ("guards", "unmapped_ratio_amber"),
        ("guards", "projected_wanted_max"),
        ("spotify", "playlists"),
        ("spotify", "followed_artists"),
        ("spotify", "saved_albums"),
        ("spotify", "liked_tracks"),
    }


def test_every_guard_and_rule_says_what_it_does() -> None:
    """Only a field whose label already says it all goes without help: the sources, and the
    liked-song scope, whose options are sentences."""
    self_explanatory = {
        "spotify.followed_artists",
        "spotify.saved_albums",
        "spotify.liked_tracks",
        "rules.liked_track_scope",
    }
    for f in FIELDS:
        assert bool(f.help.strip()) is (f.name not in self_explanatory), f.name


def test_liked_track_scope_choice_labels_are_sentences_but_the_values_stay_the_stored_ones() -> None:
    """The dropdown shows a sentence per option, but the posted/stored value is unchanged."""
    scope = next(f for f in FIELDS if (f.section, f.key) == ("rules", "liked_track_scope"))

    assert scope.choices == ("album", "smallest")
    assert scope.choice_labels["album"] != "album"
    assert scope.choice_labels["smallest"] != "smallest"
    assert "album" in scope.choice_labels["album"].lower()


def test_the_reworded_help_no_longer_uses_the_flagged_jargon() -> None:
    """The reworded help drops "Release group MBIDs" and "catalogue gap this new" jargon."""
    deny = next(f for f in FIELDS if (f.section, f.key) == ("rules", "deny_releases"))
    recent = next(f for f in FIELDS if (f.section, f.key) == ("rules", "recent_release_days"))

    assert "Release group MBIDs" not in deny.help
    assert "catalogue gap this new" not in recent.help


def test_current_values_include_defaults_for_keys_the_file_leaves_out(config_path: Path) -> None:
    values = current_values(load_config(config_path))

    assert values[("spotify", "playlists")] == ("pl1", "pl2")
    assert values[("guards", "max_unmonitors_scheduled")] == 100
    assert values[("rules", "deny_releases")] == ()
    assert len(values) == len(FIELDS)


def test_a_key_outside_the_allowlist_in_the_form_is_ignored(config_path: Path) -> None:
    form = _form(config_path)
    form["lidarr.url"] = ["http://evil.example"]
    form["state.db"] = ["/tmp/elsewhere.sqlite"]

    values, errors = parse_form(form)
    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert not errors
    assert check.changes == []
    assert check.new_text == config_path.read_text()


# ---------------------------------------------------------------- the round trip


def test_an_untouched_form_changes_nothing(config_path: Path) -> None:
    values, errors = parse_form(_form(config_path))
    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert errors == {}
    assert check.changes == []
    assert check.new_text == config_path.read_text()


def test_a_change_keeps_every_comment_and_pins_no_defaults(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, rules__singles_fallback_days=90))
    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.errors == {}
    assert [c.key for c in check.changes] == ["singles_fallback_days"]
    assert "# likearr configuration - the comments must survive a browser save." in check.new_text
    assert "# Keep this at album." in check.new_text
    assert "singles_fallback_days = 90" in check.new_text
    assert "max_unmonitors_scheduled" not in check.new_text  # the untouched default stays implicit


def test_a_change_to_a_section_the_file_lacks_adds_it(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, guards__max_unmonitors_scheduled=50))
    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.errors == {}
    assert "[guards]" in check.new_text
    assert "max_unmonitors_scheduled = 50" in check.new_text


def test_unchecking_a_playlist_removes_it(config_path: Path) -> None:
    form = _form(config_path)
    form["spotify.playlists"] = ["pl2"]
    values, _ = parse_form(form)

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert "# Playlists I own." in check.new_text
    assert 'playlists = ["pl2"]' in check.new_text


# ---------------------------------------------------------------- validation


def test_a_non_number_is_a_field_error_and_nothing_is_planned(config_path: Path) -> None:
    form = _form(config_path)
    form["rules.singles_fallback_days"] = ["six months"]

    _, errors = parse_form(form)

    assert "rules.singles_fallback_days" in errors


def test_a_mistyped_mbid_is_refused_by_the_real_config_validation(config_path: Path) -> None:
    form = _form(config_path)
    form["rules.deny_releases"] = [f"{MBID}\nnot-an-mbid"]
    values, errors = parse_form(form)
    assert not errors

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert "rules.deny_releases" in check.errors
    assert "not-an-mbid" in check.errors["rules.deny_releases"]


def test_an_unknown_scope_is_refused(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, rules__liked_track_scope="everything"))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert "rules.liked_track_scope" in check.errors


# ---------------------------------------------------------------- second confirms


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("rules__liked_track_scope", "smallest"),
        ("rules__allow_compilation_fallback", False),
        ("rules__allow_remix_releases", False),
        ("rules__keep_remix_only_tracks", False),
        ("rules__deny_releases", (MBID,)),
    ],
)
def test_a_change_that_re_resolves_needs_a_second_confirm(config_path: Path, key: str, value: object) -> None:
    values, _ = parse_form(_form(config_path, **{key: value}))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.errors == {}
    assert len(check.confirm) == 1
    assert "resolve" in check.confirm[0]
    assert "Review changes before" in check.confirm[0]


@pytest.mark.parametrize(
    ("key", "looser"),
    [
        ("guards__max_unmonitors_scheduled", 500),
        ("guards__source_shrink_pct", 50.0),
        ("guards__artist_shrink_pct", 90.0),
        ("guards__unmapped_ratio_amber", 0.5),
        ("guards__projected_wanted_max", 9000),
    ],
)
def test_loosening_a_guard_needs_a_second_confirm_naming_it(config_path: Path, key: str, looser: object) -> None:
    values, _ = parse_form(_form(config_path, **{key: looser}))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert len(check.confirm) == 1
    assert key.split("__")[1] in check.confirm[0]


def test_loosening_unmapped_ratio_amber_says_status_not_amber(config_path: Path) -> None:
    """The confirm text talks about Status needing attention, not Home Assistant's
    amber state. The config key itself (`unmapped_ratio_amber`) is unchanged - see the field
    label and `_GUARD_EFFECT` in `settings.py`."""
    field = next(f for f in FIELDS if (f.section, f.key) == ("guards", "unmapped_ratio_amber"))
    assert "amber" not in field.label.lower()
    assert "Status" in field.label

    values, _ = parse_form(_form(config_path, guards__unmapped_ratio_amber=0.5))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    (reason,) = check.confirm
    assert "turning amber" not in reason
    assert "Status shows needs attention" in reason


def test_tightening_a_guard_saves_in_one_step(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, guards__max_unmonitors_scheduled=10))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.errors == {}
    assert check.confirm == []


def test_an_ordinary_change_saves_in_one_step(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, spotify__liked_tracks=False))

    assert plan_save(config_path.read_text(), values, base_dir=config_path.parent).confirm == []


# ---------------------------------------------------------------- writing


def test_writing_backs_up_the_old_file_and_replaces_it_atomically(config_path: Path) -> None:
    os.chmod(config_path, 0o640)
    before = config_path.read_bytes()

    backup = write_config(
        config_path, "new = 1\n", expected_hash=file_hash(before), now=datetime(2026, 9, 23, 14, 3, 11, tzinfo=UTC)
    )

    assert backup.name == "config.toml.bak-20260923-140311"
    assert backup.read_bytes() == before
    assert config_path.read_text() == "new = 1\n"
    assert os.stat(config_path).st_mode & 0o777 == 0o640
    assert not [p for p in config_path.parent.iterdir() if p.name.startswith(".config.toml")]


@pytest.mark.parametrize(
    ("before", "after"),
    [(0o644, 0o640), (0o666, 0o660), (0o664, 0o660), (0o640, 0o640), (0o600, 0o600), (0o660, 0o660)],
)
def test_a_save_drops_world_bits_from_the_config_and_its_backup(config_path: Path, before: int, after: int) -> None:
    """config.toml can hold a capability URL (a webhook with a token), so a save never leaves it or
    its backup readable by other users. Group bits stay: a host user editing by hand may be in the
    container's group. Not a hard 0600."""
    os.chmod(config_path, before)
    backup = write_config(
        config_path,
        "new = 1\n",
        expected_hash=file_hash(config_path.read_bytes()),
        now=datetime(2026, 9, 23, 14, 3, 11, tzinfo=UTC),
    )

    assert os.stat(config_path).st_mode & 0o777 == after
    assert os.stat(backup).st_mode & 0o777 == after


def test_a_backup_keeps_the_old_file_s_modification_time(config_path: Path) -> None:
    os.utime(config_path, (1_700_000_000, 1_700_000_000))
    backup = write_config(
        config_path,
        "new = 1\n",
        expected_hash=file_hash(config_path.read_bytes()),
        now=datetime(2026, 9, 23, tzinfo=UTC),
    )
    assert os.stat(backup).st_mtime == 1_700_000_000


def test_a_backup_never_follows_a_symlink_planted_at_its_name(config_path: Path, tmp_path: Path) -> None:
    """The name is taken with O_EXCL, so a file or link already there (even a dangling one) is
    skipped, never written through."""
    outside = tmp_path / "outside.txt"
    planted = config_path.with_name("config.toml.bak-20260923-140311")
    planted.symlink_to(outside)

    backup = write_config(
        config_path,
        "new = 1\n",
        expected_hash=file_hash(config_path.read_bytes()),
        now=datetime(2026, 9, 23, 14, 3, 11, tzinfo=UTC),
    )

    assert backup.name == "config.toml.bak-20260923-140311-2"
    assert not outside.exists()
    assert backup.read_text() == CONFIG


def test_writing_refuses_when_the_file_changed_since_the_form_was_rendered(config_path: Path) -> None:
    rendered = file_hash(config_path.read_bytes())
    config_path.write_text(CONFIG + "\n# a hand edit\n")

    with pytest.raises(SaveConflict):
        write_config(config_path, "new = 1\n", expected_hash=rendered, now=datetime(2026, 9, 23, tzinfo=UTC))

    assert config_path.read_text().endswith("# a hand edit\n")
    assert not [p for p in config_path.parent.iterdir() if ".bak-" in p.name]


def test_only_the_newest_ui_backups_are_kept_and_hand_made_ones_never_pruned(config_path: Path) -> None:
    folder = config_path.parent
    for day in range(1, BACKUP_KEEP + 6):
        (folder / f"config.toml.bak-202608{day:02d}-000000").write_text("old")
    (folder / "config.toml.bak-20250101-before-upgrade").write_text("hand made")
    (folder / "config.toml.bak-20260801").write_text("hand made, no time")

    write_config(
        config_path,
        "new = 1\n",
        expected_hash=file_hash(config_path.read_bytes()),
        now=datetime(2026, 9, 23, 14, 3, 11, tzinfo=UTC),
    )

    ui_backups = sorted(p.name for p in folder.iterdir() if re.fullmatch(r"config\.toml\.bak-\d{8}-\d{6}", p.name))
    assert len(ui_backups) == BACKUP_KEEP
    assert "config.toml.bak-20260923-140311" in ui_backups
    assert "config.toml.bak-20260801-000000" not in ui_backups
    assert (folder / "config.toml.bak-20250101-before-upgrade").exists()
    assert (folder / "config.toml.bak-20260801").exists()


def test_two_saves_in_one_second_do_not_overwrite_a_backup(config_path: Path) -> None:
    when = datetime(2026, 9, 23, 14, 3, 11, tzinfo=UTC)
    first = write_config(config_path, "a = 1\n", expected_hash=file_hash(config_path.read_bytes()), now=when)
    second = write_config(config_path, "a = 2\n", expected_hash=file_hash(config_path.read_bytes()), now=when)

    assert first != second
    assert first.read_text() == CONFIG
    assert second.read_text() == "a = 1\n"


# ---------------------------------------------------------------- lists: order and comments (review)

ANNOTATED = CONFIG.replace(
    'playlists = ["pl1", "pl2"]',
    'playlists = [\n    "pl1",  # road trip\n    "pl2",  # gym\n]',
) + (
    "deny_releases = [\n"
    '    "0f0f0f0f-1111-2222-3333-444444444444",  # the 40-disc box set\n'
    '    "1a1a1a1a-1111-2222-3333-444444444444",  # a remix EP\n'
    "]\n"
)


def test_the_same_playlists_in_another_order_are_no_change(config_path: Path) -> None:
    config_path.write_text(ANNOTATED)
    form = _form(config_path)
    form["spotify.playlists"] = ["pl2", "pl1"]  # the picker lists them by name, not in file order
    values, _ = parse_form(form)

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.changes == []
    assert check.new_text == ANNOTATED


def test_removing_a_playlist_keeps_the_comments_on_the_others(config_path: Path) -> None:
    config_path.write_text(ANNOTATED)
    form = _form(config_path)
    form["spotify.playlists"] = ["pl2"]
    values, _ = parse_form(form)

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert [c.key for c in check.changes] == ["playlists"]
    assert "# gym" in check.new_text
    assert "# road trip" not in check.new_text
    assert "pl1" not in check.new_text


def test_adding_a_refused_release_keeps_every_annotation_and_the_file_order(config_path: Path) -> None:
    config_path.write_text(ANNOTATED)
    form = _form(config_path)
    new = "2b2b2b2b-1111-2222-3333-444444444444"
    form["rules.deny_releases"] = [form["rules.deny_releases"][0] + "\n" + new.upper()]
    values, _ = parse_form(form)

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    assert check.errors == {}
    assert "# the 40-disc box set" in check.new_text
    assert "# a remix EP" in check.new_text
    text = check.new_text
    assert text.index("0f0f0f0f") < text.index("1a1a1a1a") < text.index("2b2b2b2b")


# ---------------------------------------------------------------- numbers must be finite (review)


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "1e999"])
def test_a_guard_must_be_a_finite_number(config_path: Path, value: str) -> None:
    form = _form(config_path)
    form["guards.unmapped_ratio_amber"] = [value]

    _, errors = parse_form(form)

    assert "guards.unmapped_ratio_amber" in errors


# ---------------------------------------------------------------- the confirm text is accurate (review)


def test_refusing_a_release_says_what_actually_re_resolves(config_path: Path) -> None:
    values, _ = parse_form(_form(config_path, rules__deny_releases=(MBID,)))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    (reason,) = check.confirm
    assert "every liked" not in reason
    assert "landed on" in reason
    assert "Review changes before" in reason


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("guards.max_unmonitors_scheduled", "9" * 400),
        ("guards.source_shrink_pct", "1e300"),
        ("rules.recent_release_days", "-" + "9" * 40),
    ],
)
def test_a_number_out_of_any_sane_range_is_a_field_error(config_path: Path, name: str, value: str) -> None:
    form = _form(config_path)
    form[name] = [value]

    _, errors = parse_form(form)

    assert "out of range" in errors[name]


# ---------------------------------------------------------------- switching a source on

SOURCE_WARNING = "with no cap. Review changes first"


@pytest.mark.parametrize("key", ["spotify__followed_artists", "spotify__saved_albums", "spotify__liked_tracks"])
def test_switching_a_source_on_needs_a_second_confirm(config_path: Path, key: str) -> None:
    config_path.write_text(
        CONFIG.replace("liked_tracks = true", "liked_tracks = false\nfollowed_artists = false\nsaved_albums = false")
    )
    values, _ = parse_form(_form(config_path, **{key: True}))

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    (reason,) = check.confirm
    assert key.split("__")[1] in reason
    assert SOURCE_WARNING in reason


def test_adding_a_playlist_needs_a_second_confirm(config_path: Path) -> None:
    form = _form(config_path)
    form["spotify.playlists"] = ["pl1", "pl2", "pl3"]
    values, _ = parse_form(form)

    check = plan_save(config_path.read_text(), values, base_dir=config_path.parent)

    (reason,) = check.confirm
    assert "pl3" in reason
    assert SOURCE_WARNING in reason


def test_switching_a_source_off_or_removing_a_playlist_saves_in_one_step(config_path: Path) -> None:
    form = _form(config_path, spotify__liked_tracks=False)
    form["spotify.playlists"] = ["pl1"]
    values, _ = parse_form(form)

    assert plan_save(config_path.read_text(), values, base_dir=config_path.parent).confirm == []


# ---------------------------------------------------------------- pause / resume


def test_pausing_saves_in_one_step_with_a_reason_and_the_time(config_path: Path) -> None:
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

    check = plan_pause(config_path.read_text(), "maintenance window", base_dir=config_path.parent, now=now)

    assert check.errors == {}
    assert check.confirm == []
    assert "enabled = false" in check.new_text
    assert "maintenance window" in check.new_text
    assert "2026-09-24" in check.new_text
    reloaded = load_config_from_text(check.new_text, config_path.parent)
    assert reloaded.schedule.enabled is False
    assert reloaded.schedule.paused_reason == "maintenance window"
    assert reloaded.schedule.paused_at == now


def test_pausing_with_no_reason_is_allowed(config_path: Path) -> None:
    check = plan_pause(config_path.read_text(), "", base_dir=config_path.parent, now=datetime(2026, 9, 24, tzinfo=UTC))

    assert check.errors == {}
    reloaded = load_config_from_text(check.new_text, config_path.parent)
    assert reloaded.schedule.paused_reason == ""


def test_pausing_collapses_whitespace_and_caps_the_reason_length(config_path: Path) -> None:
    check = plan_pause(
        config_path.read_text(), "line one\nline two   " + "x" * 300, base_dir=config_path.parent, now=UTC_NOW
    )

    reloaded = load_config_from_text(check.new_text, config_path.parent)
    assert "\n" not in reloaded.schedule.paused_reason
    assert len(reloaded.schedule.paused_reason) <= 200


def test_resuming_always_needs_a_second_confirm(config_path: Path) -> None:
    paused = plan_pause(config_path.read_text(), "testing", base_dir=config_path.parent, now=UTC_NOW)

    check = plan_resume(paused.new_text, base_dir=config_path.parent)

    assert check.errors == {}
    assert check.confirm != []
    reloaded = load_config_from_text(check.new_text, config_path.parent)
    assert reloaded.schedule.enabled is True


def test_resuming_an_already_enabled_schedule_still_confirms(config_path: Path) -> None:
    """Not gated on whether it changed anything: resume is a deliberate act every time it's asked."""
    check = plan_resume(config_path.read_text(), base_dir=config_path.parent)

    assert check.confirm != []


# ---------------------------------------------------------------- live schedule preview


def test_preview_schedule_gives_the_next_fires_in_the_chosen_timezone() -> None:
    preview = preview_schedule("0 6 * * *", "America/New_York", now=UTC_NOW, count=3)

    assert preview.error == ""
    assert len(preview.fires) == 3
    assert all(f.tzname() in ("EDT", "EST") for f in preview.fires)
    assert list(preview.fires) == sorted(preview.fires)


def test_preview_schedule_reports_a_bad_cron_line_the_same_way_the_save_would() -> None:
    preview = preview_schedule("not a cron line", "UTC", now=UTC_NOW)

    assert preview.fires == ()
    assert "[schedule] cron" in preview.error
    assert "five fields" in preview.error


def test_preview_schedule_reports_a_line_that_fires_too_often() -> None:
    preview = preview_schedule("*/5 * * * *", "UTC", now=UTC_NOW)

    assert preview.fires == ()
    assert "can fire as often as every 5 minutes" in preview.error


def test_preview_schedule_reports_a_bad_timezone_the_same_way_the_save_would() -> None:
    preview = preview_schedule("0 6 * * *", "Mars/Olympus_Mons", now=UTC_NOW)

    assert preview.fires == ()
    assert "[schedule] timezone" in preview.error
    assert "is not an IANA timezone name" in preview.error


@pytest.mark.parametrize(
    ("cron", "summary"),
    [
        ("0 6 * * *", "Every day at 06:00"),
        ("15 */6 * * *", "Every 6 hours at :15"),
        ("0 9 * * 1-5", "Weekdays at 09:00"),
    ],
)
def test_preview_schedule_describes_common_shapes_in_plain_english(cron: str, summary: str) -> None:
    preview = preview_schedule(cron, "UTC", now=UTC_NOW)

    assert preview.summary == summary


@pytest.mark.parametrize("cron", ["*/17 * * * *", "0 6 1 * *", "0 6,18 * * 2"])
def test_preview_schedule_omits_the_summary_for_anything_else(cron: str) -> None:
    preview = preview_schedule(cron, "UTC", now=UTC_NOW)

    assert preview.summary == ""


# ---------------------------------------------------------------- readable confirm rows


def test_describe_changes_reads_a_bool_as_on_off() -> None:
    (row,) = describe_changes([Change("rules", "allow_remix_releases", True, False)], {})

    assert row.label == "Allow remix releases"
    assert row.summary == "on -> off"
    assert row.section == "rules"
    assert row.key == "allow_remix_releases"
    assert row.old_raw == "True"
    assert row.new_raw == "False"


def test_describe_changes_names_added_and_removed_playlists() -> None:
    change = Change("spotify", "playlists", ("pl1", "pl2"), ("pl2", "pl3"))
    names = {"pl1": "Road trip", "pl3": "New finds"}

    (row,) = describe_changes([change], names)

    assert row.label == "Playlists"
    assert row.summary == "added: New finds / removed: Road trip"
    assert row.old_raw == "pl1, pl2"
    assert row.new_raw == "pl2, pl3"


def test_describe_changes_falls_back_to_the_id_for_an_unnamed_playlist() -> None:
    change = Change("spotify", "playlists", (), ("pl9",))

    (row,) = describe_changes([change], {})

    assert row.summary == "added: pl9"


def test_describe_changes_reads_deny_releases_as_a_count_not_mbids() -> None:
    change = Change("rules", "deny_releases", (MBID,), (MBID, "1a1a1a1a-1111-2222-3333-444444444444"))

    (row,) = describe_changes([change], {})

    assert row.summary == "1 release added to the refused list"
    assert MBID not in row.summary
    assert MBID in row.new_raw  # the MBID is still there, for the collapsed <details>


def test_describe_changes_reads_deny_releases_additions_and_removals_together() -> None:
    change = Change(
        "rules",
        "deny_releases",
        (MBID, "1a1a1a1a-1111-2222-3333-444444444444"),
        ("2b2b2b2b-1111-2222-3333-444444444444",),
    )

    (row,) = describe_changes([change], {})

    assert row.summary == "1 release added to the refused list, 2 removed"


_OTHER_MBID = "1a1a1a1a-1111-2222-3333-444444444444"


def _deny_confirm(old: tuple[str, ...], new: tuple[str, ...]) -> str:
    (text,) = _confirmations([Change("rules", "deny_releases", old, new)], {})
    return text


def test_adding_a_deny_entry_says_the_songs_on_it_re_resolve() -> None:
    text = _deny_confirm((), (MBID,))

    assert "changes what the songs that landed on the release you added resolve to" in text
    assert "removed" not in text
    assert "Review changes before" in text


def test_removing_a_deny_entry_says_the_songs_kept_off_it_re_resolve() -> None:
    """Removing an entry now re-resolves what fell through from it, and says so."""
    text = _deny_confirm((MBID, _OTHER_MBID), ())

    assert "changes what the songs that were kept off the releases you removed resolve to" in text
    assert "landed on" not in text
    assert "Review changes before" in text


def test_a_deny_change_both_ways_names_both() -> None:
    text = _deny_confirm((MBID,), (_OTHER_MBID,))

    assert "landed on the release you added" in text
    assert "kept off the release you removed" in text


def test_describe_changes_labels_a_schedule_change_without_a_field() -> None:
    (row,) = describe_changes([Change("schedule", "cron", "0 6 * * *", "0 7 * * *")], {})

    assert row.label == "Schedule (cron)"
    assert row.summary == "0 6 * * * -> 0 7 * * *"


def test_describe_changes_reads_an_ordinary_value_as_old_arrow_new() -> None:
    (row,) = describe_changes([Change("rules", "singles_fallback_days", 180, 90)], {})

    assert row.label == "Singles fallback (days)"
    assert row.summary == "180 -> 90"


# ---------------------------------------------------------------- Clean up's switch


@pytest.mark.parametrize("prune", ['prune = "x"\n', '[[prune]]\nholding_dir = "/x"\n'])
def test_the_clean_up_switch_refuses_a_prune_that_is_not_a_table_rather_than_crashing(
    config_path: Path, prune: str
) -> None:
    text = prune + config_path.read_text()  # first: a bare key after a table header lands in that table
    assert load_config_from_text(text, config_path.parent).prune.errors  # loads: never fatal

    check = plan_cleanup(text, True, base_dir=config_path.parent)

    assert "prune.enabled" in check.errors
    assert check.new_text == text


def test_the_clean_up_switch_replaces_an_enabled_that_is_not_a_boolean_even_when_it_reads_as_off(
    config_path: Path,
) -> None:
    text = config_path.read_text() + '[prune]\nenabled = "yes"\n'

    check = plan_cleanup(text, False, base_dir=config_path.parent)

    assert check.errors == {}
    assert check.changes
    reloaded = load_config_from_text(check.new_text, config_path.parent)
    assert reloaded.prune.enabled is False
    assert reloaded.prune.errors == ()
