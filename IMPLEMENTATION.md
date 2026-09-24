# AI-Powered OS Management Agent - Implementation Tracker

## Project Configuration
- **Primary LLM**: LM Studio (Qwen 3.5-9B) - local, unlimited, no rate limits
- **Fallback LLMs**: Groq (llama-3.3-70b-versatile) → Gemini (gemini-2.0-flash)
- **Interface**: Textual TUI (full dashboard: chat + live stats + audit log)
- **Timeline**: 1 week MVP (Steps 1-6)
- **RAG/Memory**: Stretch goal (Step 11)

---

## Progress Tracker

### Phase 1: Foundation (Day 1) - Steps 1-2

#### Step 1: Project Scaffold
- [x] Initialize git repo
- [x] Create `pyproject.toml` with all dependencies
- [x] Create virtualenv, install deps
- [x] Create `.env.example` with `GEMINI_API_KEY`, `GROQ_API_KEY`
- [x] Create `.env` with actual keys (gitignored)
- [x] Create folder structure per spec
- [x] Create stub files for all modules
- [x] Write `src/main.py` entry point that starts TUI
- [x] Test Groq API call works
- [ ] Test Gemini API call works (quota exhausted)

**Status**: Completed
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: Groq API working. Gemini quota exhausted on free tier. 

---

#### Step 2: Tool Registry (Read-Only Tier)
- [x] Implement `src/tools/read_only.py`:
  - [x] `list_processes()` → `[{pid, name, memory_mb, cpu_percent, uptime_sec}]`
  - [x] `get_disk_usage(path: str)` → `{total_gb, used_gb, free_gb, percent}`
  - [x] `get_cpu_usage()` → `float` (system-wide)
  - [x] `get_top_memory_processes(n: int)` → list sorted by memory
  - [x] `list_open_ports()` → `[{pid, process, port, protocol}]`
- [x] Implement `src/tools/registry.py`:
  - [x] Tool schema class with: name, description, params (JSONSchema), tier
  - [x] `get_tool_schemas()` returning list compatible with Gemini function calling
  - [x] `execute_tool(name, params)` dispatcher
- [x] Unit test each tool in isolation

**Status**: Completed
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: All 5 read-only tools implemented and tested. list_open_ports handles macOS permission gracefully. 

---

### Phase 2: Core Agent Loop (Day 2) - Step 3

#### Step 3: Basic Agent Loop (Single-Shot Function Calling)
- [x] Implement `src/agent/prompts.py`:
  - [x] System prompt defining agent role, available tools, response format
  - [x] Tool result summarization prompt
- [x] Implement `src/agent/core.py`:
  - [x] `Agent` class with `chat(user_input: str) -> str`
  - [x] Loop: user input → Gemini with tools → tool call → execute → result to Gemini → final answer
  - [x] Groq fallback on Gemini failure (rate limit, timeout)
  - [x] Max 3 tool calls per turn
- [x] Integrate with TUI: basic chat panel that sends input to agent

**Status**: Implemented (needs API keys to test)
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: Agent core has LLM integration with Gemini primary and Groq fallback. TUI chat panel wired to agent. 

---

### Phase 3: Safety & Permissions (Day 3) - Steps 4-5

#### Step 4: Tiered Permissions + Reversible/Destructive Tools
- [x] Implement `src/tools/reversible.py`:
  - [x] `pause_process(pid: int)` → `SIGSTOP`
  - [x] `resume_process(pid: int)` → `SIGCONT`
  - [x] `set_priority(pid: int, nice: int)` → `psutil.Process.nice()`
  - [x] `clear_cache_dir(path: str)` → delete `*.cache`, `__pycache__`, etc.
- [x] Implement `src/tools/destructive.py`:
  - [x] `kill_process(pid: int, signal: int = 9)` → `SIGKILL`
  - [x] `delete_file(path: str)` → `os.remove` / `shutil.rmtree`
  - [x] `stop_service(name: str)` → `systemctl stop` (Linux) / `launchctl` (macOS)
