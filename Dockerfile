# One image, two entry points: the API (`doctask.main:app`) and the collection
# watcher (`doctask.watcher`). Same dependencies, same code, so a rule that says "the
# watcher and the API drive the same code path" is true in the container the same way
# it is true in the process -- there is only one image, not an API image and a
# separately-built watcher image that could drift.

FROM python:3.11-slim AS base

# libmupdf needs no system package beyond what the pymupdf wheel bundles; psycopg
# (binary extra) is likewise wheel-only. Nothing here compiles from source.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Not part of the package -- `scripts/apply_migrations.py` is what the compose
# `migrate` service runs, and it reads `migrations/*.sql` relative to the working
# directory rather than importing them, so both come along as plain files.
COPY scripts ./scripts
COPY migrations ./migrations

# Runs as a non-root user in both services; nothing here needs root.
RUN useradd --create-home --uid 1000 doctask
USER doctask

# No CMD: `docker-compose.yml` sets the command per service (uvicorn for `api`,
# `python -m doctask.watcher` for `watcher`), so this image is not opinionated about
# which half of the system a given container runs.
