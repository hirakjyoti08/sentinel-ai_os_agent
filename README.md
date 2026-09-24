# 🛡️ Sentinel – AI-Powered OS Management Agent

A natural-language agent that manages your computer's OS-level operations (processes, memory, disk, services) using an LLM for planning and reasoning, a whitelisted tool layer for safe execution, tiered permissions, an audit/undo log, and a background monitor for autonomous anomaly response.

## Features

- **Natural Language Interface**: Ask questions like "why is my CPU high?" or "free up disk space"
- **Tool-Based Execution**: 12 whitelisted tools across 3 safety tiers (read-only, reversible, destructive)
- **Safety First**: Tiered confirmations, circuit breaker, full audit trail with undo capability
- **Multi-Step Reasoning**: Plan → Act → Observe → Reflect loop for complex goals
- **Live TUI Dashboard**: Three-panel interface (chat + live stats + audit log)
- **Background Monitor**: Autonomous anomaly detection (optional)
- **Dual LLM Support**: Groq (primary) with Gemini fallback

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Textual TUI                            │
│  ┌─────────────────┬─────────────────────────────────────┐  │
│  │   Chat Panel    │  System Stats Panel (Live)          │  │
│  │                 │  • CPU / Memory / Disk %            │  │
│  │  User Input     │  • Top 5 Processes                  │  │
│  │  Tool Calls     │                                     │  │
│  │  Agent Response │  ┌─────────────────────────────────┐│  │
│  │                 │  │  Audit Log Panel                ││  │
│  │  (Ctrl+U undo)  │  │  • Last 10 actions              ││  │
│  └─────────────────┴──│  • Color-coded by tier          ││  │
│                      └─────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        Agent Core                           │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │ Plan         │→ │ Act          │→ │ Observe            │ │
│  │ (LLM creates │  │ (Execute     │  │ (Feed result back  │ │
│  │  JSON plan)  │  │  tool)       │  │  to LLM)           │ │
│  └──────────────┘  └──────────────┘  └────────────────────┘ │
│        │                                       ▲             │
│        └────────────── Reflect ────────────────┘             │
│                     (LLM decides next step)                  │
└─────────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ Read-Only     │    │ Reversible    │    │ Destructive   │
│ (auto-approve)│    │ (y/N confirm) │    │ (type 'yes')  │
├───────────────┤    ├───────────────┤    ├───────────────┤
│ list_processes│    │ pause_process │    │ kill_process  │
│ get_disk_usage│    │ resume_process│    │ delete_file   │
│ get_cpu_usage │    │ set_priority  │    │ stop_service  │
│ get_top_mem   │    │ clear_cache   │    │               │
│ list_ports    │    │               │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
         │                    │                    │
         └────────────────────┴────────────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │  Audit Log (SQLite)│
                    │  + Undo Support   │
                    └───────────────────┘
```

## Quick Start

### Prerequisites
- Python 3.11+
- Groq API key (free at https://console.groq.com)
- Optional: Gemini API key (free at https://aistudio.google.com)

### Installation

```bash
# Clone and enter project
cd ai-os-agent

# Install dependencies
pip install -e .

# Configure API keys
cp .env.example .env
# Edit .env and add your GROQ_API_KEY (required) and GEMINI_API_KEY (optional)

# Initialize database
python -c "from src.storage.audit_log import init_db; init_db()"

# Run TUI
python -m src.main
```

### Usage

**In the TUI:**
- Type natural language commands in the chat panel
- Press `Ctrl+U` to undo last action
- Press `Ctrl+L` to clear chat
- Press `Ctrl+C` to quit

**Example commands:**
- "What is my CPU usage?"
- "Show me top 5 memory processes"
- "Check disk usage"
- "Why is my CPU high?"
- "Free up disk space" (multi-step)
- "List open ports"

## Project Structure

```
ai-os-agent/
├── pyproject.toml
├── .env.example
├── .env                    # Your API keys (gitignored)
├── README.md
├── src/
│   ├── main.py             # Entry point
│   ├── agent/
│   │   ├── core.py         # Plan-Act-Observe-Reflect loop
│   │   ├── prompts.py      # System prompts
│   │   └── memory.py       # Conversation memory
│   ├── tools/
│   │   ├── registry.py     # Tool definitions & dispatcher
│   │   ├── read_only.py    # 5 read-only tools
│   │   ├── reversible.py   # 4 reversible tools
│   │   └── destructive.py  # 3 destructive tools
│   ├── safety/
│   │   ├── permissions.py  # Tier enforcement & confirmations
│   │   └── circuit_breaker.py # Rate limiting
│   ├── monitor/
│   │   └── watcher.py      # Background anomaly detector
│   ├── storage/
│   │   ├── audit_log.py    # SQLite audit trail
│   │   └── db_schema.sql
│   └── ui/
│       └── tui.py          # Textual 3-panel dashboard
├── tests/
│   ├── test_tools.py       # Unit tests
│   └── eval/               # Evaluation harness (optional)
└── logs/
    └── audit.db            # SQLite audit database
```

## Safety Model

| Tier | Tools | Confirmation | Use Case |
|------|-------|--------------|----------|
| **read_only** | list_processes, get_disk_usage, get_cpu_usage, get_top_memory_processes, list_open_ports | None (auto) | Information gathering |
| **reversible** | pause_process, resume_process, set_priority, clear_cache_dir | `y/N` | Temporary changes |
| **destructive** | kill_process, delete_file, stop_service | Type `yes` | Irreversible actions |

**Circuit Breaker**: After 5 destructive actions in 5 minutes, forces re-confirmation for each subsequent destructive action.

**Audit Log**: Every action logged with timestamp, params, result, and undo data. Supports `undo last` for reversible actions and process restart for killed processes.

## Development

### Run Tests
```bash
python -m pytest tests/ -v
```

### Key Classes
- `Agent` (src/agent/core.py) - Main orchestrator
- `SystemWatcher` (src/monitor/watcher.py) - Background monitor
- `CircuitBreaker` (src/safety/circuit_breaker.py) - Rate limiter
- `AIOSAgentApp` (src/ui/tui.py) - TUI application

## Known Limitations

- Groq free tier: 100K tokens/day rate limit
- Gemini free tier: 15 RPM, 1500 RPD rate limit
- macOS: `list_open_ports` requires Full Disk Access or sudo
- `undo last` for `kill_process` attempts process restart (best effort)
- Background monitor (Step 8) implemented but not fully integrated in TUI

## License

MIT