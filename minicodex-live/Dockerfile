# minicodex-live: the console as a container.
#
# Two stages, because the frontend toolchain (node_modules is ~200MB of dev
# dependencies) has no business in the runtime image.  The runtime installs
# bubblewrap: without it the startup gate refuses to serve at all, which is
# the point of the gate -- the image either confines commands or it does not
# start.
#
# Build:   docker build -t minicodex-live .
# Run:     docker compose up          (see compose.yaml for why the security
#                                     options there are not optional)

# -- stage 1: build the console frontend --------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# -- stage 2: the server ------------------------------------------------------
FROM python:3.12-slim AS runtime

# bubblewrap is the sandbox.  `tini` reaps the agent's orphaned subprocesses
# (a shell session killed mid-command leaves a process group behind; chapter 2
# kills the group, but only if the reaper survives to run the cleanup).
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap tini git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md ./
COPY --from=frontend /build/frontend/dist ./frontend/dist

# `--frozen` is the lockfile being a contract, not a suggestion.  The venv
# lives in the image; the state lives in the volume below -- two different
# directories, and the difference is the whole deployment model.
RUN uv sync --frozen --all-extras --no-dev

# A container that runs shell commands on behalf of authenticated users must
# not be root by accident.  10001 owns nothing at build time; the volume is
# chowned at first mount (see compose.yaml's volume setup).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin minicodex \
    && mkdir -p /data \
    && chown -R minicodex:minicodex /app /data
USER minicodex

ENV MINICODEX_DATA_DIR=/data/console \
    MINICODEX_HOME=/data/home \
    PATH=/app/.venv/bin:$PATH

VOLUME /data

# The startup gate inside here needs a bind-mounted bwrap workspace that works
# under userns; compose.yaml carries the security options and the explanation.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "minicodex", "serve", "--host", "0.0.0.0", "--port", "8000"]
