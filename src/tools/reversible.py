# Reversible System Tools (require confirmation)

import psutil
import os
import shutil
import signal
from typing import Dict, Any


def pause_process(pid: int) -> Dict[str, Any]:
    """Pause a process (SIGSTOP). Reversible with resume_process."""
    try:
        proc = psutil.Process(pid)
        proc.suspend()
        return {"success": True, "pid": pid, "action": "paused", "undo_data": {"pid": pid, "action": "pause_process"}}
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"Process {pid} not found"}
    except psutil.AccessDenied:
        return {"success": False, "error": f"Permission denied for process {pid}"}


def resume_process(pid: int) -> Dict[str, Any]:
    """Resume a paused process (SIGCONT)."""
    try:
        proc = psutil.Process(pid)
        proc.resume()
        return {"success": True, "pid": pid, "action": "resumed", "undo_data": {"pid": pid, "action": "resume_process"}}
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"Process {pid} not found"}
    except psutil.AccessDenied:
        return {"success": False, "error": f"Permission denied for process {pid}"}


def set_priority(pid: int, nice: int) -> Dict[str, Any]:
    """Set process priority (nice value). Range: -20 (highest) to 19 (lowest)."""
    try:
        proc = psutil.Process(pid)
        previous_nice = proc.nice()
        proc.nice(nice)
        return {
            "success": True,
            "pid": pid,
            "nice": nice,
            "undo_data": {"pid": pid, "previous_nice": previous_nice}
        }
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"Process {pid} not found"}
    except psutil.AccessDenied:
        return {"success": False, "error": f"Permission denied for process {pid}"}


def clear_cache_dir(path: str) -> Dict[str, Any]:
    """Clear cache directories (*.cache, __pycache__, node_modules, etc.)."""
    if not os.path.exists(path):
        return {"success": False, "error": f"Path {path} does not exist"}
    
    cleared = []
    patterns = ['*.cache', '__pycache__', '*.pyc', 'node_modules', '.pytest_cache', '*.log']
    
    for root, dirs, files in os.walk(path):
        for pattern in patterns:
            if pattern in dirs:
                full_path = os.path.join(root, pattern)
                try:
                    shutil.rmtree(full_path)
                    cleared.append(full_path)
                except Exception as e:
                    pass  # Skip on error
    
    return {"success": True, "cleared": cleared, "count": len(cleared)}