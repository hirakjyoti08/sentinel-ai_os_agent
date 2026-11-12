<div align="center">

# 🛡️ Sentinel

**An AI co-pilot for your operating system — ask in plain English, act with guardrails.**

Sentinel watches your machine's telemetry, explains what's slowing it down, and manages processes,
ports and disk space through a live terminal dashboard. Every action passes through a
three-tier permission model, a circuit breaker and an audit log with undo.

[![CI](https://github.com/hirakjyoti08/sentinel-ai_os_agent/actions/workflows/ci.yml/badge.svg)](https://github.com/hirakjyoti08/sentinel-ai_os_agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)
![Textual](https://img.shields.io/badge/UI-Textual-5A4FCF)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Features](#-features) ·
[How it works](#-how-it-works) ·
[Safety model](#-safety-model) ·
[Getting started](#-getting-started) ·
[Usage](#-usage) ·
[Development](#-development) ·
[Roadmap](#-roadmap)

<img src="assets/sentinel_preview.png" alt="Sentinel terminal dashboard: chat panel, live telemetry, process and port tables, audit trail" width="100%" />

</div>

---

## ✨ Features

| | |
|---|---|
| 💬 **Natural-language control** | Ask *"why is my CPU high?"*, *"free up disk space"* or *"can I run Android Studio here?"* — Sentinel picks the right tools, runs them and explains the result. |
| 🧠 **Multi-LLM with fallback** | NVIDIA Nemotron is the primary model, with Groq and Google Gemini as automatic fallbacks when a provider is down or rate-limited. Provider status is shown live in the UI. |
| ⚡ **Fast path & offline mode** | Requests like *"kill spotify"* or *"close port 3000"* are recognised locally and routed straight to a confirmation, without waiting on an LLM. If every provider is down, common questions (CPU, memory, disk, ports, battery…) are still answered locally. |
| 🛡️ **Safety by design** | 20 whitelisted tools in three tiers (read-only, reversible, destructive), type-checked arguments, explicit confirmations, a protected-process list, and a circuit breaker for runaway destructive actions. |
| ↩️ **Audit log with undo** | Every action is written to SQLite with its parameters, result and undo data. `Ctrl+U` reverts the last change — resume a paused process, restore a deleted file, restart a service. |
| 📊 **Live dashboard** | A [Textual](https://textual.textualize.io) TUI with streaming chat, CPU / GPU / memory / disk / network gauges and sparklines, clickable *Kill* and *Close* actions on processes and ports, and the audit trail. |
| 🔔 **Proactive alerts** | A background watcher detects CPU, memory, disk and GPU pressure, names the culprit process, and sends in-app toasts plus native macOS / Linux desktop notifications with cooldown throttling. |
| 🤖 **Natural-language rules** | Say *"if any process uses over 90% CPU for 5 minutes, pause it and notify me"*. Sentinel turns it into a structured rule, shows you what it understood, and saves it only after you confirm. Rules then run locally on every telemetry tick — no LLM calls — and can notify, pause or lower priority, never kill or delete. |
| 🧾 **Root-cause reports** | When an alert fires, Sentinel gathers the culprit's age, child processes, threads, listening ports, other busy processes and your recent actions, and writes a short incident report: what happened, the likely cause, and what to do. AI-written when a model is reachable, local analysis otherwise. |
| 🧹 **Developer disk cleaner** | Finds and safely clears Xcode DerivedData, Homebrew, pip, npm/yarn caches and the trash, reporting how much space was reclaimed. |

---

## 🧭 How it works

```mermaid
flowchart TD
    U([You]) -->|natural language| TUI["Textual TUI<br/>chat · telemetry · audit trail"]
    W["SystemWatcher<br/>background telemetry"] -->|anomaly + culprit PID| N["Notifier<br/>toasts · desktop alerts · cooldown"]
    W -->|live metrics| TUI
    N --> TUI
    W -->|every tick| R["Rule engine<br/>local · no LLM"]
    R -->|pause · lower priority| P
    N -->|alert context| IR["Incident report<br/>AI or local analysis"]
    IR --> TUI

    TUI --> A["Agent core"]
    A -->|common commands| FP["Fast path<br/>local intent routing"]
    A -->|everything else| LLM["LLM fallback chain<br/>Nemotron → Groq → Gemini"]
    LLM -->|tool calls| A

    FP --> P{"Permission tier"}
    A --> P
    P -->|read-only| T["Tool registry<br/>20 tools · validated args"]
    P -->|reversible / destructive| C["Confirmation +<br/>circuit breaker +<br/>protected-process guard"]
    C -->|approved| T
    T --> OS[("Operating system<br/>psutil · lsof · osascript")]
    T --> L[("Audit log<br/>SQLite + undo data")]
```

1. **Understand.** Your message reaches the `Agent`. Common commands take the local fast path; anything else goes to the LLM chain, which falls back provider by provider. If all of them fail, the same local intent table answers what it can.
2. **Act.** The model replies with tool calls. Each one is looked up in the registry, its arguments are validated against a typed schema, it is checked against its permission tier and, if needed, confirmed by you. Kills requested by the model are always turned into a confirmation prompt first.
3. **Record.** The action is logged *before* it runs, then updated with the result and the data needed to undo it.
4. **Respond.** Tool results are trimmed and fed back to the model, which loops until it has an answer and streams it token by token into the chat.

---

## 🛡️ Safety model

| Tier | Confirmation | Tools |
|------|--------------|-------|
| 🟢 **Read-only** | None — runs automatically | `list_processes` `get_cpu_usage` `get_gpu_usage` `get_memory_usage` `get_disk_usage` `get_system_info` `get_top_memory_processes` `list_open_ports` `get_network_bandwidth` `get_battery_and_thermal` `analyze_disk_hogs` |
| 🟡 **Reversible** | Yes / no prompt | `pause_process` `resume_process` `set_priority` `clear_cache_dir` `clean_developer_caches` |
| 🔴 **Destructive** | Explicit confirmation | `kill_process` `close_port` `delete_file` `stop_service` |

On top of the tiers:

- **Unknown tools default to destructive.** Anything not in the registry gets the strictest treatment.
- **Validated arguments.** Every tool's arguments are a Pydantic model; malformed or out-of-range values from the model (a PID of `"abc"`, a port of `70000`) are rejected with a clear error before anything runs.
- **Protected processes can't be touched.** `launchd`, `kernel_task`, `WindowServer`, `systemd`, `Finder` and other critical system processes (plus PIDs ≤ 1) are refused for kill and pause.
- **Circuit breaker.** After 5 destructive actions within 5 minutes, further destructive actions are blocked until the window clears.
- **Recoverable deletes.** `delete_file` moves the target to `.trash/` first, so undo can restore it.
- **Rules can't do anything destructive.** Automation rules can only notify, pause a process or lower its priority — killing, deleting or closing can't be expressed in a rule. Every rule is shown back to you and saved only after you type *yes*; its actions are audited, a pause can be undone with Ctrl+U, and Sentinel never targets itself, the terminal it runs in, or protected system processes.
- **Redacted incident data.** Command lines can contain tokens or passwords, so they are masked before an incident is sent to an LLM. Set `SENTINEL_INCIDENT_REPORTS=local` to keep incident data on your machine entirely.
- **Best-effort process restart.** Undoing a kill relaunches the process from its saved command line and working directory.

---

## 🚀 Getting started

### Prerequisites

- **Python 3.11+** on macOS or Linux
- An **NVIDIA API key** — free credits at [build.nvidia.com](https://build.nvidia.com)
- *Optional:* fallback keys from [Groq](https://console.groq.com) and [Google AI Studio](https://aistudio.google.com)

### Install

```bash
git clone https://github.com/hirakjyoti08/sentinel-ai_os_agent.git
cd sentinel-ai_os_agent

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Prefer a standalone command? Install it with [pipx](https://pipx.pypa.io) instead:

```bash
pipx install .
```

### Configure

```bash
cp .env.example .env
```

Then open `.env` and add your keys:

| Variable | Required | Default | Purpose |
|----------|:--------:|---------|---------|
| `NVIDIA_API_KEY` | ✅ | — | Primary LLM (Nemotron via NVIDIA NIM) |
| `GROQ_API_KEY` | | — | First fallback LLM |
| `GEMINI_API_KEY` | | — | Second fallback LLM |
| `NVIDIA_MODEL` | | `nvidia/nemotron-3-ultra-550b-a55b` | Override the primary model |
| `GROQ_MODEL` | | `qwen/qwen3.8-27b` | Override the Groq model |
| `GEMINI_MODEL` | | `gemini-3.6-flash` | Override the Gemini model |
| `SENTINEL_DESKTOP_ALERTS` | | `true` | Native desktop notifications |
| `SENTINEL_ALERT_SOUND` | | `true` | Play a sound with alerts |
| `ALERT_CPU_THRESHOLD` / `ALERT_MEMORY_THRESHOLD` / `ALERT_DISK_THRESHOLD` / `ALERT_GPU_THRESHOLD` | | `85` / `90` / `90` / `90` | Alert thresholds (%) |
| `ALERT_COOLDOWN_SEC` | | `60` | Minimum seconds between repeat alerts |
| `SENTINEL_INCIDENT_REPORTS` | | `llm` | `llm` (AI-written), `local` (never sent to an LLM) or `off` |
| `SENTINEL_HOME` | | see below | Where the audit DB, `rules.json` and `.trash/` live |

Runtime data goes in the repository folder when you run from a source checkout, and in `~/.sentinel` when Sentinel is installed as a package. Set `SENTINEL_HOME` to put it anywhere else.

### Run

```bash
sentinel            # or: python -m sentinel
```

The audit database is created automatically on first launch.

---

## 💡 Usage

**Try asking:**

```text
What is my CPU usage?
Why is my laptop so slow right now?
Show me the top 5 memory processes
Can I run Android Studio on this machine?
Free up disk space
Which apps are listening on network ports?
Kill spotify
Close port 3000
```

**Automate it with rules:**

```text
if any process uses over 90% CPU for 5 minutes, pause it and notify me
when chrome uses more than 30% memory, lower its priority
if disk is above 90%, alert me
list rules
disable rule 1        (also: enable rule <id>, delete rule <id>)
```

Sentinel reads common phrasings instantly and offline; anything else is interpreted by the LLM and validated before you're asked to confirm.

**After an alert:** an incident report appears in the chat automatically. Ask *"explain the last alert"* to see it again.

**Keyboard shortcuts:**

| Key | Action |
|-----|--------|
| `Enter` | Send message |
| `Ctrl+U` | Undo the last reversible or destructive action |
| `Ctrl+L` | Clear the chat |
| `Ctrl+C` / `Esc` | Quit |

The quick-action buttons run common requests in one click (system health, top processes, kill the highest-CPU process, close a port). You can also click **Kill** next to any process or **Close** next to any open port — every button goes through the same confirmation flow as a typed command.

---

## 🗂️ Project structure

```text
sentinel-ai_os_agent/
├── sentinel/
│   ├── main.py               # Entry point (`sentinel` command)
│   ├── paths.py              # Runtime data locations
│   ├── agent/
│   │   ├── core.py           # Agent: chat loop + permission pipeline
│   │   ├── intents.py        # Ordered intent table for local routing
│   │   ├── confirmations.py  # Kill / close-port targeting → UI confirmation
│   │   ├── local_handlers.py # Offline answers for read-only intents
│   │   ├── undo.py           # Reverse audited actions
│   │   ├── prompts.py        # System prompt, enriched with host hardware
│   │   └── memory.py         # Conversation memory
│   ├── llm/
│   │   └── providers.py      # Provider interface, Nemotron/Groq/Gemini, fallback chain
│   ├── tools/
│   │   ├── registry.py       # Typed (Pydantic) tool args, schemas, tiers, dispatch
│   │   ├── read_only.py      # 11 telemetry tools
│   │   ├── reversible.py     # 5 reversible tools
│   │   └── destructive.py    # 4 destructive tools
│   ├── safety/
│   │   ├── permissions.py    # Confirmations + protected-process guard
│   │   └── circuit_breaker.py
│   ├── monitor/
│   │   ├── watcher.py        # Background telemetry + anomaly detection
│   │   ├── incidents.py      # Root-cause context + incident reports
│   │   └── notifier.py       # Desktop notifications + cooldown
│   ├── rules/
│   │   ├── models.py         # Rule / Condition / Action (Pydantic)
│   │   ├── parser.py         # Natural language → rule (local, then LLM)
│   │   ├── engine.py         # Sustained-condition + cooldown evaluation
│   │   └── store.py          # rules.json persistence
│   ├── storage/
│   │   └── audit_log.py      # SQLite audit trail + undo data
│   └── ui/
│       └── tui.py            # Textual dashboard
├── tests/
│   ├── test_agent_automation.py # Rules and incident reports through the agent
│   ├── test_agent_safety.py  # Safety paths end to end with a scripted LLM
│   ├── test_incidents.py     # Context, redaction, local and AI reports
│   ├── test_intents.py       # Routing table and priority order
│   ├── test_providers.py     # Streaming, normalization, fallback chain
│   ├── test_registry.py      # Typed args, generated schemas, tiers
│   ├── test_rules.py         # Parser, validation, store, engine timing
│   ├── test_tools.py         # Tools, TUI widgets, notifications
│   └── test_tui_bindings.py  # Keyboard shortcuts
├── .github/workflows/ci.yml  # Lint + tests on macOS & Linux
├── .env.example
└── pyproject.toml
```

---

## 🧪 Development

```bash
pip install -e ".[dev]"

ruff check .             # lint
python -m pytest -v      # run the test suite
```

The 236 tests don't need API keys or network access. The safety tests drive the agent with a scripted LLM against a real disposable process and a temporary audit database, so they check what actually happens to the system — that an LLM-requested kill only produces a confirmation, that reversible actions are refused without approval, that the circuit breaker blocks bursts, that undo really resumes a paused process, and that a saved rule pauses a real process once its condition has held long enough. [GitHub Actions](.github/workflows/ci.yml) runs lint and tests on every push and pull request, on **Ubuntu and macOS** with **Python 3.11, 3.12 and 3.13**.

**Architecture at a glance**

| Piece | File | Role |
|-------|------|------|
| `Agent` | `sentinel/agent/core.py` | Chat loop and the permission pipeline every tool call goes through |
| `FallbackChain` / `LLMProvider` | `sentinel/llm/providers.py` | One interface over Nemotron, Groq and Gemini; normalizes streamed and non-streamed tool calls into `LLMResponse` |
| `INTENTS` | `sentinel/agent/intents.py` | Ordered, pure matchers for the fast path and offline fallback |
| `Tool` + Pydantic args | `sentinel/tools/registry.py` | Single source for tiers, LLM schemas and argument validation |
| `RuleEngine` / `Rule` | `sentinel/rules/` | Parses rules from natural language, evaluates them every tick |
| `gather_context` / `build_report` | `sentinel/monitor/incidents.py` | Root-cause context and incident reports |
| `SystemWatcher` | `sentinel/monitor/watcher.py` | Collects metrics and raises anomalies |
| `NotificationCooldownManager` | `sentinel/monitor/notifier.py` | Throttles repeat alerts |
| `CircuitBreaker` | `sentinel/safety/circuit_breaker.py` | Limits bursts of destructive actions |
| `AIOSAgentApp` | `sentinel/ui/tui.py` | The Textual application |

---

## 🗺️ Roadmap

- [ ] Connect the multi-step planner (`Agent.run_goal`: plan → act → observe → reflect, with dry-run) to the chat UI
- [ ] Evaluation suite that scores tool choice, safety and step count across scripted scenarios
- [ ] Expose the tool registry as an MCP server, behind the same permission tiers
- [x] Natural-language automation rules (*"pause anything above 90% CPU for 5 minutes"*)
- [x] Root-cause incident reports for alerts
- [ ] Approve/deny prompt in the TUI for reversible tools requested by the LLM (e.g. *"pause Chrome"*)
- [ ] Undo timeline: browse and revert any past action, not just the last one
- [ ] Fully offline mode with a local model

---

## ⚠️ Known limitations

- **Network required.** The LLM providers are cloud APIs, and their free tiers have rate limits; Sentinel falls back between them automatically.
- **Port listing on macOS** may need Full Disk Access or `sudo` to see every process.
- **Undoing a kill is best effort.** The process is relaunched from its saved command line, but in-memory state is lost.
- **Lowering a priority is one-way without admin rights.** Unprivileged users can lower a process's priority but not raise it again, so the `lower_priority` rule action lasts until the process restarts.
- **Linux desktop notifications** need `notify-send` (part of `libnotify`).

---

## 🤝 Contributing

Issues and pull requests are welcome. Before opening a PR, please run `ruff check .` and `python -m pytest`, and add tests for any new tool or safety rule.

## 📄 License

Released under the [MIT License](LICENSE) © 2026 Hirakjyoti Talukdar.
