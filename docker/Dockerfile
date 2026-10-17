# SupportAgent runtime image — 10-wp WP-000: multi-stage, non-root user,
# `supportagent` entrypoint. The image is the walking skeleton; the API and
# worker entrypoints grow with their WPs (WP-013, WP-004/006).
#
# Both stages use python:3.12-slim; uv 0.8.17 is pinned in the builder —
# matching CI's UV_VERSION and the version that generated uv.lock.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
# Lock-first: the dependency layer re-resolves only when the lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv venv /venv
# Install the project itself (src layout) into the same venv.
# UV_PROJECT_ENVIRONMENT points `uv sync` at /venv (VIRTUAL_ENV is a pip
# convention that uv ignores).
COPY src ./src
# Authoring machines may hand us source files that are not world-readable
# (umask 077); the runtime user is non-root, so normalize.
RUN chmod -R a+rX /app
RUN UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    VIRTUAL_ENV=/venv
# Non-root (05 §1): fixed uid/gid so host volume mounts are predictable.
RUN groupadd --gid 10001 supportagent \
    && useradd --uid 10001 --gid supportagent --create-home --home-dir /app supportagent
WORKDIR /app
# The editable install resolves supportagent to /app/src — keep the same path.
COPY --from=builder /venv /venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
USER supportagent
EXPOSE 8000
ENTRYPOINT ["supportagent"]
# Walking skeleton: print the version. Replaced by `serve` in WP-013.
CMD ["--version"]
