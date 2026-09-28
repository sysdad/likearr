"""`.github/workflows/image-publish.yml`: the image-publish workflow.

The merge gate for this workflow is a review confirming its only trigger is a `v*` tag push -
these tests pin that in code too, along with the other safety properties the ruling called out:
least-privilege permissions, no PAT, and the same Dockerfile/smoke test gating the push. No YAML
library is in the dev deps (see test_compose_example.py), so this checks the file's shape at the
text level, the same way the rest of this file's neighbours pin ci.yml and the Dockerfile.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "image-publish.yml"


def _text() -> str:
    return WORKFLOW_PATH.read_text()


def test_the_file_exists() -> None:
    assert WORKFLOW_PATH.is_file()


def test_the_only_top_level_trigger_is_a_v_tag_push() -> None:
    text = _text()
    # The `on:` block, up to the next top-level (unindented) key.
    match = re.search(r"^on:\n((?:[ \t].*\n?)*)", text, re.MULTILINE)
    assert match, "no on: block"
    on_block = match.group(1)
    assert "tags:" in on_block
    assert re.search(r'-\s*"v\*"', on_block)
    # None of the other triggers this workflow must never fire on.
    for forbidden in ("pull_request", "workflow_dispatch", "schedule", "branches"):
        assert forbidden not in on_block, forbidden


def test_only_one_job_and_it_is_the_publish_job() -> None:
    text = _text()
    match = re.search(r"^jobs:\n(.*)", text, re.DOTALL | re.MULTILINE)
    assert match, "no jobs: block"
    job_names = re.findall(r"^  (\w[\w-]*):\n", match.group(1), re.MULTILINE)
    assert job_names == ["publish"], job_names


def test_only_the_publish_job_can_write_packages() -> None:
    text = _text()
    # Top-level permissions: (apply to any job that doesn't override them) must not include
    # packages: write - only the publish job's own permissions: block may grant it.
    top_permissions = re.search(r"^permissions:\n((?:[ \t].*\n?)*)", text, re.MULTILINE)
    assert top_permissions, "no top-level permissions: block"
    assert "packages: write" not in top_permissions.group(1)
    assert "contents: read" in top_permissions.group(1)
    job_block = text.split("jobs:", 1)[1]
    # Only an actual `packages: write` mapping entry, not the word appearing in a comment.
    grants = re.findall(r"^\s*packages:\s*write\s*$", job_block, re.MULTILINE)
    assert len(grants) == 1, grants


def test_login_uses_the_github_token_not_a_pat() -> None:
    text = _text()
    assert "docker/login-action@" in text
    assert "secrets.GITHUB_TOKEN" in text
    # No other secret is read anywhere in the file.
    assert re.findall(r"secrets\.\w+", text) == ["secrets.GITHUB_TOKEN"]


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    text = _text()
    uses_lines = re.findall(r"uses:\s*(\S+)", text)
    assert uses_lines, "no uses: lines found"
    for uses in uses_lines:
        assert re.search(r"@[0-9a-f]{40}$", uses), uses


def test_the_smoke_test_runs_before_the_push_step() -> None:
    text = _text()
    smoke_idx = text.index("Smoke test")
    login_idx = text.index("Log in to GHCR")
    push_idx = text.index("Build and push multi-arch image")
    assert smoke_idx < login_idx < push_idx


def test_the_smoke_test_matches_cis_user_and_checks() -> None:
    # Same non-root uid/gid ruling and the same two commands ci.yml's build-only job runs.
    text = _text()
    assert "--user 1030:100" in text
    assert "likearr:publish-smoke --help" in text
    assert "likearr:publish-smoke doctor --help" in text


def test_the_smoke_test_includes_cis_root_owned_and_precompiled_check() -> None:
    # ci.yml's docker job check has to run here too: a published image skipping it would
    # mean the security hardening it verifies is never re-checked on the path that reaches GHCR.
    text = _text()
    assert "image code is root-owned and precompiled" in text
    assert "for user in likearr 1030:100; do" in text
    assert '--user "${user}" --entrypoint sh likearr:publish-smoke' in text
    assert "is not owned by root" in text
    assert "is not precompiled" in text
    assert "the working directory is on sys.path" in text


def test_the_published_image_is_owner_agnostic_and_lowercased() -> None:
    text = _text()
    assert "ghcr.io/${{ steps.owner.outputs.name }}/likearr" in text
    assert "tr '[:upper:]' '[:lower:]'" in text


def test_tags_include_the_semver_from_the_git_tag_and_latest() -> None:
    text = _text()
    assert "type=semver,pattern={{version}}" in text
    assert "type=raw,value=latest" in text
