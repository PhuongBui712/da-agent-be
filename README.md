# DA-Agent — Excel Data-Analyst Agent (Agent module)

A Senior-Data-Analyst agent built on the **Claude Agent SDK**, driven from a
**Claude Code-style TUI**. This package is the *agent module* of a larger system; it is
structured so the same core can later be driven by a web backend without changes.

It can read and understand Excel/CSV files (schema + cross-sheet relationships), answer
simple-to-complex questions, generate new sheets/charts via the Anthropic **xlsx** skill,
and run an end-to-end investigation using subagents — proposing a **plan for your
approval** and asking **multiple-choice questions** when requirements are ambiguous.

```
› Analyze sales.xlsx and surface the key trends
✻ Thinking …
● Bash(extract-text /data/sales.xlsx | head -40)
  ⎿  ## Sheet: Orders …
▌ Plan
  1. Profile both sheets …            ← approve / revise inline
● Task(profiler: profile both sheets)
  ⎿  Orders: 48,211 rows …            ← subagent steps, indented
? Where should I put the output?      ← AskUserQuestion picker
✶ done in 12.3s · 7 turns · $0.0421
```

## Quick start

Requirements: **Python ≥ 3.10**, **Node.js** (the SDK runs the Claude Code CLI), and an
Anthropic API key.

```bash
# 1. Claude Code CLI (the SDK spawns it under the hood)
npm install -g @anthropic-ai/claude-code

# 2. This package
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Credentials
export ANTHROPIC_API_KEY=sk-ant-...

# 4. See the TUI without spending tokens
da-agent demo

# 5. Chat for real
da-agent           # or: da-agent chat
```

Optional: install **LibreOffice** so the xlsx skill can recalculate formulas
(`scripts/recalc.py`).

## Run with Docker (backend + frontend)

Package the whole stack so another machine only needs Docker + a token. The
backend image bundles Python, Node, and the `claude` CLI (the SDK spawns it as a
subprocess), so nothing needs installing by hand.

```bash
# from da-agent-be/  (expects ../da-agent-fe alongside it)
cp .env.docker.example .env     # then fill in ANTHROPIC_AUTH_TOKEN
docker compose up --build
```

- FE → http://localhost:3000  ·  BE → http://localhost:8765
- KB / sessions / outputs / attachments persist in the `da-agent-data` volume.
- Credentials are read from `.env` at runtime — never baked into the images.

**LibreOffice (formula recalc):** off by default to keep the image small. Enable
with `docker compose build --build-arg INSTALL_LIBREOFFICE=1 backend` (or flip
the arg in `docker-compose.yml`).

### Run on another host (not localhost)

The FE bakes its backend URL at build time and the BE allow-lists FE origins, so
two values must match the host you serve from:

```bash
# in .env:
VITE_API_BASE_URL=http://<host>:8765       # FE → BE (baked into FE build)
DA_AGENT_CORS_ORIGINS=http://<host>:3000   # BE accepts this FE origin

docker compose up --build
```

`DA_AGENT_CORS_ORIGINS` is comma-separated; unset, it defaults to
`http://127.0.0.1:3000,http://localhost:3000`.

### Commands & flags
- `da-agent` / `da-agent chat` — interactive multi-turn session
- `da-agent demo` — scripted walkthrough of the TUI (no API key)
- `--no-plan` — don't start in plan mode · `--no-thinking` — hide thinking · `--model <id>`
- In-session: `/plan` re-enter plan mode next turn · `/exit` quit

## Where things live

Data lives under `~/.da-agent` (override with `DA_AGENT_HOME`):

```
~/.da-agent/
├── kb/          # persistent spreadsheets (drop .xlsx here to use across sessions)
├── workspace/   # agent writes generated files here (new .xlsx, charts)
└── sessions/    # SDK session JSONL (CLAUDE_CONFIG_DIR) — resumable, no DB
```

## Architecture

The agent core depends only on a small **`AgentUI` protocol** — never on the terminal
libraries. The CLI is one adapter (`ConsoleAgentUI`); a future web backend implements the
same protocol over a websocket and reuses everything else.

```
cli.py ─┐
        ├─► AgentRunner ──► ClaudeSDKClient ──► claude CLI ──► model
ConsoleAgentUI (AgentUI)        │  builds ClaudeAgentOptions:
   rich render  +               │   • skills=["xlsx"]  (from .claude/skills/)
   prompt_toolkit picker        │   • agents={profiler, analyst, visualizer}
        ▲                       │   • mcp: ask_user_question (custom tool)
        │  protocol calls       │   • can_use_tool → plan approval (ExitPlanMode)
        └───────────────────────┘
```

| Concern | Where | Notes |
|---|---|---|
| SDK session + options + render loop | `agent/core.py` | UI-agnostic core |
| Senior-analyst persona / workflow | `agent/prompts.py` | system prompt |
| `AskUserQuestion` tool | `agent/tools.py` | in-process SDK MCP tool we fully control |
| Plan approval | `agent/permissions.py` | intercepts `ExitPlanMode` in `can_use_tool` |
| Subagents | `agent/subagents.py` | dispatched via the `Task` tool |
| Interaction payloads | `agent/events.py` | serializable (websocket-ready) |
| UI seam | `ui/base.py` | `AgentUI` protocol |
| Terminal render + spinner | `ui/console.py` | `rich` |
| Tabbed picker / plan prompt | `ui/prompts.py` | `prompt_toolkit`, TTY + non-TTY fallback |
| xlsx skill (bundled) | `.claude/skills/xlsx/` | discovered via `setting_sources=["project"]` |

### Design choices worth knowing
- **Plan approval** uses the SDK's `plan` permission mode; when the model calls
  `ExitPlanMode`, `can_use_tool` surfaces the plan. On approval the runner switches to
  `acceptEdits` so execution isn't gated step-by-step (all steps still render).
- **`AskUserQuestion`** is a custom in-process MCP tool rather than the built-in, so the
  full request→render→answer round-trip is under our control and identical across UIs.
- **Skills** load from `.claude/skills/` at the project root (the SDK `cwd`). Only `xlsx`
  is enabled here.
- **Non-destructive writes** and **output-target clarification** are enforced via the
  system prompt.

## Tests

```bash
pip install -e ".[dev]"
pytest
```
