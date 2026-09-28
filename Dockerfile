# syntax=docker/dockerfile:1

# Pin uv via the official distroless image rather than `pip install uv` or curl | sh, so the
# builder stage never needs network access beyond PyPI for the project's own dependencies. A
# named stage (rather than `COPY --from=ghcr.io/astral-sh/uv:0.6` directly) so Dependabot's
# docker ecosystem can see and track this image: it does not watch images referenced only in
# a COPY --from. Pinned by digest (the `0.6` tag can move) - a multi-arch index, so the digest
# still resolves on both amd64 and arm64. Re-resolve with:
#   curl -s "https://ghcr.io/token?service=ghcr.io&scope=repository:astral-sh/uv:pull" \
#     | grep -o '"token":"[^"]*"' | cut -d'"' -f4 \
#     | xargs -I{} curl -s -D - -o /dev/null -H "Authorization: Bearer {}" \
#         -H "Accept: application/vnd.oci.image.index.v1+json" \
#         https://ghcr.io/v2/astral-sh/uv/manifests/0.6 | grep -i docker-content-digest
FROM ghcr.io/astral-sh/uv:0.6@sha256:4a6c9444b126bd325fba904bff796bf91fb777bf6148d60109c4cb1de2ffc497 AS uv

# ---------------------------------------------------------------- builder
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS builder

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first (no dev extras, no project code yet) so this layer is cached across
# source-only changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now add the project itself and install it into the same venv.
COPY likearr ./likearr
COPY README.md ./
# The example config `likearr start` writes to /data/config.toml on a first start with none,
# inside the package where `config.example_config_text` looks for it.
COPY deploy/config.example.toml ./likearr/config.example.toml
# pyproject's wheel force-include reads it from deploy/, and hatch checks that path on every build,
# the editable one `uv sync` makes here included.
COPY deploy/config.example.toml ./deploy/config.example.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# `uv sync` installs the project editable, so UV_COMPILE_BYTECODE compiles only the dependencies.
# Precompile likearr itself: the final image's code is root-owned (below), so no process could
# write a .pyc there and each would otherwise recompile every module in memory. checked-hash, not
# timestamps, so a .pyc stays valid whatever mtime the COPY into the final stage leaves behind;
# -f, so a stale .pyc from the build context is replaced rather than kept. Then make everything
# readable (and directories enterable) by any uid: the final stage copies it root-owned, so the
# runtime user reads it through the world bits, which a checkout made under umask 077 lacks.
RUN /app/.venv/bin/python -m compileall -q -f --invalidation-mode checked-hash /app/likearr \
    && chmod -R a+rX /app/likearr /app/.venv

# ---------------------------------------------------------------- final
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS final

ARG LIKEARR_UID=1000
ARG LIKEARR_GID=1000

# Not read by likearr itself (the version comes from the installed package's own metadata) -
# these two exist only to label the image and to tell `likearr doctor`/the web footer
# which commit is running, which the image otherwise has no way to know (`.dockerignore` excludes
# `.git`). Neither is required: an image built without them is simply unlabelled and shows no
# commit. Pass them with, for example, `--build-arg VCS_REF=$(git rev-parse --short HEAD)
# --build-arg VERSION=$(git describe --tags --always)`.
ARG VCS_REF=""
ARG VERSION=""
LABEL org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
ENV LIKEARR_COMMIT="${VCS_REF}"

# No build tools in the final image: just the interpreter, the prebuilt venv, and the app.
# The GID may already exist in the base image (100 is `users` on Debian); reuse it rather than fail,
# so media-owner uid/gid pairs like 1030:100 work as build args.
RUN (getent group "${LIKEARR_GID}" >/dev/null || groupadd --gid "${LIKEARR_GID}" likearr) \
    && useradd --uid "${LIKEARR_UID}" --gid "${LIKEARR_GID}" --create-home --shell /usr/sbin/nologin likearr

WORKDIR /app

# Root-owned and world-readable, not owned by the runtime user: the process can read and
# run its code but never rewrite it, and a restarted container cannot carry an edited copy.
# Everything likearr writes lives under /data.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/likearr /app/likearr

# PYTHONSAFEPATH: `python -c` and `python -m` would otherwise put the working directory
# (writable /data, or a job's directory under it) first on sys.path, so a file planted there would
# shadow the standard library in the healthcheck and in every job, and outlive a restart.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1

# State DB, Spotify token file and config all live here. Mount a host directory or named volume.
VOLUME ["/data"]

USER likearr

# Relative outputs (diff.json, adopt.json, prune.json) must land somewhere writable: a scheduled
# `run --scheduled` with no --out died on "Permission denied: diff.json" in /app. /data is the volume.
WORKDIR /data

# `likearr start` (the service: web UI, scheduler and job runner) listens here. The one-shot
# tools container (likearr-cli) passes its own command and never binds this port.
EXPOSE 8770

# Curl-free (no curl in this image) and on 127.0.0.1, which the web UI always accepts, whatever
# LIKEARR_ALLOWED_HOSTS says (tests/web/test_app.py). A fresh install with no state database yet still reads healthy: see
# the healthz docstring in likearr/web/app.py. likearr-cli never
# binds the port and inherits this check too; its compose service disables it explicitly.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=5)"]

ENTRYPOINT ["likearr"]
CMD ["start", "--host", "0.0.0.0", "--port", "8770", "-c", "/data/config.toml"]