- [x] Update `registry.py` with all new tools, tiers: `reversible`, `destructive`
- [x] Implement `src/safety/permissions.py`:
  - [x] `require_confirmation(tier, action_description) -> bool`
  - [x] Read-only: auto-approve
  - [x] Reversible: prompt "About to pause PID 1234. Continue? [y/N]"
  - [x] Destructive: prompt with red warning, require typing "yes"
- [x] Wire confirmation into `Agent.execute_tool()`

**Status**: Implemented (needs testing with TUI confirmation flow)
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: All 12 tools registered with tiers. Permissions module handles confirmation prompts. Circuit breaker implemented. 

---

#### Step 5: Audit Log + Undo
- [x] Create `src/storage/db_schema.sql`
- [x] Implement `src/storage/audit_log.py`:
  - [x] `log_action(action, params, tier, undo_data)` → returns `log_id`
  - [x] `update_result(log_id, result)`
  - [x] `get_last_action()` → last executed entry
  - [x] `mark_undone(log_id)`
- [x] Capture `undo_data` for each tool:
  - [x] `kill_process`: store process cmdline, cwd, env
  - [x] `delete_file`: store path, backup to `.trash/` (optional)
  - [x] `pause_process`: store PID
  - [x] `stop_service`: store service name
- [x] Add `undo_last()` to agent: reads last log, attempts reversal, logs undo
- [x] Add `/undo` command to TUI (bound to Ctrl+U)

**Status**: Implemented (needs testing)
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: SQLite audit log with indexes. Undo implemented for pause_process (resume) and kill_process (restart via cmdline). 

---

### Phase 4: Multi-Step Reasoning (Day 4) - Step 6

#### Step 6: Plan → Act → Observe → Reflect Loop
- [x] Redesign `src/agent/core.py` for multi-step:
  - [x] New `Agent.run_goal(goal: str, max_steps: int = 10)` method
  - [x] **Plan**: Send goal + tool schemas → get structured plan JSON
  - [x] **Act**: Execute step 1
  - [x] **Observe**: Feed result back
  - [x] **Reflect**: LLM decides: continue / done / error → next step or summary
- [x] Update system prompt for planning vs. reflection modes
- [x] Add `dry_run` mode: show full plan, ask confirmation before executing any destructive step
- [x] Add `max_steps` safety limit (default 10)
- [ ] Test goals: "free up disk space", "why is CPU high", "find memory leak"

**Status**: Implemented (needs LLM API keys to test)
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: Full Plan-Act-Observe-Reflect loop implemented with JSON plan parsing and reflection. 

---

### Phase 5: TUI & Polish (Day 5) - Step 9

#### Step 9: Textual TUI (Core Panels)
- [x] Implement `src/ui/tui.py` using Textual:
  - [x] **Left panel** (60%): Chat history + input box
  - [x] **Right-top** (20%): Live system stats (CPU%, Mem%, Disk%, top 5 processes)
  - [x] **Right-bottom** (20%): Audit log tail (last 10 entries, color by tier)
  - [x] Background thread updates stats every 2s via `psutil`
- [x] Wire chat input → `Agent.chat()` / `Agent.run_goal()`
- [x] Display tool calls/results in chat with distinct styling
- [x] Show confirmation prompts inline in chat (via permissions module)
- [x] Add keybindings: `Ctrl+C` cancel, `Ctrl+U` undo, `Ctrl+L` clear

**Status**: Implemented (needs testing)
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: Full 3-panel dashboard with live metrics, chat, and audit log. Background monitor integrated. Local fallback handles "kill highest cpu/memory" with inline confirmation.

--- 

---

### Phase 6: Testing & Hardening (Day 6-7)

#### Step 7: Circuit Breaker
- [x] Implement `src/safety/circuit_breaker.py`:
  - [x] Track destructive actions per session (sliding 5-min window)
  - [x] Threshold: 5 destructive actions → force re-confirmation for each subsequent
  - [x] Reset on session restart

