# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12.11
ARG RCLONE_VERSION=1.75.1

# Keep rclone sourced from the official image while the application image
# remains based on the Python slim image used by the service.
FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        ffmpeg \
        libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${APP_GID}" muli \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /home/muli muli

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-compile .

# These paths document the container contract. Compose bind mounts replace
# them with explicitly selected host directories at runtime.
RUN mkdir -p /state /staging /sources/source-01 \
    && chown -R "${APP_UID}:${APP_GID}" /state /staging /sources

USER muli:muli
ENTRYPOINT ["muli-ingest"]
