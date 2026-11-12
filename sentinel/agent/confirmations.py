"""Destructive-request handling: find the target, then ask the UI to confirm.

Nothing in this module kills anything. Each function resolves *what* would be
terminated and returns a ``__CONFIRM_KILL__{json}__`` payload that the TUI
turns into a confirmation prompt; the kill itself only happens after the user
approves, through ``Agent._execute_tool_with_permissions``.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import psutil

from sentinel.safety.permissions import is_protected_process
from sentinel.tools.registry import execute_tool

CONFIRM_PREFIX = "__CONFIRM_KILL__"
CONFIRM_SUFFIX = "__"


def confirm_kill_payload(**data: Any) -> str:
    """Encode a kill-confirmation request for the UI."""
    return f"{CONFIRM_PREFIX}{json.dumps({'type': 'confirm_kill', **data})}{CONFIRM_SUFFIX}"


def parse_confirm_kill(response: str) -> dict[str, Any] | None:
    """Decode a payload produced by :func:`confirm_kill_payload` (``None`` if not one)."""
    if not (response.startswith(CONFIRM_PREFIX) and response.endswith(CONFIRM_SUFFIX)):
        return None
    try:
        return json.loads(response[len(CONFIRM_PREFIX):-len(CONFIRM_SUFFIX)])
    except ValueError:
        return None


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return f"PID {pid}"


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def find_listeners(port: int) -> list[tuple[int, str]]:
    """``(pid, name)`` of processes listening on ``port`` (psutil, then ``lsof``)."""
    found: dict[int, str] = {}
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr.port == port and conn.pid and conn.pid > 1:
                found.setdefault(conn.pid, _process_name(conn.pid))
    except Exception:
        pass

    if not found:  # non-root on macOS often needs lsof
        try:
            res = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-n", "-P"],
                capture_output=True, text=True, timeout=1.5, check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 1:
                        found.setdefault(int(parts[1]), parts[0])
        except Exception:
            pass
    return list(found.items())


def port_kill_confirmation(port: int) -> str | None:
    """Confirmation payload for closing ``port``, or ``None`` if nothing listens."""
    listeners = find_listeners(port)
    if not listeners:
        return None
    pids = [pid for pid, _ in listeners]
    names = [name for _, name in listeners]
    return confirm_kill_payload(
        port=port,
        pids=pids,
        pid=pids[0],
        name=f"{', '.join(names)} on port {port}",
        reason=f"close port {port}",
        metric="port_close",
    )


_PORT_RE = re.compile(r"(?:close|kill|stop|free|shut|terminate)\s*(?:port\s*)?:?\s*(\d{1,5})")
_PORT_FALLBACK_RE = re.compile(r"port\s+(\d{1,5})")


def handle_close_port_request(text: str) -> str:
    """Reply to "close port 3000" (confirmation) or "close port" (list open ports)."""
    match = _PORT_RE.search(text) or _PORT_FALLBACK_RE.search(text)
    if match:
        port = int(match.group(1))
        return port_kill_confirmation(port) or (
            f"No active listening process found on port {port}. The port is already closed."
        )

    result = execute_tool("list_open_ports", {})
    connections = result.get("connections", []) if result.get("success") else []
    if not connections:
        return "No open listening network ports found."
    lines = [
        "Currently open listening network ports:",
        "",
        "| Port | Protocol | PID | Process |",
        "|:---:|:---:|:---:|---|",
    ]
    lines += [
        f"| {c['port']} | {c.get('protocol', 'TCP')} | {c['pid']} | {c['process']} |"
        for c in connections[:8]
    ]
    lines += ["", "Which port would you like to close? (e.g. type 'close port 5000' or click 🔌 Close Port)."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #


def handle_kill_highest(metric: str) -> str:
    """Confirmation for the top CPU (``metric="cpu"``) or memory consumer."""
    if metric == "cpu":
        result = execute_tool("list_processes", {})
        if not result.get("success"):
            return f"Error getting processes: {result.get('error')}"
        candidates = [
            p for p in result.get("processes", [])
            if p["pid"] > 1 and (p.get("cpu_percent") or 0) > 0.1
        ]
        if not candidates:
            return "No user processes with significant CPU usage found."
        target = max(candidates, key=lambda p: p.get("cpu_percent") or 0)
        metric_value = f"{target.get('cpu_percent', 0):.1f}% CPU"
    else:
        result = execute_tool("get_top_memory_processes", {"n": 10})
        if not result.get("success"):
            return f"Error getting memory processes: {result.get('error')}"
        candidates = [p for p in result.get("processes", []) if p["pid"] > 1 and p["memory_mb"] > 10]
        if not candidates:
            return "No user processes with significant memory usage found."
        target = max(candidates, key=lambda p: p["memory_mb"])
        metric_value = f"{target['memory_mb']:.1f} MB"

    return confirm_kill_payload(
        pid=target["pid"],
        name=target["name"],
        reason=f"highest {metric} ({metric_value})",
        metric=metric,
    )


_RAW_PID_RE = re.compile(
    r"^(?:(?:kill|terminate|stop\s+process|close\s+process|end\s+process)\s+)?(?:pid\s*[:=]?\s*)?(\d+)$",
    re.IGNORECASE,
)
_PID_RE = re.compile(r"pid\s*[:=]?\s*(\d+)", re.IGNORECASE)
_NAME_RE = re.compile(
    r"^(?:kill\s+all|terminate\s+all|kill|terminate|stop\s+process|close\s+process|end\s+process)"
    r"\s+(?:process\s+)?(.+)",
    re.IGNORECASE,
)


def _resource_score(p: dict[str, Any]) -> float:
    return (p.get("memory_mb", 0) or 0) + (p.get("cpu_percent", 0) or 0)


def handle_kill_by_name_or_pid(text: str) -> str:
    """Confirmation for "kill 1234", "kill pid 1234", "kill spotify", "kill all node"."""
    text = text.strip()
    target_pid: int | None = None
    target_name: str | None = None

    if m := _RAW_PID_RE.search(text):
        target_pid = int(m.group(1))
    elif m := _PID_RE.search(text):
        target_pid = int(m.group(1))
    elif m := _NAME_RE.search(text):
        target_name = m.group(1).strip().lower()
    else:
        target_name = text.lower()

    result = execute_tool("list_processes", {})
    if not result.get("success"):
        return f"Error getting processes: {result.get('error')}"
    processes = result.get("processes", [])

    if target_pid is not None:
        target = next((p for p in processes if p["pid"] == target_pid), None)
        if target is None:
            try:
                target = {"pid": target_pid, "name": psutil.Process(target_pid).name()}
            except Exception:
                return f"No running process found with PID {target_pid}"
        if target["pid"] <= 1:
            return f"Cannot kill system process PID {target['pid']} ({target['name']})"
        protected, why = is_protected_process(target["pid"], target.get("name"))
        if protected:
            return f"🛡️ Safety Protection: {why}. Termination blocked."
        return confirm_kill_payload(
            pid=target["pid"],
            name=target["name"],
            reason=f"requested kill by PID {target_pid}",
            metric="user_requested",
        )

    assert target_name is not None
    matches = [p for p in processes if target_name in p["name"].lower()]
    if not matches:
        squashed = target_name.replace(" ", "").replace("-", "")
        matches = [
            p for p in processes
            if squashed in p["name"].lower().replace(" ", "").replace("-", "")
        ]
    if not matches:
        return f"No running process found matching '{target_name}'"

    allowed, blocked = [], []
    for p in matches:
        protected, why = is_protected_process(p["pid"], p["name"])
        (blocked if protected else allowed).append((p, why))
    if not allowed:
        return f"🛡️ Safety Protection: {blocked[0][1]}. Termination blocked."
    allowed_procs = [p for p, _ in allowed]

    if re.search(r"\b(all|every)\b", text, re.IGNORECASE):
        pids = [p["pid"] for p in allowed_procs]
        return confirm_kill_payload(
            pids=pids,
            pid=pids[0],
            name=f"all {len(pids)} processes matching '{target_name}'",
            reason=f"kill all {len(pids)} processes matching '{target_name}'",
            metric="user_requested_all",
        )

    exact = [p for p in allowed_procs if p["name"].lower() == target_name]
    if len(exact) == 1:
        target = exact[0]
    elif exact:
        target = max(exact, key=_resource_score)
    elif len(allowed_procs) == 1:
        target = allowed_procs[0]
    else:
        target = max(allowed_procs, key=_resource_score)

    tip = ""
    if len(allowed_procs) > 1:
        tip = (
            f" (Note: {len(allowed_procs)} matching processes found. "
            f"Type 'kill all {target_name}' to kill all)"
        )
    return confirm_kill_payload(
        pid=target["pid"],
        name=target["name"],
        reason=f"kill {target['name']}{tip}",
        metric="user_requested",
    )


def confirmation_for_tool_call(tool_name: str, args: dict[str, Any]) -> str | None:
    """Intercept LLM-requested kills/port closes so the user confirms them in the UI."""
    if tool_name == "close_port":
        try:
            port = int(args.get("port") or 0)
        except (TypeError, ValueError):
            return None
        return port_kill_confirmation(port) if port else None
    if tool_name == "kill_process":
        try:
            pid = int(args.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        if pid > 1:
            return confirm_kill_payload(
                pid=pid,
                name=_process_name(pid),
                reason=f"kill requested by assistant for PID {pid}",
                metric="llm_requested",
            )
    return None
