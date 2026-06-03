# DA-Agent — Backend

Excel data-analyst agent built on the **Claude Agent SDK**, exposed via a
**FastAPI** server with SSE streaming. It profiles spreadsheets, runs
hypothesis-driven analysis through specialised subagents, and produces
deliverables (`.xlsx`, `.pptx`, `.docx`) — proposing a plan and asking
multiple-choice questions when requirements are ambiguous.

> Frontend lives in a sibling repo:
> **[`PhuongBui712/da-agent-fe`](https://github.com/PhuongBui712/da-agent-fe)**
> (Vite + React 19 + Tailwind). The `docker-compose.yml` here builds both stacks
> together.

---

## Architecture

```
FE (Next sibling repo)
  │  REST + SSE
  ▼
FastAPI server   ─►  AgentRunner  ─►  ClaudeSDKClient  ─►  claude CLI  ─►  model
     │                  │
     │                  ├─ subagents: profiler · analyst · reporter
     │                  ├─ skills:    xlsx · data-analysis  (reporter adds pptx, docx)
     │                  └─ MCP tool:  AskUserQuestion
     │
     ├─ KB ingestion (`kb_profiler` subagent, opus model)
     ├─ Per-session symlink farm  (kb_scope enforcement)
     └─ Outputs registry          (per-session + sidecar metadata)
```

| Concern | Where |
|---|---|
| HTTP routes (sessions, messages, kb, outputs, attachments) | `src/da_agent/server/routes/` |
| Agent core + system prompt | `src/da_agent/agent/` |
| KB ingestion pipeline | `src/da_agent/ingestion/` |
| Outputs observer + registry | `src/da_agent/outputs/` |
| Settings / paths | `src/da_agent/config.py` |
| TUI (CLI mode) | `src/da_agent/ui/` |

The 6-phase data-analysis methodology is enforced via the `data-analysis`
skill loaded into the system prompt; phases 3–4 are delegated to `analyst`
and phase 6 (final deliverable) to `reporter`.

---

## Run with Docker (recommended)

The compose file boots the **backend + frontend** as one stack. The frontend
build context points at `../da-agent-fe`, so both repos must sit side-by-side.

### Required folder layout

```
<parent-dir>/
├── da-agent-be/                ← run `docker compose` from here
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── .env                    ← copy from .env.docker.example, fill secrets
│   └── …
└── da-agent-fe/                ← sibling, must exist
    ├── Dockerfile
    ├── nginx.conf
    └── …
```

### Steps

```bash
# 1. Clone both repos as siblings
git clone https://github.com/PhuongBui712/da-agent-be.git  da-agent-be
git clone https://github.com/PhuongBui712/da-agent-fe.git  da-agent-fe

# 2. Configure
cd da-agent-be
cp .env.docker.example .env
$EDITOR .env                    # set ANTHROPIC_AUTH_TOKEN at minimum

# 3. Boot
docker compose up --build
```

- **FE** → `http://localhost:3000`
- **BE** → `http://localhost:8765`
- KB / sessions / outputs / attachments persist in the named volume `da-agent-data`.
- Credentials are read from `.env` at runtime — never baked into images.

### Run on another host (not `localhost`)

The FE bundles its backend URL at **build time** and the BE allow-lists FE
origins. Two values must match the host you serve from:

```bash
# in da-agent-be/.env
VITE_API_BASE_URL=http://<host>:8765       # baked into FE bundle
DA_AGENT_CORS_ORIGINS=http://<host>:3000   # BE accepts this FE origin

docker compose up --build
```

`DA_AGENT_CORS_ORIGINS` is comma-separated; unset, it defaults to
`http://127.0.0.1:3000,http://localhost:3000`.

### LibreOffice (formula recalc, pptx/docx export)

Bundled by default (`INSTALL_LIBREOFFICE=1` in `docker-compose.yml`). To slim
the image, build with `--build-arg INSTALL_LIBREOFFICE=0`.

---

## Run locally (no Docker)

Requirements: **Python ≥ 3.10**, **Node.js** (the SDK spawns the `claude` CLI
under the hood), and an Anthropic-compatible endpoint.

```bash
# Claude Code CLI (the SDK shell-outs to it)
npm install -g @anthropic-ai/claude-code

# This package
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Credentials (Databricks-routed Anthropic shown)
export ANTHROPIC_BASE_URL=https://<workspace>.azuredatabricks.net/serving-endpoints/anthropic
export ANTHROPIC_AUTH_TOKEN=<your-pat>

# Boot the API
da-agent serve --host 127.0.0.1 --port 8765
```

Optional: install **LibreOffice** so the xlsx skill can recalculate formulas.

---

## CLI

| Command | Purpose |
|---|---|
| `da-agent serve` | Start the FastAPI backend (`--host`, `--port`, `--no-plan`, `--no-thinking`, `--model`) |
| `da-agent chat` | Interactive multi-turn TUI session |
| `da-agent demo` | Scripted offline TUI walkthrough (no API key) |

In-session: `/plan` re-enter plan mode · `/exit` quit.

---

## HTTP API (high level)

| Group | Endpoints |
|---|---|
| Sessions | `GET/POST /sessions`, `GET/PATCH/DELETE /sessions/{sid}`, `POST /sessions/{sid}/fork`, `GET /sessions/{sid}/messages` |
| Messages (SSE) | `POST /sessions/{sid}/messages` — streams agent events |
| Interactions | `GET/POST /sessions/{sid}/interactions/{iid}` — `AskUserQuestion` round-trip |
| Knowledge base | `POST /kb/files` (upload + ingest), `GET/DELETE /kb/files/{kb_id}`, `GET /kb/files/{kb_id}/{manifest,memory,versions}`, `POST /kb/files/{kb_id}/reprocess` |
| Attachments | `POST/GET /sessions/{sid}/attachments`, `DELETE /sessions/{sid}/attachments/{att_id}` |
| Outputs | `GET /outputs`, `GET /outputs/{output_id}{,/meta}`, `DELETE /outputs/{output_id}` |

---

## Configuration

All env vars (Settings live in `src/da_agent/config.py`):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_BASE_URL` | Anthropic-compatible endpoint URL (Databricks supported) |
| `ANTHROPIC_AUTH_TOKEN` | Endpoint token / PAT |
| `ANTHROPIC_MODEL` | Default model alias |
| `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` | Per-tier model id overrides |
| `ANTHROPIC_CUSTOM_HEADERS` | Extra headers (e.g. Databricks routing flag) |
| `DA_AGENT_HOME` | Data root (default `~/.da-agent`; Docker `/data`) |
| `DA_AGENT_MODEL` | Model used by the in-session agent loop |
| `DA_AGENT_KB_PROFILER_MODEL` | Model used by KB ingestion (defaults to opus) |
| `DA_AGENT_CORS_ORIGINS` | Comma-separated FE origins allowed by CORS |
| `DA_AGENT_MAX_TURNS` | Optional turn cap |
| `DA_AGENT_PLAN_FIRST` | Start each session in plan mode (default `false`) |
| `DA_AGENT_SHOW_THINKING` | Expose extended-thinking blocks (default `true`) |
| `DA_AGENT_STREAM` | Token-level SSE streaming (default `true`) |
| `DA_AGENT_ATTACHMENT_MAX_BYTES` | Upload hard cap (default `100 MB`) |
| `DA_AGENT_SCOPE_WARN_BYTES` | Soft warn for `<scope>` block size |
| `VITE_API_BASE_URL` | Baked into the FE bundle at build time |

---

## Data layout (`DA_AGENT_HOME`)

```
~/.da-agent/
├── kb/                         # ingested KB files (raw + manifest + versions)
├── outputs/<sid>/              # registered standalone outputs (flat, per session)
├── attachments/<sid>/<att_id>/ # per-session uploads
├── sessions/                   # SDK session JSONL (CLAUDE_CONFIG_DIR) — resumable
├── sessions-data/<sid>/        # per-session symlink farm (kb_scope enforcement)
│   ├── kb/<kb_id> → kb/<kb_id> # one symlink per in-scope KB, rebuilt every turn
│   └── workspace/              # subagent scratch
└── agent-memory/kb_profiler/   # ingestion profiler memory notes
```

The `sessions-data/<sid>/{kb,workspace}/` farm is the load-bearing scope-enforcement
layer — it lists in `add_dirs` so the SDK sandbox can only see the KBs explicitly
in `kb_scope`. The final deliverable is written directly to canonical
`outputs/<sid>/`.

---

## Tests

```bash
pip install -e ".[dev]"
pytest                          # full unit + integration suite
```

Smoke scripts under `scripts/` exercise live and offline paths
(`smoke_kb_scope_leak`, `smoke_outputs`, `smoke_pptx_delegation`,
`smoke_security`, `smoke_streaming`, …). Most run without a live model;
`smoke_pptx_delegation` and friends require Anthropic credentials.

---

## License

See repository root.
