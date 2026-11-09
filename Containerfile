# SPDX-License-Identifier: AGPL-3.0-or-later
# Dinnerbell container image. Build: docker build -f Containerfile -t dinnerbell .
#                                  or: podman build -t dinnerbell .

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# ---- build: resolve dependencies from uv.lock into /app/.venv -------------------------------
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
# The build backend comes from uv.lock's `build` group, installed with --require-hashes into a
# throwaway venv, and the wheel is built with --no-build-isolation and --offline. Nothing is resolved from PyPI
# outside the lock, and the backend never reaches the runtime stage.
RUN uv export --locked --only-group build --no-emit-project --no-header --format requirements-txt \
      --output-file /tmp/build-requirements.txt \
 && uv venv /tmp/build-venv \
 && uv pip install --python /tmp/build-venv --require-hashes --no-deps -r /tmp/build-requirements.txt
COPY README.md LICENSE ./
COPY src ./src
RUN uv --offline build --wheel --no-build-isolation --python /tmp/build-venv/bin/python --out-dir /tmp/dist \
 && uv --offline pip install --python /app/.venv --no-deps --no-index /tmp/dist/*.whl

# ---- runtime ---------------------------------------------------------------------------------
# Keep this digest equal to the build stage's so the venv's interpreter matches.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
LABEL org.opencontainers.image.title="Dinnerbell" \
      org.opencontainers.image.description="Household meal coordination" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/dsmorgan/dinnerbell"
# Debian's security fix for zlib (CVE-2026-85091, CVE-2026-76844), which this base digest predates.
# Pinned, so the build stays reproducible. Remove this step once python:3.12-slim ships it.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
 && rm -rf /var/lib/apt/lists/*
# Remove pip and ensurepip's bundled pip wheel: the app runs from /app/.venv, which has none, and
# nothing in the image installs packages. This keeps pip's advisories out of the image scan.
RUN python -m pip uninstall --yes pip \
 && rm -rf /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/ensurepip \
 && groupadd --system --gid 10001 dinnerbell \
 && useradd --system --uid 10001 --gid dinnerbell --no-create-home --home-dir /nonexistent dinnerbell \
 && mkdir -p /data/backups \
 && chown -R dinnerbell:dinnerbell /data
COPY --from=build /app/.venv /app/.venv
# The short commit hash of a main-branch build, shown as 0.2.0+abc1234. Release builds leave it
# empty. Declared after the venv copy so changing it rebuilds only the layers below.
ARG DINNERBELL_BUILD=""
ENV DINNERBELL_BUILD=$DINNERBELL_BUILD \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DINNERBELL_DATABASE_URL=sqlite:////data/dinnerbell.db
USER dinnerbell:dinnerbell
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8013
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8013/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["dinnerbell"]
CMD ["serve"]
