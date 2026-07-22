# syntax=docker/dockerfile:1.7
FROM node:22.17.0-bookworm-slim AS web-builder
WORKDIR /src/apps/web
RUN corepack enable && corepack prepare pnpm@11.13.0 --activate
COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm build

FROM python:3.12.10-slim-bookworm AS wheel-builder
WORKDIR /src
RUN python -m pip install --no-cache-dir build==1.3.0
COPY pyproject.toml README.md ./
COPY corvus/ corvus/
RUN python -m build --wheel --outdir /wheel

FROM python:3.12.10-slim-bookworm AS runtime
ENV CORVUS_BOOTSTRAP_TOKEN="" \
    CORVUS_SESSION_SECRET="" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home-dir /app --create-home corvus \
    && install -d -o corvus -g corvus /data /app/web
COPY --from=wheel-builder /wheel/*.whl /tmp/corvus.whl
RUN python -m pip install --no-cache-dir /tmp/corvus.whl && rm /tmp/corvus.whl
COPY --from=web-builder --chown=corvus:corvus /src/apps/web/dist/ /app/web/
USER corvus
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=2).read()"]
CMD ["corvus-mvp", "server", "--database", "/data/corvus.sqlite3", "--host", "0.0.0.0", "--port", "8080", "--static-web-dir", "/app/web"]
