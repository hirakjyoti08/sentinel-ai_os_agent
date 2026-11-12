"""Offline answers for read-only intents (used when no LLM provider is reachable).

Each handler takes the lower-cased request text, calls read-only tools through
the registry, and returns a Markdown reply. Destructive intents live in
:mod:`sentinel.agent.confirmations`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import psutil

from sentinel.agent import confirmations
from sentinel.agent.intents import is_greeting, is_identity_question, top_n
from sentinel.tools.registry import execute_tool

GREETING_REPLY = (
    "Hello! I am OS Assistant. I'm here to help you monitor system health, inspect "
    "processes, check GPU/CPU usage, and manage your computer. How can I assist you today?"
)
IDENTITY_REPLY = "I am OS Assistant, your operating system management and performance companion."

# ``Agent._execute_tool_with_permissions``: tier check, confirmation, audit log.
GuardedRunner = Callable[[str, dict[str, Any]], dict[str, Any]]


def render_ascii_bar(percent: float, width: int = 10) -> str:
    """Compact ASCII gauge, e.g. ``[███░░░░░░░]  30.0%``."""
    filled = max(0, min(width, int(round((percent / 100.0) * width))))
    return f"[{'█' * filled}{'░' * (width - filled)}] {percent:>5.1f}%"


def greeting_reply(text: str) -> str | None:
    if is_greeting(text):
        return GREETING_REPLY
    if is_identity_question(text):
        return IDENTITY_REPLY
    return None


def _process_table(processes: list[dict], *, memory_first: bool = False) -> list[str]:
    if memory_first:
        lines = ["| Rank | PID | Process | Memory | CPU% |", "|:---:|:---:|---|:---:|:---:|"]
        lines += [
            f"| {i} | {p['pid']} | {p['name']} | {p['memory_mb']:.1f} MB | {p['cpu_percent']:.1f}% |"
            for i, p in enumerate(processes, 1)
        ]
    else:
        lines = ["| Rank | PID | Process | CPU% | Memory |", "|:---:|:---:|---|:---:|:---:|"]
        lines += [
            f"| {i} | {p['pid']} | {p['name']} | {p['cpu_percent']:.1f}% | {p['memory_mb']:.1f} MB |"
            for i, p in enumerate(processes, 1)
        ]
    return lines


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def system_health(_text: str) -> str:
    cpu = execute_tool("get_cpu_usage", {})
    gpu = execute_tool("get_gpu_usage", {})
    disk = execute_tool("get_disk_usage", {"path": "/"})
    memory = execute_tool("get_top_memory_processes", {"n": 3})

    cpu_val = cpu.get("cpu_percent", 0.0) if cpu.get("success") else 0.0
    disk_val = disk.get("percent", 0.0) if disk.get("success") else 0.0
    gpu_val = gpu.get("gpu_percent") if (gpu.get("success") and gpu.get("available")) else None

    cpu_status = "Idle" if cpu_val < 30 else ("Moderate" if cpu_val < 75 else "High")
    disk_status = "Free" if disk_val < 75 else ("Warning" if disk_val < 90 else "Full")

    lines = [
        "| Resource | Usage | Graph | Status | Details |",
        "|---|---|---|---|---|",
        f"| CPU | {cpu_val:.1f}% | {render_ascii_bar(cpu_val)} | {cpu_status} | System Load |",
    ]
    if gpu_val is not None:
        lines.append(
            f"| GPU | {gpu_val:.1f}% | {render_ascii_bar(gpu_val)} | Cool | {gpu.get('device', 'GPU')} |"
        )
    free_gb = disk.get("free_gb", 0.0) if disk.get("success") else 0.0
    total_gb = disk.get("total_gb", 0.0) if disk.get("success") else 0.0
    lines.append(
        f"| Disk | {disk_val:.1f}% | {render_ascii_bar(disk_val)} | {disk_status} | "
        f"{free_gb:.1f} GB free of {total_gb:.1f} GB |"
    )

    if memory.get("success") and memory.get("processes"):
        lines += ["", "| Rank | PID | Top Process | CPU% | Memory |", "|:---:|:---:|---|:---:|:---:|"]
        lines += [
            f"| {i} | {p['pid']} | {p['name']} | {p.get('cpu_percent', 0.0):.1f}% | "
            f"{p.get('memory_mb', 0.0):.1f} MB |"
            for i, p in enumerate(memory["processes"][:3], 1)
        ]

    if cpu_val > 80.0:
        verdict = "Verdict: High CPU utilization detected."
    elif disk_val > 85.0:
        verdict = "Verdict: Disk storage is nearly full."
    else:
        verdict = "Verdict: System is cool, healthy, and running smoothly."
    lines.append(f"\n{verdict}")
    return "\n".join(lines)


def top_cpu_question(_text: str) -> str:
    result = execute_tool("list_processes", {})
    if not result.get("success"):
        return f"Error getting process list: {result.get('error')}"
    processes = result.get("processes", [])
    if not processes:
        return "No active user processes found."
    top = processes[0]
    lines = _process_table(processes[:5])
    lines.append(f"\nTop consumer: [bold]{top['name']}[/bold] (PID {top['pid']}).")
    return "\n".join(lines)


def top_n_processes(text: str) -> str:
    n = top_n(text)
    n = 5 if n is None else n
    result = execute_tool("list_processes", {})
    if not result.get("success"):
        return f"Error getting process list: {result.get('error')}"
    return "\n".join(_process_table(result.get("processes", [])[:n]))


def cpu_usage(_text: str) -> str:
    result = execute_tool("get_cpu_usage", {})
    if not result.get("success"):
        return f"Error getting CPU: {result.get('error')}"
    p = result["cpu_percent"]
    desc = "Idle" if p < 30 else ("Moderate" if p < 75 else "High Load")
    return "\n".join([
        "| Metric | Usage | Graph | Status |",
        "|---|---|---|---|",
        f"| CPU Utilization | {p:.1f}% | {render_ascii_bar(p)} | {desc} |",
    ])


def battery_thermal(_text: str) -> str:
    result = execute_tool("get_battery_and_thermal", {})
    if not result.get("success"):
        return f"Error getting battery/thermal info: {result.get('error')}"
    lines = ["| Component | Status | Details |", "|---|---|---|"]
    if result.get("has_battery"):
        pct = result.get("percent", 0)
        graph = render_ascii_bar(pct) if pct is not None else "-"
        lines.append(f"| Battery Level | {pct:.1f}% | {graph} |")
        lines.append(f"| Power State | {result.get('status')} | {result.get('time_remaining') or 'N/A'} |")
        if result.get("cycle_count"):
            lines.append(f"| Battery Health | {result.get('condition')} | {result.get('cycle_count')} cycles |")
    else:
        lines.append("| Battery | Desktop / No Battery | AC Power Connected |")
    status = "⚠️ Throttling" if result.get("thermal_throttling", False) else "Normal / Cool"
    lines.append(f"| Thermal State | {status} | {result.get('thermal_state', 'Nominal')} |")
    return "\n".join(lines)


def memory(text: str) -> str:
    result = execute_tool("get_top_memory_processes", {"n": top_n(text) or 5})
    if not result.get("success"):
        return f"Error getting memory info: {result.get('error')}"
    processes = result.get("processes", [])
    if not processes:
        return "No memory-intensive processes found."
    return "\n".join(_process_table(processes, memory_first=True))


def disk_hogs(_text: str) -> str:
    result = execute_tool("analyze_disk_hogs", {})
    if not result.get("success"):
        return f"Error scanning disk hogs: {result.get('error')}"
    targets = result.get("targets", [])
    total = result.get("total_reclaimable_str", "0 B")
    if not targets:
        return (
            "### Developer Cache & Storage Consumption\n"
            "No significant reclaimable developer caches found. Your system storage is clean!\n"
            "Total Reclaimable: **0 B**"
        )
    lines = [
        "### Developer Cache & Storage Consumption",
        f"Identified {len(targets)} reclaimable developer & system caches ({total} total potential savings):",
        "",
        "| Category | Size | Path | Description |",
        "|---|:---:|---|---|",
    ]
    lines += [f"| {t['name']} | {t['size_str']} | {t['path']} | {t['description']} |" for t in targets[:6]]
    lines += ["", f"Total Reclaimable: **{total}**. To clean all caches, enter 'clean developer caches all'."]
    return "\n".join(lines)


def clean_caches(text: str, run_guarded: GuardedRunner) -> str:
    """Reversible: runs through the permission pipeline, never directly."""
    target = next((t for t in ("pip", "npm", "xcode", "homebrew", "yarn", "trash") if t in text), "all")
    result = run_guarded("clean_developer_caches", {"targets": [target]})
    if not result.get("success"):
        return f"Error cleaning caches: {result.get('error')}"
    return (
        f"✨ Successfully cleaned {result.get('count', 0)} cache directories and reclaimed "
        f"**{result.get('reclaimed_str', '0 B')}**."
    )


def disk_usage(_text: str) -> str:
    r = execute_tool("get_disk_usage", {"path": "/"})
    if not r.get("success"):
        return f"Error getting disk usage: {r.get('error')}"
    pct = r["percent"]
    tip = "Plenty of free storage" if pct < 75 else "Storage nearly full"
    return "\n".join([
        "| Mount | Used | Total | Free | Graph | Status |",
        "|---|---|---|---|---|---|",
        f"| Primary (/) | {r['used_gb']:.1f} GB ({pct:.1f}%) | {r['total_gb']:.1f} GB | "
        f"{r['free_gb']:.1f} GB | {render_ascii_bar(pct)} | {tip} |",
    ])


def gpu(_text: str) -> str:
    r = execute_tool("get_gpu_usage", {})
    if not r.get("success"):
        return f"Error getting GPU usage: {r.get('error')}"
    if not r.get("available"):
        return "Dedicated GPU telemetry is not accessible on this system configuration."
    if r.get("memory_used_mb") is not None and r.get("memory_total_mb") is not None:
        vram = f"{r['memory_used_mb']:.1f} MB / {r['memory_total_mb']:.1f} MB"
    elif r.get("memory_used_mb") is not None:
        vram = f"{r['memory_used_mb']:.1f} MB used"
    else:
        vram = "Unified Memory"
    return "\n".join([
        "| Device | Utilization | Graph | VRAM Details |",
        "|---|---|---|---|",
        f"| {r.get('device', 'GPU')} | {r['gpu_percent']:.1f}% | {render_ascii_bar(r['gpu_percent'])} | {vram} |",
    ])


def list_processes(_text: str) -> str | None:
    result = execute_tool("list_processes", {})
    if not result.get("success"):
        return None
    return "\n".join(_process_table(result.get("processes", [])[:10]))


def network_bandwidth(_text: str) -> str:
    r = execute_tool("get_network_bandwidth", {"interval": 0.3})
    if not r.get("success"):
        return f"Error measuring network bandwidth: {r.get('error')}"
    return "\n".join([
        "| Direction | Current Speed | Total Transferred | Status |",
        "|---|---|---|---|",
        f"| Download (RX) | {r.get('download_speed', '0 B/s')} | {r.get('total_received', '0 B')} | Active |",
        f"| Upload (TX)   | {r.get('upload_speed', '0 B/s')} | {r.get('total_sent', '0 B')} | Active |",
        "\nLive network throughput measured over 300ms window.",
    ])


def inspect_process(text: str) -> str | None:
    m = re.search(r"\b(\d+)\b", text)
    if not m:
        return None
    pid = int(m.group(1))
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        cpu = proc.cpu_percent(interval=0.1)
        mem = proc.memory_info().rss / (1024 * 1024)
        status = proc.status()
    except Exception as e:
        return f"Cannot inspect PID {pid}: process may have already exited ({e})."
    return "\n".join([
        f"Process Telemetry for PID {pid} (`{name}`):",
        "",
        "| Metric | Value | Graph / Detail |",
        "|---|---|---|",
        f"| PID | {pid} | System Process Identifier |",
        f"| Name | {name} | Executable binary |",
        f"| Status | {status} | Operational state |",
        f"| CPU% | {cpu:.1f}% | {render_ascii_bar(min(100.0, cpu))} |",
        f"| Memory | {mem:.1f} MB | Physical resident set size |",
    ])


def open_ports(_text: str) -> str:
    r = execute_tool("list_open_ports", {})
    if not r.get("success"):
        return f"Error listing ports: {r.get('error')}"
    conns = r.get("connections", [])
    if not conns:
        return "No open listening network ports found."
    lines = [
        f"Found {len(conns)} active listening network ports:",
        "",
        "| Port | Protocol | PID | Process Name |",
        "|------|----------|-----|--------------|",
    ]
    lines += [f"| {c['port']} | {c['protocol']} | {c['pid']} | `{c['process']}` |" for c in conns[:12]]
    if len(conns) > 12:
        lines.append(f"\n... and {len(conns) - 12} additional listening ports.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Intent → handler
# --------------------------------------------------------------------------- #

# Handlers for intents that change the system. They receive the agent's
# permission-checked runner instead of calling tools directly.
GUARDED_HANDLERS: dict[str, Callable[[str, GuardedRunner], str | None]] = {
    "clean_caches": clean_caches,
}

HANDLERS: dict[str, Callable[[str], str | None]] = {
    "greeting": greeting_reply,
    "system_health": system_health,
    "kill_highest_cpu": lambda _t: confirmations.handle_kill_highest("cpu"),
    "kill_highest_memory": lambda _t: confirmations.handle_kill_highest("memory"),
    "close_port": confirmations.handle_close_port_request,
    "kill_process": confirmations.handle_kill_by_name_or_pid,
    "top_cpu_question": top_cpu_question,
    "top_n_processes": top_n_processes,
    "cpu_usage": cpu_usage,
    "battery_thermal": battery_thermal,
    "memory": memory,
    "disk_hogs": disk_hogs,
    "disk_usage": disk_usage,
    "gpu": gpu,
    "list_processes": list_processes,
    "network_bandwidth": network_bandwidth,
    "inspect_process": inspect_process,
    "open_ports": open_ports,
}
