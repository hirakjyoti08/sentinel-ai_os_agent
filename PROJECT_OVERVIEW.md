# Sentinel – AI‑Powered OS Management Agent

## 1. Purpose
A natural‑language desktop agent that can **inspect, control, and remediate** OS‑level resources (processes, memory, disk, services, network ports) on the local machine.  
The user talks to the agent in plain English; the agent **plans**, **executes whitelisted tools**, **observes results**, and **reflects** until the goal is satisfied. All actions are logged, tier‑gated, and undoable.

---

## 2. High‑Level Architecture
```
┌─────────────────────┐        ┌───────────────────────┐
│   Textual TUI       │◄──────►│   Agent Core          │
│  (chat + live stats │        │  Plan → Act → Observe │
│   + audit log)      │        │  → Reflect loop       │
└─────────────────────┘        └─────────┬─────────────┘
                                         │
                ┌────────────────────────┼────────────────────────┐
                ▼                        ▼                        ▼
        ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
        │ Read‑Only     │         │ Reversible    │         │ Destructive   │
        │ (auto‑approve)│         │ (y/N confirm) │         │ (type “yes”)  │
        └───────────────┘         └───────────────┘         └───────────────┘
                │                        │                        │
                └────────────────────────┴────────────────────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │  Audit Log (SQLite) │
                              │  + Undo Support     │
                              └─────────────────────┘
```

* **TUI** – three panels (chat, live system metrics, audit‑log tail).  
* **Agent Core** – orchestrates the *Plan‑Act‑Observe‑Reflect* loop, calls LLMs, enforces safety.  
* **Tool Registry** – 12 whitelisted functions split into three safety tiers.  
* **Safety Layer** – tiered confirmations + circuit‑breaker (5 destructive actions / 5 min).  
* **Audit Log** – every action persisted with parameters, result, and *undo_data* for reversible ops.  
* **Background Monitor** – optional daemon that watches for anomalies (CPU spikes, disk pressure) and can trigger autonomous remediation.

---

## 3. Safety Model (Tiered Permissions)

| Tier | Tools (examples) | Confirmation | Typical Use |
|------|------------------|--------------|-------------|
| **read_only** | `list_processes`, `get_disk_usage`, `get_cpu_usage`, `get_top_memory_processes`, `list_open_ports` | none (auto) | Information gathering |
| **reversible** | `pause_process`, `resume_process`, `set_priority`, `clear_cache_dir` | `y/N` prompt | Temporary state changes |
| **destructive** | `kill_process`, `delete_file`, `stop_service` | type **“yes”** | Irreversible actions |

**Circuit Breaker** – after 5 destructive actions in a rolling 5‑minute window the agent forces an extra explicit confirmation for each subsequent destructive call.

**Undo** – `Ctrl+U` (or `/undo`) reads the latest audit entry and attempts reversal:
* `pause_process` → `resume_process`
* `kill_process` → restart using stored `cmdline`, `cwd`, `env`
* `delete_file` → restore from `.trash/` (if backup enabled)

---

## 4. Tool Catalog (12 tools)

| Tier | Function | Signature | Return |
|------|----------|-----------|--------|
| read_only | `list_processes` | `() → List[ProcessInfo]` | pid, name, mem MB, cpu %, uptime s |
| read_only | `get_disk_usage` | `(path: str) → DiskInfo` | total/used/free GB, % |
| read_only | `get_cpu_usage` | `() → float` | system‑wide % |
| read_only | `get_top_memory_processes` | `(n: int) → List[ProcessInfo]` | top‑N by RSS |
| read_only | `list_open_ports` | `() → List[PortInfo]` | pid, process, port, proto |
| reversible | `pause_process` | `(pid: int) → bool` | SIGSTOP |
| reversible | `resume_process` | `(pid: int) → bool` | SIGCONT |
| reversible | `set_priority` | `(pid: int, nice: int) → bool` | `psutil.Process.nice()` |
| reversible | `clear_cache_dir` | `(path: str) → int` | bytes freed |
| destructive | `kill_process` | `(pid: int, signal: int=9) → bool` | SIGKILL default |
| destructive | `delete_file` | `(path: str) → bool` | `os.remove` / `shutil.rmtree` |
| destructive | `stop_service` | `(name: str) → bool` | `systemctl stop` / `launchctl` |

All tools are registered in `src/tools/registry.py` with JSON‑Schema descriptors compatible with Gemini/OpenAI function‑calling.

---

## 5. Agent Loop – Plan → Act → Observe → Reflect

1. **Plan** – User goal + tool schemas → LLM returns a JSON plan (ordered steps).  
2. **Act** – Execute step 1 via the tool dispatcher.  
3. **Observe** – Tool result (stdout / return value / error) fed back to LLM.  
4. **Reflect** – LLM decides *continue / done / error* and either emits next step or final answer.  
5. Loop until `max_steps` (default 10) or *done*.

**Dry‑run mode** – shows the full plan, asks for confirmation before any destructive step.

---

