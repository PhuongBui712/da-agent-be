# Module Explanation — `da-agent-be`

> Tài liệu này mô tả chi tiết từng module trong hệ thống `da-agent-be`: vai trò, public API, các điểm tinh tế (invariant / seam / bug-fix history), và các sơ đồ Mermaid biểu diễn core logic của hệ thống.
>
> Phạm vi: toàn bộ `src/da_agent/` (CLI + agent core + UI Protocol + FastAPI server + KB pipeline + Outputs registry).

---

## Table of Contents

- [0. Tổng quan kiến trúc](#0-tổng-quan-kiến-trúc)
- [1. Entry points: `__init__.py`, `__main__.py`, `cli.py`](#1-entry-points-__init__py-__main__py-clipy)
- [2. config.py — Settings + path resolution](#2-configpy--settings--path-resolution)
- [3. agent/ — Agent core](#3-agent--agent-core)
  - [3.1 agent/events.py](#31-agentevents.py)
  - [3.2 agent/prompts.py](#32-agentpromptspy)
  - [3.3 agent/subagents.py](#33-agentsubagentspy)
  - [3.4 agent/permissions.py — make_can_use_tool](#34-agentpermissionspy--make_can_use_tool)
  - [3.5 agent/todos.py — TodoStore](#35-agenttodospy--todostore)
  - [3.6 agent/core.py — AgentRunner](#36-agentcorepy--agentrunner)
- [4. ui/ — UI Protocol seam](#4-ui--ui-protocol-seam)
  - [4.1 ui/base.py — AgentUI Protocol](#41-uibasepy--agentui-protocol)
  - [4.2 ui/console.py — ConsoleAgentUI](#42-uiconsolepy--consoleagentui)
  - [4.3 ui/prompts.py](#43-uipromptspy)
- [5. outputs/ — Outputs detection + registry](#5-outputs--outputs-detection--registry)
  - [5.1 outputs/observer.py — OutputsObserver](#51-outputsobserverpy--outputsobserver)
  - [5.2 outputs/registry.py — OutputsRegistry](#52-outputsregistrypy--outputsregistry)
- [6. kb/ + ingestion/ — Knowledge Base pipeline](#6-kb--ingestion--knowledge-base-pipeline)
  - [6.1 ingestion/registry.py — KbRegistry](#61-ingestionregistrypy--kbregistry)
  - [6.2 ingestion/profiler.py — kb_profiler subagent definition](#62-ingestionprofilerpy--kb_profiler-subagent-definition)
  - [6.3 ingestion/runner.py — orchestrator](#63-ingestionrunnerpy--orchestrator)
  - [6.5 KB ingestion sequence](#65-kb-ingestion-sequence)
  - [6.6 Legacy (inactive — kept for backcompat, NOT imported)](#66-legacy-inactive--kept-for-backcompat-not-imported)
- [7. server/ — FastAPI layer](#7-server--fastapi-layer)
  - [7.1 server/app.py — Factory](#71-serverapppy--factory)
  - [7.2 server/state.py — In-process state](#72-serverstatepy--in-process-state)
  - [7.3 server/sse.py](#73-serverssespy)
  - [7.4 server/schemas.py — Pydantic models](#74-serverschemaspython--pydantic-models)
  - [7.5 server/scope.py — build_scope / render_scope](#75-serverscopepy--build_scope--render_scope)
  - [7.6 server/attachments_registry.py](#76-serverattachments_registrypy)
  - [7.7 server/web_ui.py — WebAgentUI](#77-serverweb_uipy--webagentui)
  - [7.8 server/routes/ — Endpoint table](#78-serverroutes--endpoint-table)
  - [7.9 server/replay.py — Session-history replay](#79-serverreplaypy--session-history-replay)
  - [7.10 server/session_farm.py — per-session symlink farm](#710-serversession_farmpy--per-session-symlink-farm)
- [8. SSE & Message turn — Flow tổng hợp](#8-sse--message-turn--flow-tổng-hợp)
  - [8.1 Sequence: POST /sessions/{sid}/messages](#81-sequence-post-sessionssidmessages)
  - [8.2 Inventory các SSE event được emit](#82-inventory-các-sse-event-được-emit)
- [9. Crash recovery & restart safety](#9-crash-recovery--restart-safety)
- [10. Map tóm tắt các seam mở rộng](#10-map-tóm-tắt-các-seam-mở-rộng)

---

## 0. Tổng quan kiến trúc

`da-agent-be` là một Excel data-analyst agent đặt trên Claude Agent SDK, triển khai dưới hai bề mặt sử dụng:
- **CLI** (`da-agent chat` / `da-agent demo`) — single-user REPL với `rich` overlay + `prompt_toolkit` selector.
- **HTTP server** (`da-agent serve`) — FastAPI + Server-Sent Events; backend cho frontend React (`da-agent-fe`).

Cả hai bề mặt **chia chung agent core** (`agent/`) và **chia chung Protocol** (`ui/base.py::AgentUI`). Sự khác biệt nằm ở triển khai UI: CLI dùng `ConsoleAgentUI` (`ui/console.py`), web dùng `WebAgentUI` (`server/web_ui.py`).

```mermaid
flowchart LR
    subgraph EntryPoints["Entry points"]
        CLI["cli.py: chat / demo / serve"]
    end

    subgraph AgentCore["agent/ (SDK-facing core)"]
        Runner["AgentRunner<br/>(send + render dispatch)"]
        Perms["make_can_use_tool<br/>(ExitPlan / AskQuestion gate)"]
        Todos["TodoStore<br/>(Task* / TodoWrite normalizer)"]
        Subagents["build_subagents()<br/>analyst / reporter (kb_profiler is ingestion-only — see §6)"]
        Prompts["build_system_prompt()"]
    end

    subgraph UISeam["ui/ (Protocol seam)"]
        UIProto["AgentUI Protocol"]
        Console["ConsoleAgentUI"]
        Web["WebAgentUI"]
    end

    subgraph ServerLayer["server/ (FastAPI)"]
        App["create_app + lifespan"]
        State["AppState<br/>(registry + interactions + kb + outputs + attachments)"]
        Routes["routes/<br/>sessions, messages, interactions,<br/>kb, attachments, outputs"]
        Scope["scope.py<br/>(build_scope / render_scope)"]
        SSE["sse.py<br/>(format_event)"]
    end

    subgraph DataLayer["Data subsystems"]
        KB["kb/<br/>registry + manifest + preprocess + runner"]
        Outputs["outputs/<br/>registry + observer"]
        Attach["attachments_registry"]
    end

    SDK[("Claude Agent SDK<br/>ClaudeSDKClient")]

    CLI --> Runner
    CLI --> Console
    CLI --> App

    App --> State
    App --> Routes
    Routes --> State
    Routes --> Scope
    Routes --> Runner
    Routes --> Web
    Routes --> SSE

    Runner --> SDK
    Runner --> UIProto
    Runner --> Perms
    Runner --> Todos
    Runner --> Subagents
    Runner --> Prompts
    Runner --> Outputs

    Console -.implements.-> UIProto
    Web -.implements.-> UIProto

    State --> KB
    State --> Outputs
    State --> Attach
    Web --> State
```

**Ba seam quan trọng phải bảo toàn:**
1. **`AgentUI` Protocol** (`ui/base.py:19`) — runtime-checkable Protocol; nơi duy nhất mà agent core nói chuyện với UI.
2. **`make_can_use_tool` factory** (`agent/permissions.py:33`) — closure async được SDK gọi cho mọi tool. Là điểm chặn duy nhất cho `AskUserQuestion` + `ExitPlanMode`. Không có MCP server nào nữa.
3. **`TodoStore` + `OutputsObserver`** — passive observers; không tự push event, chỉ trả `bool changed` để runner quyết định gọi UI.

---

## 1. Entry points: `__init__.py`, `__main__.py`, `cli.py`

### `__init__.py` + `__main__.py`
- **Vai trò**: Re-export 3 symbol public của package: `AgentRunner`, `Settings`, `ConsoleAgentUI`. `python -m da_agent` ⇒ `cli.main()`.

### `cli.py` — CLI entrypoint
- **Vai trò**: Routing duy nhất tới 3 mode (`chat`, `demo`, `serve`); không chứa business logic, chỉ wire-up các layer.
- **Public API**:
  - `main()` — parse args, dispatch.
  - `run_chat(settings)` — async REPL: `ConsoleAgentUI` + `AgentRunner` + `prompt_toolkit.PromptSession`; hỗ trợ slash command `/plan` để re-enter plan mode.
  - `run_demo(settings)` — scripted demo (không cần API key); gọi trực tiếp `ui.approve_plan()` / `ui.ask_question()` để chứng minh full UI flow.
  - `run_server(settings, host, port)` — `create_app(settings)` rồi `uvicorn.run(...)`.

---

## 2. `config.py` — Settings + path resolution

- **Vai trò**: Single source of truth cho runtime settings + path resolution. Không phụ thuộc thư viện ngoài stdlib.
- **Public API**:
  - `find_project_root(start?) -> Path` — walk upward tìm dir chứa `.claude/`; fallback `<file>.parents[2]`. SDK cần `cwd` này để discover `.claude/skills/`.
  - `Settings` (dataclass slots):
    - Fields: `model`, `max_turns`, `plan_first`, `show_thinking`, `stream_responses`, `attachment_max_bytes` (100 MB), `scope_warn_bytes` (256 KB), `project_root`, `data_root`.
    - Properties (derived): `kb_dir`, `sessions_dir`, `outputs_dir`, `attachments_dir`, `skills_dir`. `workspace_dir` is retained as a deprecated property only — it is no longer in `add_dirs`, no longer created by `ensure_dirs`, and absent from the system prompt (spec §8.2 — all writes route through `outputs_dir` or `kb_dir/<id>/versions/` or `attachments_dir/<sid>/<att_id>/versions/`).
    - Per-session accessors (thêm từ commit farm):
      - `sessions_data_dir` (property) → `<data_root>/sessions-data/`
      - `session_data_dir(sid)` → `<data_root>/sessions-data/<sid>/`
      - `session_kb_dir(sid)` → `<data_root>/sessions-data/<sid>/kb/`
      - `session_workspace_dir(sid)` → `<data_root>/sessions-data/<sid>/workspace/`
      - `session_outputs_view_dir(sid)` → `<data_root>/sessions-data/<sid>/outputs/` — legacy alias dir; current code does NOT create this symlink (see §7.10).
      - `outputs_session_dir(sid)` → `<data_root>/outputs/<sid>/` — canonical outputs root cho một session.
    - `ensure_dirs() -> None`.
- **Điểm tinh tế**:
  - Env: `CLAUDE_CONFIG_DIR=str(sessions_dir)` được set trong `AgentRunner._build_options()`, **không** trong `Settings` — `Settings` chỉ expose path. Đồng thời FastAPI app lifespan (`server/app.py`) mirror cùng giá trị vào `os.environ` của parent process khi enter và restore khi exit, để `claude_agent_sdk.get_session_messages` (chạy trong parent process khi replay history) đọc đúng JSONL directory mà SDK subprocess ghi vào.
  - `data_root` default `~/.da-agent` (single-user local-first).

---

## 3. `agent/` — Agent core

### 3.1 `agent/events.py`

- **Vai trò**: Plain dataclasses cho mọi structured payload trao đổi giữa agent core ↔ UI. Serializable, không phụ thuộc gì ngoài stdlib.
- **Public types**:
  - Question flow: `Option`, `Question`, `QuestionRequest`, `Answer`, `QuestionResponse`.
  - Plan flow: `PlanVerdict {APPROVE, REJECT}`, `PlanDecision(verdict, feedback)`.
  - Todos: `TodoStatus {PENDING, IN_PROGRESS, COMPLETED}`, `TodoItem`, `TodoSnapshot`.
- **Note**: `TodoItem.display_text` ưu tiên `active_form` khi status là `IN_PROGRESS` và non-empty, ngược lại trả `subject`.

### 3.2 `agent/prompts.py`

- **Vai trò**: Tạo system prompt cho agent. Trả về dict `{"type": "preset", "preset": "claude_code", "append": <text>}` để layer custom instructions trên `claude_code` preset (SDK docs: *Modifying system prompts → Append to the `claude_code` preset*). Append text được structure bằng XML tags (`<role>`, `<environment>`, `<workflow>`, `<output_rules>`, `<trigger_rules>`, `<examples>`, `<output_discipline>`) theo Claude prompting best-practices — direct/imperative voice cho rules, worked examples cho fence cả 2 phía.
- **Public API**: `build_system_prompt(settings: Settings) -> dict`.
- **Tương tác**: chỉ được gọi bởi `AgentRunner._build_options()`.
- **Layout in append**: `kb_dir/<kb_id>/{raw.xlsx, versions/v_curr.<ext>, versions/v_prev.<ext>}` cho KB; memory notes tại `kb_profiler_memory_dir/<kb_id>.md` (ngoài `kb_dir`). `manifest.json` không còn được active pipeline ghi. `attachments_dir/<sid>/<att_id>/{<filename>, versions/v_curr.<ext>, versions/v_prev.<ext>}` cho attachment. Không có "workspace" — đã deprecate (spec §8.2).

### 3.3 `agent/subagents.py`

- **Vai trò**: Định nghĩa 2 subagent specialization (`analyst`, `reporter`) — main agent dispatch qua tool `Agent` (commit `48a54ec`; lưu ý SDK transcript event names `TaskCreate`/`TaskUpdate`/`Task #N created successfully` là SDK-internal và KHÔNG thay đổi). Riêng `kb_profiler` nằm ở `ingestion/profiler.py`, **KHÔNG** nằm trong `build_subagents()` (ingestion-only, không dispatch per-turn).
- **Public API**: `build_subagents() -> dict[str, AgentDefinition]`.
- **Permissions**: `analyst` chỉ có read-only tools + xlsx skill; `reporter` có Write/Edit + xlsx/pptx/docx skills.
- **Reporter prompt structure** (`subagents.py:75–126`) — 4 named sections:
  1. **Target dispatch**: chọn skill `xlsx` / `pptx` / `docx` theo `resolved_target_kind` + extension; ghi ra `resolved_target_path` verbatim.
  2. **Working-directory discipline**: `working_dir=` là scratch; `output_path=` là đường dẫn deliverable cuối — không dùng `/tmp`, không tự bịa đường dẫn.
  3. **`output_path` writability**: harness đã mount parent dir vào sandbox. EROFS / EXDEV tại save time là code bug — KHÔNG fallback sang `cp` / `os.link` / `os.symlink`.
  4. **Language preservation**: khi ngôn ngữ là tiếng Việt, giữ nguyên từng dấu diacritic (`Ngân hàng`, không phải `Ngan hang`); không transliterate, không strip tone marks, không normalize ASCII.

### 3.4 `agent/permissions.py` — `make_can_use_tool`

- **Vai trò**: Implement `can_use_tool` callback cho SDK — gate duy nhất giữa SDK và human input.
- **Public API**: `make_can_use_tool(ask_plan, on_approved, ask_question) -> async callable`. Closure được pass vào `ClaudeAgentOptions.can_use_tool`.

```mermaid
sequenceDiagram
    autonumber
    participant SDK as ClaudeSDKClient
    participant Gate as can_use_tool
    participant UI as AgentUI
    participant Human as Human (CLI/FE)

    Note over SDK,Human: Trường hợp 1 — AskUserQuestion
    SDK->>Gate: can_use_tool("AskUserQuestion", input, ctx)
    Gate->>Gate: QuestionRequest.from_tool_input(input)
    Gate->>UI: await ask_question(request)
    UI->>Human: render selector (CLI) / SSE interaction.requested (Web)
    Human-->>UI: chọn option
    UI-->>Gate: QuestionResponse(answers)
    Gate-->>SDK: PermissionResultAllow(updated_input={questions, answers})
    Note over SDK: SDK short-circuits tool, đẩy answers thẳng làm tool result

    Note over SDK,Human: Trường hợp 2 — ExitPlanMode
    SDK->>Gate: can_use_tool("ExitPlanMode", input, ctx)
    Gate->>UI: await approve_plan(plan_text)
    UI->>Human: render plan approval
    Human-->>UI: APPROVE / REJECT(feedback)
    UI-->>Gate: PlanDecision

    alt APPROVE
        Gate->>SDK: await on_approved()
        Note right of SDK: client.set_permission_mode("acceptEdits")
        Gate-->>SDK: PermissionResultAllow()
    else REJECT
        Gate-->>SDK: PermissionResultDeny(message=feedback, interrupt=False)
        Note right of SDK: agent thấy deny message và tiếp tục
    end

    Note over SDK,Gate: Mọi tool khác → PermissionResultAllow() ngay
```

### 3.5 `agent/todos.py` — TodoStore

- **Vai trò**: Normalize hai họ tool (`Task*` mới và `TodoWrite` legacy) thành một `TodoSnapshot` thống nhất cho UI.
- **Public API**:
  - `TODO_TOOL_NAMES = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}`.
  - `TodoStore`: `reset()`, `is_pending(tool_use_id) -> bool`, `snapshot() -> TodoSnapshot`, `observe_tool_use(...) -> bool`, `observe_tool_result(...) -> bool`.
- **Điểm tinh tế**:
  - `TaskCreate` chưa tạo item lúc `tool_use` — id do SDK assign chỉ xuất hiện ở `tool_result`. Store dùng `_pending_creates` để correlate.
  - `_extract_task_id` accept hai format: regex `r"Task\s*#(\S+)\s+created successfully"` (SDK CLI native) **trước**, JSON object `{"id": ...}` / `{"task": {"id": ...}}` (transport re-encoding) là fallback. Đây là bug-fix có chủ đích — SDK thực tế emit dạng plain string.
  - "Update before create" được synthesize item rỗng để không drop row.
  - `reset()` được runner gọi đầu mỗi turn — todos là per-turn state.

### 3.6 `agent/core.py` — `AgentRunner`

- **Vai trò**: Engine trung tâm. Owns SDK session, dịch raw SDK message stream thành `AgentUI` calls. Không biết gì về `rich` hay `prompt_toolkit`.
- **Public API**:
  - `AgentRunner(ui, settings, *, on_output_detection?, resume_sdk_session_id=None)`.
  - `async __aenter__ / __aexit__` — connect/disconnect `ClaudeSDKClient`.
  - `async send(prompt, *, echo_prompt=True)` — chạy một conversational turn.
  - `async set_plan_mode()` — re-enter plan mode cho turn tiếp theo.
- **Resume seam**: `_build_options()` set `ClaudeAgentOptions(resume=self._resume_sdk_session_id)`. Khi non-None, SDK reuse JSONL transcript hiện có thay vì mint session mới; route layer truyền giá trị này từ `SessionMeta.sdk_session_id` để giữ liên tục transcript qua nhiều lần connect.
- **`add_dirs` shape** (`core.py:188–211`) — hai path tuỳ theo context:
  - **Web path** (`session_id` set): thư mục được mount vào SDK sandbox là:
    ```python
    [session_kb_dir(sid), session_workspace_dir(sid), outputs_session_dir(sid)]
    ```
    `outputs_session_dir(sid)` là `<data_root>/outputs/<sid>/` — canonical path, **KHÔNG** qua symlink alias. Symlink alias (`session_outputs_view_dir`) đã bị remove vì SDK sandbox từ chối (EROFS/EXDEV). Farm machinery tạo/cập nhật các thư mục này — xem §7.10.
  - **CLI fallback** (`session_id is None`): legacy `[kb_dir, outputs_dir, attachments_dir]`.
- **Skills** (`core.py:181`): `["xlsx", "data-analysis"]`. pptx/docx đã chuyển sang reporter subagent (xem §3.3).

#### Sequence: `AgentRunner.send()`

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Caller (CLI / route)
    participant Runner as AgentRunner
    participant Todos as TodoStore
    participant Obs as OutputsObserver
    participant UI as AgentUI
    participant SDK as ClaudeSDKClient

    Caller->>Runner: send(prompt)
    Runner->>UI: on_user_prompt(prompt)
    Runner->>UI: begin_wait("Thinking")
    Note right of Runner: begin_wait TRƯỚC reset todos<br/>(anti-flicker — bug-3 fix)
    Runner->>Todos: reset()
    Runner->>Obs: reset()
    Runner->>UI: on_todos(empty snapshot)
    Runner->>SDK: query(prompt)

    loop async for message in client.receive_response()
        alt SystemMessage
            Runner->>UI: on_system(subtype, data)
        else StreamEvent (parent_tool_use_id is None)
            Runner->>Runner: _handle_stream_event(ev)
            alt content_block_start
                Runner->>Runner: mint block_id<br/>_stream_blocks[idx] = (kind, block_id)
            else content_block_delta
                Runner->>UI: on_text_delta / on_thinking_delta
                Runner->>Runner: _streamed_block_ids.add(block_id)
            else content_block_stop
                Runner->>UI: on_text_end / on_thinking_end<br/>(chỉ khi block_id ∈ _streamed_block_ids)
            end
        else AssistantMessage
            loop pos, block in enumerate(content)
                alt suppression: main thread + block_id ∈ _streamed_block_ids
                    Runner-->>Runner: skip (đã streamed token-level)
                else
                    Runner->>Runner: _render_block(block, depth)
                    alt ThinkingBlock
                        Runner->>UI: on_thinking(text)
                    else TextBlock
                        Runner->>UI: on_text(text)
                    else ToolUseBlock — TODO tool
                        Runner->>Todos: observe_tool_use(...)
                        Runner->>UI: on_todos(snapshot) (nếu changed)
                    else ToolUseBlock — interactive
                        Runner-->>Runner: skip (đã handle bởi can_use_tool)
                    else ToolUseBlock — khác
                        Runner->>Obs: observe_tool_use(id, name, input)
                        Runner->>UI: on_tool_use(name, input, depth, tool_use_id)
                        Runner->>UI: begin_wait("Running {tool_name}")
                    end
                end
            end
        else UserMessage (tool_result blocks)
            loop block in content
                alt pending todo
                    Runner->>Todos: observe_tool_result(...)
                    Runner->>UI: on_todos(snapshot) (nếu changed)
                else
                    Runner->>Obs: observe_tool_result(id, summary, is_error)
                    Note right of Obs: phát detect → on_detect callback
                    Runner->>UI: on_tool_result(summary, is_error, depth, tool_use_id)
                end
            end
        else ResultMessage
            Runner->>UI: on_result(turns, cost_usd, duration_s)
        end
    end

    Runner->>UI: end_wait()
```

**Điểm tinh tế** (mọi rule dưới đây được test cover trong `tests/test_streaming.py`):
- `_stream_blocks: dict[idx, (kind, block_id)]` — key là **SDK content-block index**, **persist cả turn** (KHÔNG pop tại `content_block_stop`). Lý do: trailing `AssistantMessage` cần lookup positional để suppress re-render.
- Suppression rule: block bị skip khi và chỉ khi `block_id ∈ _streamed_block_ids`. Block có start+stop nhưng **zero deltas** vẫn render atomic — đây là lý do counter-based approach hỏng (corrupted khi có gap-block giữa sequence).
- Subagent stream events (`parent_tool_use_id != None`) bị drop — token-level streaming trong subagent lane defer tới v1.1.
- `input_json_delta`, `signature_delta`, empty deltas, thinking deltas khi `show_thinking=False` đều bị drop.
- `on_output_detection` truyền vào constructor; CLI pass `None` → observer thành silent sink. Server wire callback vào `AppState.outputs`.

---

## 4. `ui/` — UI Protocol seam

### 4.1 `ui/base.py` — `AgentUI` Protocol

- **Vai trò**: `@runtime_checkable Protocol` định nghĩa toàn bộ contract giữa agent core và UI. Đây là seam **duy nhất** — agent core không bao giờ import `rich` / `prompt_toolkit` / FastAPI.
- **Methods**:
  - **Atomic render** (sync): `on_user_prompt`, `on_thinking`, `on_text`, `on_tool_use`, `on_tool_result`, `on_system`, `on_result`, `on_error`, `on_output`, `on_todos`.
  - **Token streaming** (sync, spec §8.6): `on_text_delta(block_id, delta)`, `on_text_end(block_id)`, `on_thinking_delta(block_id, delta)`, `on_thinking_end(block_id)`. `block_id` là server-minted opaque (`txt_<12hex>` / `thk_<12hex>`).
  - **Wait label** (sync): `begin_wait(label)`, `end_wait()`.
  - **Interactive** (async, block on human): `ask_question(request) -> QuestionResponse`, `approve_plan(plan) -> PlanDecision`.

### 4.2 `ui/console.py` — `ConsoleAgentUI`

- **Vai trò**: Implement `AgentUI` cho CLI dùng `rich` (render) + `prompt_toolkit` (interaction). Quản lý `rich.Live` overlay duy nhất cho spinner + todo list.
- **Điểm tinh tế**:
  - Token streaming flush tại `*_end` bằng cách gọi atomic `on_text` / `on_thinking` — inline streaming sẽ conflict với `rich.Live` re-render.
  - Spinner label bị override bởi `todos.in_progress.display_text` nếu có todo đang chạy.
  - `on_output()` là no-op trong CLI — file đã trên disk, user tự tìm theo path đã print.

### 4.3 `ui/prompts.py`

- **Vai trò**: TUI cho question selector (multi-tab multi-choice) và plan approval, dùng `prompt_toolkit`. Inline (non-fullscreen).
- **Public API**: `run_question_selector(request)`, `confirm_plan()`, `build_selector_app(questions)` (tách riêng để test có thể drive mà không cần TTY).
- **Fallback**: `_fallback_select()` plain `input()` khi không có TTY (pipe/demo).

---

## 5. `outputs/` — Outputs detection + registry

### 5.1 `outputs/observer.py` — `OutputsObserver`

- **Vai trò**: Passive observer, observe Write/Edit/Bash tool calls để detect output files. Flat layout: accept paths dưới `outputs/<session_id>/<filename>` (canonical `outputs_session_dir(sid)`). Phân loại thành:
  - `standalone` — applies cho cả 5 targets (`New .xlsx`, `New .pptx`, `New .docx`, `New sheet`, `Pick sheet`); kind metadata trong registry vẫn track `kb_version` / `attachment_version` cho UI grouping nhưng path luôn là session-scoped.
  - Legacy branches (`kb_dir/<kb_id>/versions/v<N>.xlsx`, `attachments/<sid>/<att_id>/versions/`) deprecated — `_classify` return None.
  - Ambiguous: bỏ qua.
- **Public API**:
  - `OutputDetection(kind, file_path, output_id?, filename?, kb_id?, version?)` — dataclass.
  - `OutputsObserver(outputs_dir, kb_dir, on_detect)`:
    - `reset()` — clear pending + fired (gọi đầu mỗi turn).
    - `observe_tool_use(id, name, input)` — stage Write/Edit/Bash nếu match.
    - `observe_tool_result(id, content, is_error)` — pop pending, classify, gọi `on_detect(detection)`.

```mermaid
flowchart TD
    Start[ToolUseBlock] --> Filter{name ∈ Write,Edit,Bash?}
    Filter -- no --> Drop[ignore]
    Filter -- yes --> Stage["_pending[id] = candidate path(s)"]
    Stage --> AwaitResult[chờ tool_result]
    AwaitResult --> Result[ToolResultBlock]
    Result --> Err{is_error?}
    Err -- yes --> Discard[pop _pending, không fire]
    Err -- no --> Pop[pop _pending → candidates]
    Pop --> Loop[for path in candidates]
    Loop --> Cls[_classify resolved path]
    Cls --> StandCheck{nằm dưới outputs_dir<br/>và parts0 prefix out_?}
    StandCheck -- yes --> Stand["OutputDetection<br/>kind=standalone<br/>output_id, filename"]
    StandCheck -- no --> KbCheck{nằm dưới kb_dir<br/>khớp kb_id/versions/v_N.xlsx?}
    KbCheck -- yes --> Kb["OutputDetection<br/>kind=kb_version<br/>kb_id, version"]
    KbCheck -- no --> NoFire[không fire]
    Stand --> Fire[on_detect detection]
    Kb --> Fire
    Fire --> AddFired[_fired.add id<br/>guard duplicate]
```

- **Điểm tinh tế**:
  - `_fired` set guard chống duplicate emission khi SDK forward tool_result nhiều lần.
  - **Detection hai lượt**:
    - **Pass 1 (regex)**: `_BASH_REDIR_RE` match các shell redirect rõ ràng, chỉ nhận absolute path bắt đầu `/`; negative-lookbehind loại bỏ FD redirect (`2>&1`, `1>…`, `>&1`):
      ```
      _BASH_REDIR_RE = re.compile(r"(?:(?<![\d&])>{1,2}\s*|--output[= ])(/[^\s;|&<>]+)")
      ```
    - **Pass 2 (dir-snapshot diff)**: chụp snapshot thư mục trước và sau tool_result; diff để bắt các file được tạo bởi python heredoc / `shutil.copy` / `wb.save()` — những trường hợp không có shell redirect.
    - Cả hai pass đều chạy qua `_classify`, dùng `Path.resolve(strict=False)` để symlinked paths resolve về canonical inode.
  - Observer là sync; server wrap `on_detect` thành async bridge để register vào `OutputsRegistry` + emit SSE.

### 5.2 `outputs/registry.py` — `OutputsRegistry`

- **Vai trò**: Persistent registry cho standalone outputs. Layout (Phase A / flat): `outputs/<session_id>/<filename>` + hidden sidecar `.<output_id>.meta.json`. Collision-bump nếu tên trùng: `report.xlsx` → `report (2).xlsx`. `registry.json` ở top-level `outputs_dir`; per-session dir tạo lazy lúc register. (Layout system-level đầy đủ — xem `technical-spec.md §4`.)
- **Public API**:
  - `OutputMeta(id, kind, title, filename, mime, size_bytes, source_session_id, source_kb_ids, created_at)`.
  - `OutputsRegistry(root)`: `load()`, `list(*, session_id?)`, `get(output_id)`, `register_standalone(..., session_id)`, `adopt_at(*, output_id, ...)`, `delete(output_id)`, `delete_session_outputs(session_id)`.
  - `register_standalone(..., session_id)` — yêu cầu `session_id`; file land tại `outputs/<session_id>/<filename>`.
  - `delete_session_outputs(session_id)` — recursively remove `outputs/<session_id>/` + bulk-remove registry rows match `session_id`.
- **Điểm tinh tế**:
  - Atomic write: flush dùng `.tmp` rồi `os.replace`.
  - `_lock = asyncio.Lock()` — mọi mutation serialize.
  - `adopt_at()` dùng khi model write trực tiếp vào path `outputs_dir/<session_id>/<filename>` mà server chưa mint id đó — idempotent: id đã tồn tại thì trả existing row.

---

## 6. `kb/` + `ingestion/` — Knowledge Base pipeline

Active pipeline: `ingestion/` (memory-driven, `kb_profiler` opus subagent). Legacy/inactive: `kb/preprocess.py` + `kb/runner.py` + `kb/manifest.py` — vẫn on disk nhưng **KHÔNG** được server import. `server/routes/kb.py` imports from `...ingestion` only.

### 6.1 `ingestion/registry.py` — `KbRegistry`

- **Vai trò**: Quản lý `kb/registry.json` + state machine `PENDING → PROFILING → READY | READY_PARTIAL | FAILED`.
- **Concurrency cap**: `_PROFILE_LOCK = Semaphore(1)` — at most one profiler runs at a time across the host.
- **Crash recovery (rất quan trọng)**: `load()` quét bất kỳ row nào status `PROFILING` và rewrite thành `FAILED, error="interrupted by restart"` rồi persist ngay. Legacy `PROCESSING` rows từ registries cũ cũng reset to `FAILED` cho forward-compat. FE/user phải re-upload hoặc reprofile.
- **API**: `load`, `list`, `get`, `create`, `delete`, `update_status`. `update_status(... "FAILED", error=msg)` chỉ set `meta.error` khi status là FAILED; transition khác clear error về None.

### 6.2 `ingestion/profiler.py` — `kb_profiler` subagent definition

- **Vai trò**: Định nghĩa `kb_profiler` opus subagent — ingestion-only, **KHÔNG** nằm trong `build_subagents()` (không dispatch per-turn).
- **Output**: Free-form **markdown memory note** ghi tại `kb_profiler_memory_dir/<kb_id>.md` (default `~/.da-agent/agent-memory/kb_profiler/`).
- **Model**: opus-class (default `DA_AGENT_KB_PROFILER_MODEL`).
- **Permissions**: read-only tools (Read/Bash/Glob/Grep + xlsx skill); explicitly NOT allowed to write outside the memory dir.

### 6.3 `ingestion/runner.py` — orchestrator

- **Vai trò**: Async orchestrator cho per-file profiling. Status transitions, error handling, write memory note path.
- **API**: `run_pipeline(*, registry, kb_root, kb_id) -> None` — fire-and-forget task.
- **Status flow**: `PENDING → acquire(_PROFILE_LOCK) → PROFILING → kb_profiler subagent runs → READY | READY_PARTIAL | FAILED`.
- **Error handling**: Exception bị catch-all và ghi vào registry. Per-sheet failures land trên `READY_PARTIAL`. Lifecycle owned bởi `AppState._kb_tasks` — `shutdown()` cancel chúng.

### 6.5 KB ingestion sequence

```mermaid
sequenceDiagram
    autonumber
    participant FE as Client
    participant Route as routes/kb.py
    participant Reg as KbRegistry
    participant FS as Filesystem
    participant Pipe as run_pipeline (background)
    participant PP as kb_profiler (opus subagent)

    FE->>Route: POST /kb/files (multipart)
    Route->>Route: _sanitize_filename + validate ext
    Route->>FS: stream to kb/_tmp/upload_{uuid}.bin -- cap 500MB or 413, size 0 or 400
    Route->>Reg: create(filename, size_bytes)
    Reg-->>Route: KbMeta(status=PENDING)
    Route->>FS: shutil.move(tmp, kb/{kb_id}/raw.xlsx)
    Route->>Pipe: asyncio.create_task(run_pipeline)
    Route->>FE: 202 KbFileResponse(status=PENDING)

    Pipe->>Reg: update_status(kb_id, PROFILING)
    Pipe->>PP: kb_profiler subagent runs, writes <kb_id>.md memory note
    alt success
        Pipe->>Reg: update_status(kb_id, READY)
    else partial
        Pipe->>Reg: update_status(kb_id, READY_PARTIAL)
    else exception
        Pipe->>Reg: update_status(kb_id, FAILED, error)
    end

    Note over FE,Reg: FE poll GET /kb/files/{kb_id} đến khi status = READY, READY_PARTIAL, hoặc FAILED
```

### 6.6 Legacy (inactive — kept for backcompat, NOT imported)

The following modules remain on disk but the active server does not import them. `server/routes/kb.py` imports from `...ingestion` only. Active replacement: the memory-driven `ingestion/` pipeline (kb_profiler opus subagent) — see §6.1.

#### `kb/registry.py` (legacy)

Older deterministic-pipeline registry. Superseded by `ingestion/registry.py`.

#### `kb/manifest.py` (legacy)

- Schema dataclass + atomic IO cho `manifest.json`.
- **Dataclasses**: `Column`, `Region`, `SheetSummary`, `Relationship`, `Manifest`.
- **Functions**: `write_manifest_atomic(path, manifest)` (`.tmp` + `os.replace`), `read_manifest(path) -> dict`.
- **Quirk**: `Relationship.from_` ↔ JSON key `"from"`; `to_dict()` rename để giữ wire format chuẩn.
- **Status**: NOT written by the active pipeline. `manifest.json` is deprecated; the active artifact is the markdown memory note at `kb_profiler_memory_dir/<kb_id>.md`.

#### `kb/preprocess.py` (legacy)

- Sync blocking pipeline chạy trong executor thread — đọc `.xlsx`, profile từng sheet thành `Manifest`.
- **Entry point**: `build_manifest(raw_path, kb_id) -> Manifest`.
- **Stages**: `load_workbook` → `_materialize_sheet` → `_detect_regions` → `_infer_header_row` → `_collapse_multirow_headers` → `_profile_column` → `_infer_relationships`.
- **Tunables (monkey-patchable trong tests)**: `PROFILE_ROW_CAP`, `SAMPLE_ROW_LIMIT`, `SAMPLE_VALUE_LIMIT`, `MIN_REGION_AREA`, `FK_MIN_CONFIDENCE`, `HEADER_STRING_RATIO`.
- **Status**: NOT invoked by the active pipeline.

#### `kb/runner.py` (legacy)

- Async orchestrator cho legacy deterministic pipeline.
- **API**: `run_pipeline(*, registry, kb_root, kb_id) -> None`.
- **Status**: NOT scheduled by the active server. Active runner is `ingestion/runner.py`.

---

## 7. `server/` — FastAPI layer

### 7.1 `server/app.py` — Factory

- **Vai trò**: Tạo `FastAPI`, mount routers, setup CORS, điều phối lifespan.
- **Public API**: `create_app(settings: Settings | None) -> FastAPI`.
- **Lifespan**: trên enter — set `os.environ["CLAUDE_CONFIG_DIR"] = str(settings.sessions_dir)` (lưu giá trị cũ để restore) rồi `AppState(settings)` → `await registry.load()` → `await kb.load()` (sweep `PROFILING → FAILED`; legacy `PROCESSING` rows cũng reset to `FAILED` for forward-compat) → `await outputs.load()` → gắn vào `app.state.app_state`. On shutdown: `await state.shutdown()` cancel KB tasks + discard runtimes; sau đó restore `CLAUDE_CONFIG_DIR` về giá trị cũ (hoặc `pop` nếu trước đó không có).
- **Lý do mirror env**: `claude_agent_sdk.get_session_messages` (dùng cho history replay) chạy trong parent FastAPI process và đọc `CLAUDE_CONFIG_DIR` từ `os.environ` — nếu không mirror, nó sẽ tìm trong `~/.claude/projects/` thay vì `settings.sessions_dir/projects/` nơi SDK subprocess thực sự ghi JSONL.
- **CORS**: `http://127.0.0.1:3000` + `http://localhost:3000`, methods/headers `*`, `allow_credentials=False`.
- **Routers**: `sessions`, `messages`, `interactions`, `attachments` (cùng prefix `/sessions`); `kb`, `outputs`. Inline `GET /health`.

### 7.2 `server/state.py` — In-process state

| Thành phần | Vai trò |
|---|---|
| `SessionMeta` / `SessionRegistry` | Dataclass + JSON registry trên disk; atomic-rename write; `asyncio.Lock`. Corrupt JSON khi `load()` → reset `_items.clear()` (không crash). `SessionMeta` có thêm field `sdk_session_id: str \| None = None` — UUID do SDK mint, captured từ `SystemMessage(subtype="init")` của turn đầu; persist qua `to_dict`/`from_dict` và registry cũ load với `None` (backward-compat). Method `set_sdk_session_id(sid, uuid) -> bool` idempotent: trả `False` mà không flush nếu giá trị unchanged, ngược lại update + atomic flush qua `_flush_locked` hiện có (`.tmp` + `os.replace`). |
| `PendingInteraction` / `InteractionStore` | `dict[sid, dict[tool_use_id, PendingInteraction]]`. `resolve` pop **trước** khi `set_result` (tránh race với re-park cùng id). |
| `TurnStream` | Per-turn `asyncio.Queue` unbounded; sentinel-closed; `emit` sau `_closed=True` là no-op (safe trong exception handler). |
| `SessionRuntime` | Lazy `runner` + `ui` + `lock: asyncio.Lock` — serialize turns trong cùng session. |
| `AppState` | Aggregate `registry`, `interactions`, `kb`, `attachments`, `outputs`. Có `_runtimes` + double-checked locking; `_kb_tasks` set tracked-cancel; `discard_runtime`; `shutdown`. |

### 7.3 `server/sse.py`

- `format_event(event: dict) -> str` — trả `"event: {type}\ndata: {json}\n\n"`. JSON với `default=str, ensure_ascii=False`.

### 7.4 `server/schemas.py` — Pydantic models

| Group | Models |
|---|---|
| Sessions | `CreateSessionRequest` (default `name="Untitled"`), `RenameSessionRequest`, `ForkSessionRequest`, `SessionResponse`, `SessionListResponse`, `MessageHistoryResponse(events: list[dict[str, Any]])` (replay payload cho `GET /sessions/{sid}/messages`, mỗi entry là wire-shape SSE event dict). |
| Messages | `MessageRequest(prompt, kb_scope?, attachments=[])`, `AttachmentRef(attachment_id)`. |
| Interactions | `AnswerSubmission`, `QuestionResponseSubmission`, `PlanResponseSubmission(verdict: Literal["approve","reject"])`, `PendingInteractionResponse`, `PendingInteractionsListResponse`. |
| KB | `KbFileResponse(status: Literal[...])`, `KbFileListResponse`, `KbVersionResponse`, `KbVersionListResponse`. |
| Attachments | `AttachmentResponse(attachment_id, filename, size_bytes, mime, uploaded_at)`, `AttachmentListResponse`. |
| Outputs | `OutputResponse(kind: Literal["standalone","kb_version"], …)`, `OutputListResponse`. |

### 7.5 `server/scope.py` — `build_scope` / `render_scope`

- **Public API**:
  - `async build_scope(*, state, sid, body) -> ScopeBlock` — raise `HTTPException(400, detail={"error": ...})`.
  - `render_scope(block, prompt) -> str` — compose `<scope>…</scope>` + `<user_prompt>…</user_prompt>`.

#### Validation table (fail-fast theo thứ tự)

| # | Điều kiện | HTTP 400 `detail.error` |
|---|---|---|
| 1 | `kb_id` không tồn tại | `"unknown kb_id: <id>"` |
| 2 | `kb_id` tồn tại, status ≠ READY | `"kb <id> is in status <X>; only READY files can be scoped"` |
| 3 | `attachment_id` trùng lặp | `"duplicate attachment_id"` |
| 4 | `attachment_id` không tồn tại / file bị xóa | `"unknown attachment_id: <id>"` |

- **Default-empty**: `kb_scope: null` (omit) và `kb_scope: []` đều tương đương — cả hai produce empty `<scope>` block; BE không tự load READY KBs. FE phải list explicit `kb_id`s nếu muốn KB context.
- **Soft-warn**: tổng `manifest_bytes > settings.scope_warn_bytes` → log WARNING, không raise.

#### Render output shape

```text
<scope>
For this turn, only these KB files are in scope:
- kb_<id> (<filename>) — manifest at <abs_path>

Short-term attachments (no manifest, read directly with xlsx skill):
- <abs_attachment_path>
</scope>

<user_prompt>
<original user text>
</user_prompt>
```

### 7.6 `server/attachments_registry.py`

- Per-session attachment store: `attachments/<sid>/registry.json`. Lazy load qua `_ensure_loaded(sid)` dưới `_lock` (chống double-read).
- API: `list/get/create/delete/delete_session/path_for/session_dir`.
- `delete_session` dùng `asyncio.to_thread(shutil.rmtree, …)` — không block event loop.
- Atomic-rename write; corrupt JSON → silent reset.

### 7.7 `server/web_ui.py` — `WebAgentUI`

- **Vai trò**: Implement `AgentUI` Protocol bằng cách push events vào `TurnStream` (sync, fire-and-forget) và park `asyncio.Future` cho async interactions.
- **Public API**: `WebAgentUI(*, session_id, app_state, on_sdk_session_id: Callable[[str], None] | None = None)`; `attach(stream)` / `detach()` / `_emit(type, **data)`.
- **Latch `_text_delta_seen`**: lần đầu có text delta trong turn → emit `wait.end` ngay trước `assistant.text.delta`. Latch reset trong cả `attach()` và `detach()` — per-turn scoped.
- **Server-minted tool_use_id**: dùng prefix `int_<12hex>` cho parked interactions, **không phải** SDK `toolu_…`. FE round-trip ID này mà thôi.
- **`on_system(subtype, data)` capture**: khi `subtype == "init"` và `data["session_id"]` là non-empty string, gọi `on_sdk_session_id(sdk_uuid)` **trước** khi emit SSE `system` event — để route layer kịp persist UUID vào `SessionMeta` cho lần resume kế tiếp.
- `ask_question`/`approve_plan`: park `PendingInteraction(future)`, emit `interaction.requested`, `await future`. Khi `CancelledError` (session bị xóa): trả empty `QuestionResponse(answers=[])` hoặc `PlanDecision(REJECT, "cancelled")`.

### 7.8 `server/routes/` — Endpoint table

| Method | Path | Status | Module | Notes |
|---|---|---|---|---|
| GET | `/health` | 200 | app.py | `{ok: true}` |
| GET | `/sessions` | 200 | sessions.py | sort updated_at desc |
| POST | `/sessions` | 201 | sessions.py | |
| GET | `/sessions/{sid}` | 200 / 404 | sessions.py | |
| PATCH | `/sessions/{sid}` | 200 / 404 | sessions.py | rename |
| DELETE | `/sessions/{sid}` | 204 / 404 | sessions.py | gọi `outputs.delete_session_outputs(sid)` + `discard_runtime(sid)` + `shutil.rmtree(session_data_dir(sid))` (xoá per-session farm) |
| POST | `/sessions/{sid}/fork` | 201 / 404 | sessions.py | tạo child với `parent_id` |
| POST | `/sessions/{sid}/messages` | 200 SSE / 400 / 404 | messages.py | StreamingResponse `text/event-stream` |
| GET | `/sessions/{sid}/messages` | 200 / 404 | sessions.py | `MessageHistoryResponse(events: list[dict])`. Empty events nếu `sdk_session_id is None`. Đọc JSONL qua `claude_agent_sdk.get_session_messages(uuid)` (`directory=None` để scan mọi project dir) → `replay_to_events`. |
| GET | `/sessions/{sid}/interactions/pending` | 200 / 404 | interactions.py | |
| POST | `/sessions/{sid}/interactions/{tool_use_id}/respond` | 204 / 404 / 409 | interactions.py | 409 nếu đã resolved |
| POST | `/sessions/{sid}/attachments` | 201 / 400 / 404 / 413 | attachments.py | multipart |
| GET | `/sessions/{sid}/attachments` | 200 / 404 | attachments.py | |
| DELETE | `/sessions/{sid}/attachments/{att_id}` | 204 / 404 | attachments.py | |
| POST | `/kb/files` | 202 / 400 / 413 | kb.py | fire-and-forget pipeline |
| GET | `/kb/files` | 200 | kb.py | |
| GET | `/kb/files/{kb_id}` | 200 / 404 | kb.py | |
| GET | `/kb/files/{kb_id}/manifest` | 200 / 404 / 409 | kb.py | 409 nếu status ≠ READY |
| DELETE | `/kb/files/{kb_id}` | 204 / 404 | kb.py | rmtree dir |
| GET | `/kb/files/{kb_id}/versions` | 200 / 404 | kb.py | scan + sidecar |
| GET | `/kb/files/{kb_id}/versions/{version}/download` | 200 / 400 / 404 | kb.py | FileResponse |
| POST | `/kb/files/import-sheet` | 501 | kb.py | stub |
| GET | `/outputs` | 200 | outputs.py | optional `?session_id=` |
| GET | `/outputs/{output_id}/meta` | 200 / 404 | outputs.py | |
| GET | `/outputs/{output_id}` | 200 / 404 | outputs.py | FileResponse download |
| DELETE | `/outputs/{output_id}` | 204 / 404 | outputs.py | |

### 7.9 `server/replay.py` — Session-history replay

- **Vai trò**: Dịch SDK historical messages (đọc qua `claude_agent_sdk.get_session_messages`) thành wire-format SSE event dicts để FE fold replay qua chính `streamReducer` của live stream — không cần code path riêng.
- **Public API**: `replay_to_events(messages: list[SessionMessage], sid: str) -> list[dict[str, Any]]`. Trả `[]` khi `messages` rỗng.
- **`_INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode"} | TODO_TOOL_NAMES`**: tool_use blocks với name nằm trong set này bị filter ra (mirror suppression của live runner — UI surface dedicated render chúng, không phải ordinary tool steps).
- **Block mapping**:
  - `assistant.text` ← `TextBlock` (skip whitespace-only)
  - `assistant.thinking` ← `ThinkingBlock` (skip whitespace-only)
  - `tool.use` ← `ToolUseBlock` (filtered bởi `_INTERACTIVE_TOOLS`)
  - `tool.result` ← `ToolResultBlock` embedded trong user role
  - `user.prompt` ← user role với str content
- **Synthetic `result` events**: insert giữa các user-prompt boundary và một lần ở cuối — để FE reducer flip `inToolChain=false` và mark thinking blocks `done` (mirror live `ResultMessage` boundary). Payload `{turns, cost_usd: None, duration_s: 0.0}` vì historical run không lưu cost/duration.

### 7.10 `server/session_farm.py` — per-session symlink farm

- **Vai trò**: Gate quyền truy cập filesystem của SDK per turn. Mỗi session được cấp một "farm" gồm `kb/`, `workspace/`, và canonical `outputs/<sid>/` làm riêng — đây là permission gate hoạt động thực sự; `<scope>` prose block chỉ là hint cho model.
- **Module**: 92 dòng tại `server/session_farm.py`.
- **Hai hàm chính**:
  - `prepare_session_root(settings, sid)` — idempotent mkdir `<sessions-data>/<sid>/{kb, workspace}` và `<outputs_dir>/<sid>/`. Nếu legacy symlink `session_outputs_view_dir(sid)` tồn tại, tears down ngay (symlink này không còn được tạo).
  - `rebuild_kb_symlinks(settings, sid, scope_block)` — diff symlinks hiện có dưới `session_kb_dir(sid)` với desired set từ `scope_block.kb_entries`; `os.symlink` cho additions, `unlink` cho removals (chỉ unlink nếu entry là symlink — không xoá real dir).
- **Lifecycle wire-in**:
  - `routes/messages.py::post_message` gọi `prepare_session_root` TRƯỚC `build_scope`.
  - `_stream_turn` gọi `rebuild_kb_symlinks` sau `_ensure_runner` và trước `runner.send(prompt)`, dưới `runtime.lock`.
- **Lưu ý quan trọng**: `outputs_session_dir(sid)` (`<data_root>/outputs/<sid>/`) được list trực tiếp trong `add_dirs` — KHÔNG qua symlink. Legacy `session_outputs_view_dir` symlink bị SDK sandbox từ chối (EROFS/EXDEV cross-device) và đã bị remove.
- **Cleanup**: `DELETE /sessions/<sid>` gọi `shutil.rmtree(session_data_dir(sid))` để xoá toàn bộ per-session farm.

---

## 8. SSE & Message turn — Flow tổng hợp

### 8.1 Sequence: `POST /sessions/{sid}/messages`

```mermaid
sequenceDiagram
    autonumber
    participant FE as Client
    participant Route as routes/messages.py
    participant State as AppState
    participant Scope as scope.py
    participant Runtime as SessionRuntime
    participant UI as WebAgentUI
    participant Stream as TurnStream
    participant Runner as AgentRunner
    participant SDK as Claude SDK

    FE->>Route: POST /sessions/{sid}/messages<br/>{prompt, kb_scope?, attachments?}
    Route->>State: get_or_create_runtime(sid)
    alt session not found
        Route-->>FE: 404
    end
    Route->>Scope: build_scope(state, sid, body)
    alt invalid scope
        Scope-->>Route: HTTPException(400, detail.error)
        Route-->>FE: 400 {"detail":{"error":"…"}}
    end
    Scope-->>Route: ScopeBlock
    Route->>Scope: render_scope(block, prompt)
    Scope-->>Route: composed_prompt
    Route-->>FE: 200 StreamingResponse text/event-stream

    Route->>Runtime: acquire runtime.lock
    Route->>Runtime: _ensure_runner()<br/>(lazy create WebAgentUI + AgentRunner)
    Route->>State: registry.touch(sid)
    Route->>Stream: TurnStream() new
    Route->>UI: attach(stream)
    Route->>Runner: asyncio.create_task(send(composed_prompt))

    par runner_task
        Runner->>SDK: query + receive_response
        loop events
            Runner->>UI: on_text / on_tool_use / on_todos / ...
            UI->>Stream: emit({type, ...})
        end
        Runner->>Stream: close() [SENTINEL]
    and stream_pump (route generator)
        loop async for item in stream
            Route->>FE: yield format_event(item) (SSE chunk)
        end
    end

    alt client disconnect (CancelledError)
        Route->>Runner: task.cancel()
        Route->>Stream: ensure close
        Route->>UI: detach()
        Route->>Runtime: release lock
    end
```

**Chi tiết `_ensure_runner` (session-history wiring)**:
- Route layer build closure `on_sdk_session_id(sdk_uuid)` → schedule `state.registry.set_sdk_session_id(sid, sdk_uuid)` qua `asyncio.create_task` (vì callback chạy sync trong runner task, phải hop về async registry). Task được `add_done_callback(lambda t: t.exception())` để swallow exception, tránh "Task exception was never retrieved" warning lúc shutdown.
- `WebAgentUI` được khởi tạo với kwarg `on_sdk_session_id=on_sdk_session_id`; `AgentRunner` được khởi tạo thêm kwarg `resume_sdk_session_id=runtime.meta.sdk_session_id` — nếu meta đã có UUID từ phiên trước, `ClaudeAgentOptions(resume=...)` sẽ resume cùng JSONL transcript thay vì mint session mới.

### 8.2 Inventory các SSE event được emit

| Event `type` | Nguồn | Mô tả ngắn |
|---|---|---|
| `user.prompt` | `WebAgentUI.on_user_prompt` | echo user text |
| `assistant.text` | `on_text` | atomic text (fallback khi không stream) |
| `assistant.text.delta` / `.end` | streaming | token-level (spec §8.6) |
| `assistant.thinking` / `.delta` / `.end` | thinking | tương tự text |
| `tool.use` | `on_tool_use` | bao gồm `tool_use_id` |
| `tool.result` | `on_tool_result` | pair với `tool_use_id` |
| `system` | `on_system` | SDK system messages |
| `result` | `on_result` | turn done — turns/cost/duration |
| `error` | `on_error` | error message |
| `todos.snapshot` | `on_todos` | full snapshot, không phải delta |
| `wait.begin` / `wait.end` | `begin_wait`/`end_wait` | wait label |
| `interaction.requested` | `ask_question`/`approve_plan` | parked future, FE renders modal |
| `interaction.resolved` | emitted sau khi `/respond` xử lý xong | FE pop `pendingInteractions[tool_use_id]` để đóng modal |
| `output.created` | `on_output` (server bridge từ observer) | mới detect output file |

---

## 9. Crash recovery & restart safety

```mermaid
flowchart LR
    Boot[App lifespan startup] --> LoadSess[SessionRegistry.load]
    Boot --> LoadKB[KbRegistry.load]
    Boot --> LoadOut[OutputsRegistry.load]
    LoadSess --> CorruptSess{JSON corrupt?}
    CorruptSess -- yes --> ResetSess[clear _items<br/>boot sạch]
    CorruptSess -- no --> OkSess[ready]
    LoadKB --> Sweep["scan rows status PROFILING (also legacy PROCESSING)"]
    Sweep --> Rewrite["rewrite → FAILED<br/>error = interrupted by restart"]
    Rewrite --> PersistKB[atomic flush registry.json]
    LoadOut --> OkOut[ready]
    OkSess --> Ready[server up]
    PersistKB --> Ready
    OkOut --> Ready
```

- KB pipeline đang dở khi server down → row đó bắt buộc phải FAILED sau khi reload — frontend không bao giờ thấy KB "stuck" trong PROFILING (hoặc legacy PROCESSING) vô thời hạn.
- Atomic-rename pattern `.tmp` + `os.replace` được dùng đồng nhất ở 4 nơi: `SessionRegistry`, `KbRegistry`, `AttachmentsRegistry`, `OutputsRegistry`, và `write_manifest_atomic`.

---

## 10. Map tóm tắt các seam mở rộng

Khi cần thêm tính năng mới, dùng bảng dưới đây để xác định seam đúng:

| Yêu cầu mới | Đặt ở đâu |
|---|---|
| Tool tương tác mới (block agent đợi human) | Đăng ký vào `_INTERACTIVE_TOOLS` (`agent/core.py`) **+** thêm callable vào `make_can_use_tool` **+** thêm method trên `AgentUI` Protocol **+** implement cho cả `ConsoleAgentUI` và `WebAgentUI`. |
| Tool theo dõi task state | Mở rộng `TodoStore.observe_*` + thêm tên vào `TODO_TOOL_NAMES`. |
| Output kind mới | Thêm logic trong `OutputsObserver._classify` + path layout convention + `OutputMeta.kind` literal. |
| SSE event mới | Thêm method trên `AgentUI` (sync) → `WebAgentUI._emit` → ghi vào docs `technical-spec.md` §11 → FE forward-compat unknown-event handler đã sẵn. |
| Endpoint mới | Tạo `routes/<topic>.py` với `APIRouter`; include trong `app.py`; schema vào `server/schemas.py`; nếu cần state mới — thêm field vào `AppState`. |
| KB pipeline stage mới | Thêm function trong `kb/preprocess.py`; expose tunable nếu cần test monkey-patch; cập nhật `Manifest` dataclass. |
| Validation rule cho `messages` | Mở rộng table trong `scope.build_scope` (ưu tiên fail-fast trước khi spawn runner). |
| Per-session filesystem scope mới | Cập nhật `prepare_session_root` / `rebuild_kb_symlinks` trong `server/session_farm.py` (§7.10); nếu thêm thư mục mới, cũng cập nhật `add_dirs` trong `agent/core.py`. |

---

**Cuối tài liệu.** Mọi sơ đồ Mermaid ở trên là valid syntax (kiểm tra theo Mermaid `flowchart`/`sequenceDiagram` v10+); không dùng ký tự đặc biệt `()`/`/` trong node label nếu không quote bằng `[" … "]`.
