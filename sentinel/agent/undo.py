"""Reverse actions recorded in the audit log."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from sentinel.tools.registry import execute_tool

Result = dict[str, Any]


def _ok(message: str) -> Result:
    return {"success": True, "message": message}


def _fail(error: str) -> Result:
    return {"success": False, "error": error}


def _undo_pause(undo: dict, params: dict) -> Result | str:
    pid = undo.get("pid") or params.get("pid")
    return execute_tool("resume_process", {"pid": pid}) if pid else "Cannot undo pause: missing PID"


def _undo_resume(undo: dict, params: dict) -> Result | str:
    pid = undo.get("pid") or params.get("pid")
    return execute_tool("pause_process", {"pid": pid}) if pid else "Cannot undo resume: missing PID"


def _undo_set_priority(undo: dict, params: dict) -> Result | str:
    pid = undo.get("pid") or params.get("pid")
    previous = undo.get("previous_nice")
    if previous is None:
        return f"Cannot revert priority for PID {pid}: previous nice value unknown"
    return execute_tool("set_priority", {"pid": pid, "nice": previous})


def _undo_delete(undo: dict, params: dict) -> Result | str:
    dest = undo.get("path") or params.get("path")
    backup = undo.get("backup_path")
    if not backup or not os.path.exists(backup):
        return f"Cannot restore {dest}: backup not found in trash"
    try:
        if undo.get("was_dir", False):
            shutil.copytree(backup, dest)
        else:
            shutil.copy2(backup, dest)
    except Exception as e:
        return _fail(str(e))
    return _ok(f"Restored {dest} from trash")


def _undo_stop_service(undo: dict, params: dict) -> Result | str:
    name = undo.get("service") or params.get("name")
    if not name:
        return "Cannot restart service: missing service name"
    commands = {"Linux": ["systemctl", "start", name], "Darwin": ["launchctl", "start", name]}
    cmd = commands.get(platform.system())
    if cmd is None:
        return _fail(f"Unsupported OS {platform.system()}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as e:
        return _fail(str(e))
    return _ok(f"Restarted service {name}") if res.returncode == 0 else _fail(res.stderr)


def _undo_kill(undo: dict, _params: dict) -> Result | str:
    # A kill can't truly be undone; relaunch from the saved command line if we have one.
    cmdline = undo.get("cmdline") or []
    if not cmdline and undo.get("undo_list"):
        cmdline = undo["undo_list"][0].get("cmdline", [])
    if cmdline:
        try:
            subprocess.Popen(cmdline, cwd=undo.get("cwd"))
        except Exception as e:
            return _fail(str(e))
        return _ok(f"Restarted process: {cmdline}")
    pids = undo.get("terminated_pids") or [undo.get("pid")]
    port = undo.get("port")
    target = f"port {port} (PIDs: {pids})" if port else f"PIDs: {pids}"
    return _fail(f"Process on {target} cannot be automatically restarted without executable path")


UNDO_HANDLERS: dict[str, Callable[[dict, dict], Result | str]] = {
    "pause_process": _undo_pause,
    "resume_process": _undo_resume,
    "set_priority": _undo_set_priority,
    "delete_file": _undo_delete,
    "stop_service": _undo_stop_service,
    "kill_process": _undo_kill,
    "close_port": _undo_kill,
}


def undo_action(row: dict[str, Any]) -> tuple[bool, str]:
    """Reverse one audit-log row. Returns ``(succeeded, message for the user)``."""
    action = row["action"]
    handler = UNDO_HANDLERS.get(action)
    if handler is None:
        return False, f"Undo not implemented for {action}"
    undo = json.loads(row["undo_data"]) if row.get("undo_data") else {}
    params = json.loads(row["params"]) if row.get("params") else {}

    result = handler(undo, params)
    if isinstance(result, str):  # precondition failed; nothing was attempted
        return False, result
    if result.get("success"):
        return True, f"Undid {action}: {result.get('message', 'success')}"
    return False, f"Undo failed: {result.get('error', 'unknown error')}"
