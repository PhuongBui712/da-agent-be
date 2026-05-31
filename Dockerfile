# DA-Agent backend image.
#
# The Claude Agent SDK does not call the model directly -- it spawns the
# `claude` CLI (Node) as a subprocess, which then talks to the configured
# Anthropic-compatible endpoint. So this image needs BOTH Python and Node +
# the globally-installed CLI. Databricks credentials are passed at runtime via
# env vars (see .env.docker.example); none are baked into the image.
FROM python:3.12-slim

# Set INSTALL_LIBREOFFICE=1 at build time to enable the xlsx skill's formula
# recalculation (scripts/recalc.py). Forced on: skill runtime relies on it
# alongside poppler/pandoc (user-approved image bloat for skill parity).
ARG INSTALL_LIBREOFFICE=1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DA_AGENT_HOME=/data \
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# System deps: curl/git for build, Node 20 (NodeSource) for the claude CLI,
# and (optionally) LibreOffice + poppler/pandoc/fonts for docx/pptx/xlsx skills.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && if [ "$INSTALL_LIBREOFFICE" = "1" ]; then \
         apt-get install -y --no-install-recommends \
           libreoffice-core libreoffice-impress libreoffice-writer libreoffice-calc \
           poppler-utils pandoc fonts-dejavu; \
       fi \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# The CLI the SDK spawns under the hood.
RUN npm install -g @anthropic-ai/claude-code

# Pre-install Python deps required by bundled skills (xlsx/pptx/docx) so the
# agent doesn't have to fetch them from PyPI at runtime. lxml + defusedxml are
# also required by the xlsx skill scripts but are not declared in pyproject.toml.
RUN pip install --no-cache-dir \
        pandas python-pptx python-docx Pillow "markitdown[pptx]" lxml defusedxml

# Pre-install Node deps for pptx/docx skills (used via `node -e ...` from Bash).
RUN npm install -g pptxgenjs docx

WORKDIR /app

# Editable install: keeps the package at /app/src/da_agent so config.py's
# find_project_root() walks up and finds /app/.claude (skill discovery). A
# non-editable install would move the package into site-packages and break
# that lookup.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install -e .

# Bundled skills/agents must sit under the project root's .claude/ for the SDK
# to discover them (setting_sources=["project","local"]).
COPY .claude ./.claude
# COPY .claude/skills ./.claude/skills
# COPY .claude/agents ./.claude/agents

# Runtime data (kb/workspace/sessions/outputs/attachments). Mount a volume here
# to persist across restarts.
RUN mkdir -p /data

EXPOSE 8765
CMD ["da-agent", "serve", "--host", "0.0.0.0", "--port", "8765"]
