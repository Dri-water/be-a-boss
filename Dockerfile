# Robust, batteries-included base so in-container sessions can build/run most
# projects out of the box — and `apt-get`/`npm`/`pip` install whatever else they
# need at runtime (installs are ephemeral; persist them by editing this file).
#
# Full (non-slim) node:22-bookworm is glibc + buildpack-deps: broad binary
# compatibility (node-gyp, prebuilt binaries, etc.). Intentionally not Alpine.
FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# git/curl/wget/build-essential already ship in the full node image; add the rest.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip python3-dev pipx \
        ripgrep jq unzip zip ffmpeg \
        chromium fonts-liberation \
        tini sudo less procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# So sessions can screenshot web pages out of the box (headless chromium as root
# needs --no-sandbox; CHROME_BIN points tools at the binary).
ENV CHROME_BIN=/usr/bin/chromium \
    CHROMIUM_FLAGS="--headless=new --no-sandbox --disable-gpu"

# The SDK drives the standalone Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# GitHub CLI — enables the orchestrator's PR delivery route. Opt-in: auth via a
# GH_TOKEN in .env (or a mounted gh config). Without auth, delivery falls back to
# a local merge, so gh being present here costs nothing until you use it.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Sessions operate on bind-mounted repos owned by a different uid — don't let git
# refuse them with "dubious ownership".
RUN git config --system --add safe.directory '*'

# Engine-side git operations (delivery merges, branch pushes) need an identity and
# credentials: a neutral bot identity for merge commits (repo/global config still
# wins where set — and it keeps personal emails out of public history), and gh as
# the credential helper — it reads GH_TOKEN when present. Workers never see the
# token: it's scrubbed from their environment.
RUN git config --system user.name "be-a-boss" \
    && git config --system user.email "be-a-boss@localhost" \
    && git config --system credential.helper '!gh auth git-credential'

# Claude Code refuses bypassPermissions (--dangerously-skip-permissions) when
# running as root, UNLESS it knows it's sandboxed. The container is the sandbox,
# and we keep root so sessions can apt/npm/pip install freely.
ENV IS_SANDBOX=1

# Install the bot into its own venv (kept off the app source so a mounted copy of
# this repo can't shadow it).
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .
ENV PATH="/opt/venv/bin:${PATH}"

# tini reaps zombies + forwards SIGTERM so the bot shuts sessions down cleanly.
ENTRYPOINT ["tini", "--"]
CMD ["boss"]
