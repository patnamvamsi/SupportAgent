# SupportAgent runtime image — 10-wp WP-000: multi-stage, non-root user,
# `supportagent` entrypoint. The image is the walking skeleton; the API and
# worker entrypoints grow with their WPs (WP-013, WP-004/006).
#
# Both stages use python:3.12-slim; uv 0.8.17 is pinned in the builder —
# matching CI's UV_VERSION and the version that generated uv.lock.
#
# Layer caching: dependencies are installed before any source is copied,
# and the project itself is then installed *non-editable* into the same
# venv. So a src/ change rebuilds only the cheap project layer, a lock
# change rebuilds dependencies — and the runtime image is the venv alone.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
# Lock-first: the dependency layer re-resolves only when the lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv venv /venv
# Dependencies only — the project is not installed yet, so this layer is
# independent of src/. UV_PROJECT_ENVIRONMENT points `uv sync` at /venv
# (VIRTUAL_ENV is a pip convention that uv ignores).
RUN UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-dev --no-install-project
COPY src ./src
# Authoring machines may hand us source files that are not world-readable
# (umask 077); normalize before the project wheel is built from them.
RUN chmod -R a+rX /app
# Install the project itself, non-editable: the wheel lands in
# /venv/site-packages, so the runtime stage needs the venv and nothing else.
RUN UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    VIRTUAL_ENV=/venv
# Non-root (05 §1): fixed uid/gid so host volume mounts are predictable.
RUN groupadd --gid 10001 supportagent \
    && useradd --uid 10001 --gid supportagent --create-home --home-dir /app supportagent
WORKDIR /app
# The non-editable install puts supportagent in /venv/site-packages; the
# runtime image carries no source tree and no project metadata.
COPY --from=builder /venv /venv
# `supportagent migrate` (WP-003) needs the Alembic history; the installed
# wheel carries no source tree, so it resolves these from WORKDIR.
COPY alembic.ini ./
COPY migrations ./migrations
USER supportagent
EXPOSE 8000
ENTRYPOINT ["supportagent"]
# Walking skeleton: print the version. Replaced by `serve` in WP-013.
CMD ["--version"]
