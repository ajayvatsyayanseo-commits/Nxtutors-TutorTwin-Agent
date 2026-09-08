# TutorTwin API - Cloud Run target, scale to zero.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Tesseract. Every page it reads locally is a vision call not paid for, and the
# planner routes around it when the binary is absent - so this is a cost
# decision, not a dependency. `eng` only: each language pack is ~15MB of image
# for a language this deployment does not serve.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer first so source edits do not invalidate the install.
COPY pyproject.toml README.md ./
COPY src ./src
# `[gcp]` pulls boto3 (for R2, which speaks the S3 protocol - not AWS S3),
# google-cloud-tasks and google-auth. They are imported lazily by their adapters,
# but a deployed container always selects those adapters: `require_deployable()`
# refuses to start without the R2 and Cloud Tasks settings.
RUN pip install --no-cache-dir ".[gcp]"

COPY migrations ./migrations
COPY alembic.ini ./

# Never run as root.
RUN useradd --create-home --uid 10001 tutortwin
USER tutortwin

# Cloud Run injects PORT; default for local runs.
ENV PORT=8080
EXPOSE 8080

# No secrets are baked in. Every credential arrives at run time from Secret
# Manager, and .dockerignore keeps .env out of the build context so one cannot
# be copied in by accident.
#
# Single worker: Cloud Run scales by adding instances, not in-container workers,
# and extra workers would multiply the Postgres connection count per container.
# SIGTERM is handled by the entrypoint, which drains in-flight requests.
CMD exec python -m tutortwin
