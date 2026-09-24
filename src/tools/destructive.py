# Destructive System Tools (require explicit confirmation)

import psutil
import os
import shutil
import signal as signal_module
import subprocess
import platform
import time
from typing import Dict, Any


def kill_process(pid: int, signal: int = signal_module.SIGKILL, **kwargs) -> Dict[str, Any]:
    """Kill a process (SIGKILL by default). DESTRUCTIVE - cannot be undone."""
    signal_num = kwargs.get("signal_num", signal)
    try:
        proc = psutil.Process(pid)
        # Capture undo data before killing
        try:
            cmdline = proc.cmdline()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            cmdline = []
        try:
            cwd = proc.cwd()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            cwd = None
        try:
            environ = proc.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            environ = None

        undo_data = {
            "pid": pid,
            "cmdline": cmdline,
            "cwd": cwd,
            "environ": environ,
            "name": proc.name()
        }
        proc.send_signal(signal_num)
        return {"success": True, "pid": pid, "signal": signal_num, "undo_data": undo_data}
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"Process {pid} not found"}
    except psutil.AccessDenied:
        return {"success": False, "error": f"Permission denied for process {pid}"}


def delete_file(path: str) -> Dict[str, Any]:
    """Delete a file or directory. DESTRUCTIVE - moves to .trash before removal for undo."""
    if not os.path.exists(path):
        return {"success": False, "error": f"Path {path} does not exist"}
    
    abs_path = os.path.abspath(path)
    was_dir = os.path.isdir(abs_path)
    trash_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".trash"))
    backup_path = None

    try:
        os.makedirs(trash_dir, exist_ok=True)
        base_name = os.path.basename(abs_path.rstrip(os.sep)) or "deleted_item"
        backup_name = f"{int(time.time() * 1000)}_{base_name}"
        backup_path = os.path.join(trash_dir, backup_name)

        if was_dir:
            shutil.copytree(abs_path, backup_path)
            shutil.rmtree(abs_path)
        else:
            shutil.copy2(abs_path, backup_path)
            os.remove(abs_path)
            
        undo_data = {"path": abs_path, "was_dir": was_dir, "backup_path": backup_path}
        return {"success": True, "path": abs_path, "undo_data": undo_data}
    except Exception as e:
        undo_data = {"path": abs_path, "was_dir": was_dir, "backup_path": backup_path}
        return {"success": False, "error": str(e), "undo_data": undo_data}


def stop_service(name: str) -> Dict[str, Any]:
    """Stop a system service. DESTRUCTIVE - may affect system stability."""
    system = platform.system()
    undo_data = {"service": name, "system": system}
    
    try:
        if system == "Linux":
            result = subprocess.run(["systemctl", "stop", name], capture_output=True, text=True)
        elif system == "Darwin":  # macOS
            result = subprocess.run(["launchctl", "stop", name], capture_output=True, text=True)
        else:
            return {"success": False, "error": f"Unsupported OS: {system}", "undo_data": undo_data}
        
        if result.returncode == 0:
            return {"success": True, "service": name, "undo_data": undo_data}
        else:
            return {"success": False, "error": result.stderr, "undo_data": undo_data}
    except Exception as e:
        return {"success": False, "error": str(e), "undo_data": undo_data}


def close_port(port: int, signal: int = signal_module.SIGTERM, **kwargs) -> Dict[str, Any]:
    """Close an open network port by terminating the listening process(es). DESTRUCTIVE."""
    signal_num = kwargs.get("signal_num", signal)
    pids = []
    seen = set()

    # 1. Check psutil network connections
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status == 'LISTEN' and conn.laddr.port == port and conn.pid:
                if conn.pid > 1 and conn.pid not in seen:
                    seen.add(conn.pid)
                    pids.append(conn.pid)
    except Exception:
        pass

    # 2. Check lsof (especially for non-root on macOS/Linux)
    if not pids:
        try:
            res = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-n", "-P"],
                capture_output=True,
                text=True,
                timeout=1.5
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        pid = int(parts[1])
                        if pid > 1 and pid not in seen:
                            seen.add(pid)
                            pids.append(pid)
        except Exception:
            pass

    if not pids:
        return {
            "success": False,
            "port": port,
            "error": f"No active listening process found on port {port}."
        }

    terminated_pids = []
    proc_names = []
    undo_list = []
    failed_errors = []

    for pid in pids:
        res = kill_process(pid, signal=signal_num)
        if res.get("success"):
            terminated_pids.append(pid)
            if res.get("undo_data"):
                undo_list.append(res["undo_data"])
                proc_names.append(res["undo_data"].get("name", f"PID {pid}"))
        else:
            failed_errors.append(f"PID {pid}: {res.get('error')}")

    if not terminated_pids:
        return {
            "success": False,
            "port": port,
            "error": f"Failed to terminate process on port {port}: {', '.join(failed_errors)}"
        }

    return {
        "success": True,
        "port": port,
        "terminated_pids": terminated_pids,
        "processes": proc_names,
        "undo_data": {
            "tool": "close_port",
            "port": port,
            "terminated_pids": terminated_pids,
            "processes": proc_names,
            "undo_list": undo_list
        }
    }