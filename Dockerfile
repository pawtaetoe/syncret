# syncret server: one process for one person. Base images pinned by digest.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 app \
 && mkdir /data && chown app:app /data

# syncret with its server and anthropic extras, from the lockfile
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --extra server --extra anthropic \
 && rm /usr/local/bin/uv

# claude code for the claude-code provider; the installer checks the binary's sha256
USER app
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install.sh \
 && bash /tmp/install.sh \
 && rm /tmp/install.sh

ENV PATH=/app/.venv/bin:/home/app/.local/bin:$PATH \
    SYNCRET_DATA=/data \
    CLAUDE_CONFIG_DIR=/home/app/.claude \
    DISABLE_AUTOUPDATER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
ENTRYPOINT ["tini", "--"]
# listens on all interfaces inside the container; compose publishes it on 127.0.0.1 only
CMD ["syncret", "serve", "--host", "0.0.0.0", "--port", "8080"]