## 6. LLM Providers (fallback chain)

| Priority | Provider | Model | Notes |
|----------|----------|-------|-------|
| 1 | **LM Studio** (local) | Qwen 3.5‑9B (OpenAI‑compatible) | Unlimited, no rate‑limits, function‑calling works |
| 2 | **Groq** | `llama‑3.3‑70b‑versatile` | Free tier, 100 K tokens/day |
| 3 | **Gemini** | `gemini‑2.0‑flash` | Free tier, 15 RPM / 1500 RPD |

Configured via `.env`:
```
LMSTUDIO_BASE_URL=http://localhost:1234/v1
LMSTUDIO_MODEL=qwen-3.5-9b
GROQ_API_KEY=…
GEMINI_API_KEY=…
```

---

## 7. Project Structure (key files)

```
ai-os-agent/
├─ pyproject.toml
├─ .env.example / .env
├─ README.md
├─ IMPLEMENTATION.md
├─ src/
│  ├─ main.py                     # entry point → launches TUI
│  ├─ agent/
│  │  ├─ core.py                  # Agent class, Plan‑Act‑Observe‑Reflect
│  │  ├─ prompts.py               # System prompts (plan, reflect, summarise)
│  │  └─ memory.py                # Conversation history
│  ├─ tools/
│  │  ├─ registry.py              # Tool schemas + dispatcher
│  │  ├─ read_only.py
│  │  ├─ reversible.py
│  │  └─ destructive.py
│  ├─ safety/
│  │  ├─ permissions.py           # Tier confirmation logic
│  │  └─ circuit_breaker.py       # Sliding‑window destructive counter
│  ├─ monitor/
│  │  └─ watcher.py               # Background anomaly detector (optional)
│  ├─ storage/
│  │  ├─ audit_log.py             # SQLite CRUD + undo helpers
│  │  └─ db_schema.sql
│  ├─ ui/
│  │  └─ tui.py                   # Textual 3‑panel dashboard
│  └─ llm/
│     └─ lmstudio_client.py       # OpenAI‑compatible wrapper
├─ tests/
│  ├─ test_tools.py               # Unit tests for each tool
│  └─ eval/                       # (planned) evaluation harness
└─ logs/
   └─ audit.db                    # SQLite audit database
```

---

## 8. Getting Started

```bash
# 1️⃣  Clone & enter
cd ai-os-agent

# 2️⃣  Create venv & install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3️⃣  Configure keys
cp .env.example .env
# edit .env → add GROQ_API_KEY (required) and optionally GEMINI_API_KEY
# start LM Studio locally and load Qwen‑3.5‑9B (or any function‑calling model)

# 4️⃣  Initialise audit DB
python -c "from src.storage.audit_log import init_db; init_db()"

# 5️⃣  Run the TUI
python -m src.main
```

**TUI key‑bindings**

| Key | Action |
|-----|--------|
| `Ctrl+U` | Undo last action |
| `Ctrl+L` | Clear chat |
| `Ctrl+C` | Quit |

**Example natural‑language commands**

* “What is my CPU usage?”
* “Show me the top 5 memory processes.”
* “Why is my CPU high?”
* “Free up disk space.”  *(multi‑step)*
* “List open ports.”

---

## 9. Development & Testing

```bash
# Run unit tests
python -m pytest tests/ -v

# Lint / type‑check (if configured)
# ruff check .
# mypy src/
```

*Tests* cover each tool in isolation (`tests/test_tools.py`).  
Future evaluation harness (`tests/eval/`) will measure planning accuracy without executing destructive actions.

---

## 10. Known Limitations & Platform Notes

| Limitation | Impact |
|------------|--------|
| Groq free tier → 100 K tokens/day | May hit rate‑limit under heavy use |
| Gemini free tier → 15 RPM / 1500 RPD | Often exhausted; used only as last fallback |
| macOS `list_open_ports` | Requires Full Disk Access or `sudo` |
| `undo last` for `kill_process` | Best‑effort restart (needs stored `cmdline`) |
| Background monitor (Step 8) | Implemented but not fully wired into TUI yet |

---

## 11. License
MIT – see `LICENSE` (or add one) for full text.

---

## 12. Quick Reference for New Contributors / AI Readers

| What you need to know | Where to look |
|------------------------|---------------|
| Overall goal & architecture | This file + `README.md` |
| Step‑by‑step implementation log | `IMPLEMENTATION.md` |
| Agent orchestration logic | `src/agent/core.py` |
| Tool implementations | `src/tools/*.py` |
| Safety & confirmations | `src/safety/permissions.py`, `circuit_breaker.py` |
| Audit & undo | `src/storage/audit_log.py` |
| TUI layout & key‑bindings | `src/ui/tui.py` |
| LLM client wiring | `src/llm/lmstudio_client.py`, `src/agent/core.py` |
| Configuration (env vars) | `.env.example` |

With this document a human or an AI can instantly grasp **what the project does, how it is organized, the safety guarantees, and how to run or extend it**.