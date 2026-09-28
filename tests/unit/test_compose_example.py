"""`deploy/compose.example.yaml`: the service split between the always-on `likearr` service and
the one-shot `likearr-cli` service, and the README quick start that pastes a minimal
single-service block derived from it.

No YAML library is in the dev deps, so this checks the file's shape at the text level: the
`likearr` service is always-on with no profile and no library mount, and `likearr-cli` is the
one-shot, "tools"-profiled service that carries the library-mount comment.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "compose.example.yaml"


def _text() -> str:
    return COMPOSE_PATH.read_text()


def test_the_compose_example_parses_as_yaml_if_a_yaml_library_is_available() -> None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return
    doc = yaml.safe_load(_text())
    services = doc["services"]
    assert set(services) == {"likearr", "likearr-cli"}
    assert "profiles" not in services["likearr"]
    assert services["likearr"]["restart"] == "unless-stopped"
    assert services["likearr-cli"]["profiles"] == ["tools"]


def test_likearr_service_is_always_on_with_no_profile() -> None:
    text = _text()
    likearr_block, _, rest = text.partition("\n  likearr-cli:")
    assert "  likearr:" in likearr_block
    assert "restart: unless-stopped" in likearr_block
    assert "profiles:" not in likearr_block
    # /data only: the library is never mounted on the always-on service.
    assert "/data/media" not in likearr_block
    assert rest  # the likearr-cli block exists past the split


def test_likearr_cli_is_the_one_shot_tools_service() -> None:
    text = _text()
    _, _, cli_block = text.partition("\n  likearr-cli:")
    assert 'profiles: ["tools"]' in cli_block
    assert 'restart: "no"' in cli_block
    # The optional library mount comment for prune-stage lives only here.
    assert "/data/media" in cli_block
    # The image's CMD is now the always-on service: a bare `compose run likearr-cli` must not
    # quietly start a second web server and scheduler inside a one-shot container.
    assert 'command: ["--help"]' in cli_block


def test_no_host_crontab_block_remains() -> None:
    text = _text()
    assert "* * * * *" not in text
    assert "/etc/cron.d/likearr" not in text
    assert "Settings page" in text


def test_no_reference_to_the_old_likearr_ui_service_name() -> None:
    assert "likearr-ui" not in _text()


# The README quick start has to run as written with no clone. It pastes a minimal
# single-service block as compose.yaml at the repo root, so every relative path in it resolves
# from there, same as the full deploy/compose.example.yaml this file otherwise checks.

REPO_ROOT = COMPOSE_PATH.parents[1]
ENV_EXAMPLE_PATH = REPO_ROOT / "deploy" / "env.example"
README_PATH = REPO_ROOT / "README.md"


def _service_blocks() -> dict[str, str]:
    likearr_block, _, cli_block = _text().partition("\n  likearr-cli:")
    return {"likearr": likearr_block, "likearr-cli": cli_block}


def _list_under(block: str, key: str) -> list[str]:
    """The `- item` lines under `key:` in one service block, in order."""
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{key}:")
    items: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("- "):
            break
        items.append(stripped[2:].strip())
    return items


def _env_file_name_from_example() -> str:
    match = re.search(r"^# Copy to (\S+)", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)
    assert match, "deploy/env.example no longer says which file to copy it to"
    return match.group(1)


def _quick_start_section() -> str:
    _, _, section = README_PATH.read_text().partition("\n## Quick start\n")
    section, _, _ = section.partition("\n## ")
    return section


def _quick_start_commands() -> list[str]:
    _, _, block = _quick_start_section().partition("```bash\n")
    block, _, _ = block.partition("```")
    return [line.strip() for line in block.splitlines() if line.strip()]


def _quick_start_yaml_block() -> str:
    _, _, block = _quick_start_section().partition("```yaml\n")
    block, _, _ = block.partition("```")
    return block


def test_the_readme_quick_start_service_agrees_with_compose_example() -> None:
    """The README's Quick start pastes its own minimal single-service block rather than the full
    deploy/compose.example.yaml; hand-maintained separately, the two must still agree
    on the image tag and the settings that protect a run in progress, or one has silently drifted."""
    block = _quick_start_yaml_block()
    assert block.strip(), "README.md's Quick start has no ```yaml block"
    likearr_block = _service_blocks()["likearr"]
    needles = (
        "./likearr-data:/data",
        "8770:8770",
        "mem_limit: 768m",
        "stop_grace_period: 30m",
        "restart: unless-stopped",
    )
    for needle in needles:
        assert needle in block, f"README Quick start block is missing {needle!r}"
        assert needle in likearr_block, f"deploy/compose.example.yaml's likearr service is missing {needle!r}"
    readme_image = next(line.strip() for line in block.splitlines() if line.strip().startswith("image:"))
    compose_image = next(line.strip() for line in likearr_block.splitlines() if line.strip().startswith("image:"))
    assert readme_image == compose_image


def test_each_service_defaults_to_a_pinned_published_image() -> None:
    """The default is `image:` (a version-pinned GHCR pull), not a build from source."""
    for name, block in _service_blocks().items():
        lines = [line.strip() for line in block.splitlines()]
        images = [line for line in lines if line.startswith("image:")]
        assert len(images) == 1, name
        image = images[0].removeprefix("image:").strip()
        assert image.startswith("ghcr.io/"), (name, image)
        tag = image.rsplit(":", 1)[-1]
        # Pinned to a real version, never floating on `:latest` - see "Upgrading and pinning" at
        # the bottom of the file.
        assert tag not in {"", "latest", image}, (name, image)
        # No active (uncommented) build: line - image: is the only thing docker compose reads.
        assert "build:" not in lines, name


def test_each_services_build_alternative_is_commented_but_still_points_at_the_dockerfile() -> None:
    """The `build: .` route is kept, commented out, for people who prefer it."""
    for name, block in _service_blocks().items():
        assert "# build:" in block, name
        match = re.search(r"#\s*build:\s*\n\s*#\s*context:\s*(\S+)", block)
        assert match, name
        context = match.group(1)
        assert (REPO_ROOT / context / "Dockerfile").is_file(), (name, context)


def test_every_env_file_is_the_one_env_example_says_to_create() -> None:
    expected = _env_file_name_from_example()
    for name, block in _service_blocks().items():
        assert _list_under(block, "env_file") == [expected], name


def test_the_readme_env_file_alternative_names_the_file_env_example_says_to_create() -> None:
    """Quick start offers `env_file` as an alternative to its default inline secrets; either way
    it must point at the same filename `deploy/env.example` itself says to copy to."""
    assert f"env_file: [{_env_file_name_from_example()}]" in _quick_start_section()


def test_env_example_sets_the_ui_password_uncommented() -> None:
    assert re.search(r"^LIKEARR_UI_PASSWORD=", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)


def test_the_example_ui_password_is_empty_so_an_unedited_copy_fails_closed() -> None:
    """A placeholder would be a working, publicly known password. Empty makes `likearr start`
    refuse to run until the user sets one."""
    line = re.search(r"^LIKEARR_UI_PASSWORD=(.*)$", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)
    assert line is not None
    assert line.group(1).strip() == ""


def test_the_quick_start_is_compose_only_with_no_config_file_to_write() -> None:
    """The Compose block and `docker compose up -d` are the whole install. No clone, no
    fetched or edited config.toml - `likearr start` writes it on a first start - and the Lidarr URL
    is an environment variable in the block and in env.example alike."""
    commands = _quick_start_commands()
    assert commands == ["docker compose up -d"]
    assert "LIKEARR_LIDARR_URL:" in _quick_start_yaml_block()
    assert re.search(r"^LIKEARR_LIDARR_URL=\S", ENV_EXAMPLE_PATH.read_text(), re.MULTILINE)


def test_the_image_carries_the_example_config_a_first_start_writes() -> None:
    """`config.example_config_text` reads it from inside the package, in the image and in a wheel."""
    dockerfile = DOCKERFILE_PATH.read_text()
    assert "COPY deploy/config.example.toml ./likearr/config.example.toml" in dockerfile
    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "!deploy/config.example.toml" in dockerignore
    assert dockerignore.index("!deploy/config.example.toml") > dockerignore.index("deploy")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"deploy/config.example.toml" = "likearr/config.example.toml"' in pyproject


def test_ignore_files_cover_the_env_file_and_the_data_dir() -> None:
    env_file = _env_file_name_from_example()
    for ignore in (".gitignore", ".dockerignore"):
        entries = {line.strip() for line in (REPO_ROOT / ignore).read_text().splitlines()}
        assert env_file in entries, ignore
        assert entries & {"likearr-data/", "likearr-data"}, ignore


# The healthcheck moves into the image, and both services document the uid/gid build
# args the image already takes.

DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"


def test_the_image_code_is_root_owned_and_precompiled() -> None:
    """The runtime user must not be able to rewrite its own code, so nothing is copied into
    the final stage with --chown; and since that means no process can write a .pyc there, likearr
    (installed editable, which UV_COMPILE_BYTECODE skips) is compiled in the builder. CI's image
    smoke test checks the built image; this pins the Dockerfile lines that make it so."""
    text = DOCKERFILE_PATH.read_text()
    _, _, final = text.partition(" AS final")
    assert final, "no final stage"
    copies = [line for line in final.splitlines() if line.startswith("COPY")]
    assert copies == ["COPY --from=builder /app/.venv /app/.venv", "COPY --from=builder /app/likearr /app/likearr"]
    builder = text.partition(" AS builder")[2].partition(" AS final")[0]
    assert "python -m compileall -q -f --invalidation-mode checked-hash /app/likearr" in builder
    # Root-owned means the runtime user reads through the world bits, so they must be set.
    assert "chmod -R a+rX /app/likearr /app/.venv" in builder
    # Nor can a file planted in the writable working directory (/data) shadow what the healthcheck
    # or a job imports.
    assert "PYTHONSAFEPATH=1" in final


def test_ci_smoke_checks_the_image_code_as_both_users() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "for user in likearr 1030:100; do" in ci
    assert "! -user 0" in ci and "-perm -o+w" in ci and "__pycache__" in ci
    assert "sys.flags.safe_path" in ci


def test_the_dockerfile_declares_a_healthcheck_after_expose() -> None:
    text = DOCKERFILE_PATH.read_text()
    _, _, after_expose = text.partition("EXPOSE 8770")
    assert after_expose, "Dockerfile no longer EXPOSEs 8770"
    healthcheck_line = next((line for line in after_expose.splitlines() if line.startswith("HEALTHCHECK")), None)
    assert healthcheck_line is not None, "no HEALTHCHECK after EXPOSE 8770"
    # Curl-free (no curl in the final image) and on 127.0.0.1, same host the compose example and
    # the always-allowed loopback hosts use (tests/web/test_app.py).
    assert "curl" not in healthcheck_line
    assert "127.0.0.1:8770/healthz" in healthcheck_line
    assert '"python"' in healthcheck_line
    assert "--interval=" in healthcheck_line
    assert "--timeout=" in healthcheck_line
    assert "--start-period=" in healthcheck_line
    assert "--retries=" in healthcheck_line


def test_the_dockerfile_healthcheck_is_the_only_one_docker_build_bakes_in() -> None:
    text = DOCKERFILE_PATH.read_text()
    assert text.count("HEALTHCHECK") == 1


def test_the_compose_example_does_not_duplicate_the_images_healthcheck() -> None:
    likearr_block = _service_blocks()["likearr"]
    # The image itself now carries the check (see the Dockerfile test above); a second,
    # independent one here could drift from it and give a container two different opinions.
    assert "healthcheck:" not in likearr_block


def test_likearr_cli_disables_the_inherited_healthcheck() -> None:
    cli_block = _service_blocks()["likearr-cli"]
    # likearr-cli never binds the port, so the image's healthcheck (probing :8770) would never
    # pass; a long-running command like prune-stage must not read unhealthy because of it.
    assert re.search(r"healthcheck:\s*\n\s*disable: true", cli_block)


def test_both_services_document_the_uid_gid_build_arg_route() -> None:
    for name, block in _service_blocks().items():
        assert "LIKEARR_UID" in block, name
        assert "LIKEARR_GID" in block, name


def test_env_example_documents_tz_without_overselling_it() -> None:
    text = ENV_EXAMPLE_PATH.read_text()
    assert re.search(r"^#?\s*TZ=", text, re.MULTILINE)
    # The schedule's own timezone lives in config.toml [schedule], not this variable - don't let
    # a reader believe setting TZ moves when scheduled runs fire.
    assert "Settings" in text or "[schedule]" in text
