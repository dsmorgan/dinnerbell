# SPDX-License-Identifier: AGPL-3.0-or-later
# Dinnerbell container image. Build: docker build -f Containerfile -t dinnerbell .
#                                  or: podman build -t dinnerbell .

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

# ---- build: resolve dependencies from uv.lock into /app/.venv -------------------------------
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------------------------
# Keep this digest equal to the build stage's so the venv's interpreter matches.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
LABEL org.opencontainers.image.title="Dinnerbell" \
      org.opencontainers.image.description="Household meal coordination" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
RUN groupadd --system --gid 10001 dinnerbell \
 && useradd --system --uid 10001 --gid dinnerbell --no-create-home --home-dir /nonexistent dinnerbell \
 && mkdir -p /data/backups \
 && chown -R dinnerbell:dinnerbell /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DINNERBELL_DATABASE_URL=sqlite:////data/dinnerbell.db
USER dinnerbell:dinnerbell
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["dinnerbell"]
CMD ["serve"]