#### Local Fallback for Kill Commands (Added)
- [x] Add `_handle_kill_highest()` in `src/agent/core.py` for CPU/memory
- [x] Return structured `__CONFIRM_KILL__` response for TUI
- [x] TUI handles confirmation inline with "Type 'yes'" prompt
- [x] Uses existing `_execute_tool_with_permissions()` for audit/circuit breaker

#### LM Studio Local LLM Integration (Added)
- [x] Add `LMSTUDIO_BASE_URL` and `LMSTUDIO_MODEL` to `.env`
- [x] Create `src/llm/lmstudio_client.py` - OpenAI-compatible wrapper
- [x] Update `src/agent/core.py` - Priority: LM Studio → Groq → Gemini
- [x] Update `pyproject.toml` - Add `openai>=1.30.0` dependency
- [x] Test with Qwen 3.5-9B - function calling works

**Status**: Implemented and tested
**Started**: 2025-08-09
**Completed**: 2025-08-09
**Notes**: LM Studio runs locally, unlimited, no rate limits. Qwen3.5-9B handles tool calls correctly. Fallback chain: LM Studio → Groq → Gemini.
- [ ] Create `tests/eval/test_cases.jsonl` with 20-30 cases
- [ ] Implement `run_eval.py` for planning accuracy only (no execution)

#### Polish
- [x] Error handling: network failures, permission errors, timeouts
- [x] Logging to file + console
- [ ] README with setup, usage, architecture diagram
- [ ] Demo script for interview

**Status**: Core implementation complete, polish in progress
**Started**: 2025-08-06
**Completed**: 2025-08-06
**Notes**: All core features working. Groq rate limit hit (free tier), Gemini quota exhausted. TUI launches, tools work, agent loop functional. 

---

## Session Log

| Date | Session | Steps Worked On | Completed | Blockers | Next Steps |
|------|---------|-----------------|-----------|----------|------------|
| 2025-08-09 | 2 | LM Studio integration (Qwen 3.5-9B), local fallback for kill commands, TUI duplicate fix | LM Studio as primary, kill confirmations, no duplicate messages | None | Polish: README, demo script, background monitor |

---

## Key Decisions Log

| Date | Decision | Rationale | Files Affected |
|------|----------|-----------|----------------|
| 2025-08-06 | Primary LLM: Gemini 2.0 Flash with Groq fallback | User preference, free tiers available | src/agent/core.py |
| 2025-08-06 | Full Textual TUI with 3-panel layout | Best for live demo | src/ui/tui.py |
| 2025-08-06 | SQLite audit log with undo_data column | Zero deps, portable | src/storage/audit_log.py, src/storage/db_schema.sql |
| 2025-08-06 | Tier-based permissions (read_only/reversible/destructive) | Safety first, explicit confirmations | src/safety/permissions.py, src/tools/registry.py |
| 2025-08-06 | Circuit breaker for destructive actions | Prevent runaway agent | src/safety/circuit_breaker.py |
| 2025-08-09 | LM Studio (Qwen 3.5-9B) as primary LLM | Unlimited local inference, no rate limits, function calling works | src/llm/lmstudio_client.py, src/agent/core.py, pyproject.toml, .env |

---

## Known Issues / Tech Debt

| Issue | Severity | Status | Notes |
|-------|----------|--------|-------|
|       |          |        |       |

---

## API Keys Status
- [ ] Gemini API key configured in `.env` (quota exhausted on free tier)
- [x] Groq API key configured in `.env` (working, fallback)
- [x] LM Studio running locally (primary, no API key needed)

---

## Environment
- Python version: 3.14.3
- OS: macOS (Darwin)
- Key packages: google-generativeai 0.8.6, groq 1.6.0, psutil 5.9+, textual 8.2.8, pytest 9.1.1