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

FROM python:3.15-slim AS builder
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

FROM python:3.15-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    VIRTUAL_ENV=/venv
# Non-root (05 §1): fixed uid/gid so host volume mounts are predictable.
# /app is world-traversable: the local install (docker/local) runs the
# container as the host user, so files it writes under local/ stay theirs.
RUN groupadd --gid 10001 supportagent \
    && useradd --uid 10001 --gid supportagent --create-home --home-dir /app supportagent \
    && chmod 755 /app
WORKDIR /app
# The non-editable install puts supportagent in /venv/site-packages; the
# runtime image carries no source tree and no project metadata.
COPY --from=builder /venv /venv
# `supportagent migrate` (WP-003) needs the Alembic history; the installed
# wheel carries no source tree, so it resolves these from WORKDIR.
COPY alembic.ini ./
COPY migrations ./migrations
# The shipped content, read-only to the app user: taxonomy, packs, plugins,
# prompts, runbooks, templates, the eval cases and the calibration artifact,
# the example catalog and config. The image then runs without a checkout;
# a deployment mounts its own catalog (and anything it customises) over these.
COPY LICENSE NOTICE supportagent.example.yaml ./
COPY catalog ./catalog
COPY taxonomy ./taxonomy
COPY packs ./packs
COPY plugins ./plugins
COPY prompts ./prompts
COPY runbooks ./runbooks
COPY templates ./templates
COPY evals ./evals
USER supportagent
EXPOSE 8000
ENTRYPOINT ["supportagent"]
# Single-container mode (02 §4): the API + admin UI and the worker in one
# process; bind all interfaces inside the container. Split deployments run
# `serve` and `worker` as separate containers instead.
CMD ["run", "--host", "0.0.0.0", "--port", "8000"]
