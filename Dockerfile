# DA-Agent backend image. SDK spawns the `claude` CLI (Node), so we need
# Python + Node. Credentials come at runtime via env (see .env.docker.example).
FROM python:3.12-slim

# Enables the xlsx skill's formula recalculation via LibreOffice.
ARG INSTALL_LIBREOFFICE=1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DA_AGENT_HOME=/data \
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# System deps: build tools, Node 20 for the claude CLI, and optional
# LibreOffice/poppler/pandoc for docx/pptx/xlsx skills.
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
# RUN npm install -g @anthropic-ai/claude-code

# Python deps for bundled skills (xlsx/pptx/docx). lxml + defusedxml are
# needed by xlsx scripts but not declared in pyproject.toml.
RUN pip install --no-cache-dir \
        pandas python-pptx python-docx Pillow "markitdown[pptx]" lxml defusedxml

# Node deps for pptx/docx skills (invoked via `node -e ...`).
RUN npm install -g pptxgenjs docx

WORKDIR /app

# Editable install so find_project_root() can locate /app/.claude for skill
# discovery; a non-editable install would move the package and break that.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install -e .

# Bundled skills/agents must live under .claude/ for SDK discovery.
COPY .claude ./.claude
# COPY .claude/skills ./.claude/skills
# COPY .claude/agents ./.claude/agents

# Runtime data (kb/workspace/sessions/outputs). Mount a volume to persist.
RUN mkdir -p /data

EXPOSE 8765
CMD ["da-agent", "serve", "--host", "0.0.0.0", "--port", "8765"]
