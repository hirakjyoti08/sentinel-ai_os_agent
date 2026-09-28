# Reversible System Tools (require confirmation)

import psutil
import os
import shutil
import signal
from typing import Dict, Any
from src.safety.permissions import is_protected_process


def pause_process(pid: int) -> Dict[str, Any]:
    """Pause a process (SIGSTOP). Reversible with resume_process."""
    is_prot, prot_reason = is_protected_process(pid)
    if is_prot:
        return {"success": False, "error": f"Operation denied: {prot_reason}"}

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


def clean_developer_caches(targets: Optional[list] = None, custom_paths: Optional[list] = None) -> Dict[str, Any]:
    """Clean developer caches (e.g. 'pip', 'npm', 'xcode', 'homebrew', 'trash')."""
    from src.tools.read_only import format_bytes

    cache_map = {
        "xcode": [
            os.path.expanduser("~/Library/Developer/Xcode/DerivedData"),
            os.path.expanduser("~/Library/Developer/Xcode/iOS DeviceSupport")
        ],
        "homebrew": [os.path.expanduser("~/Library/Caches/Homebrew")],
        "pip": [os.path.expanduser("~/Library/Caches/pip"), os.path.expanduser("~/.cache/pip")],
        "npm": [os.path.expanduser("~/.npm")],
        "yarn": [os.path.expanduser("~/Library/Caches/Yarn"), os.path.expanduser("~/.cache/yarn")],
        "trash": [os.path.expanduser("~/.Trash"), os.path.expanduser("~/.local/share/Trash")]
    }

    target_name_map = {}
    selected_dirs = []

    if custom_paths:
        for cp in custom_paths:
            if isinstance(cp, dict):
                p = os.path.expanduser(cp.get("path", ""))
                name = cp.get("name", os.path.basename(p) or "Custom Cache")
            else:
                p = os.path.expanduser(str(cp))
                name = os.path.basename(p) or "Custom Cache"
            if p:
                cache_map[name.lower()] = [p]
                target_name_map[p] = name

    if not targets or "all" in [str(t).lower() for t in targets]:
        for dirs in cache_map.values():
            selected_dirs.extend(dirs)
    else:
        for t in targets:
            key = str(t).lower().strip()
            if key in cache_map:
                selected_dirs.extend(cache_map[key])
            elif os.path.exists(key):
                selected_dirs.append(key)

    reclaimed_bytes = 0
    cleared_paths = []
    cleaned_names = []

    for d in set(selected_dirs):
        if not os.path.exists(d):
            continue
        try:
            for item in os.listdir(d):
                item_path = os.path.join(d, item)
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        reclaimed_bytes += os.path.getsize(item_path)
                        os.unlink(item_path)
                    elif os.path.isdir(item_path):
                        for root, _, files in os.walk(item_path):
                            for f in files:
                                try:
                                    reclaimed_bytes += os.path.getsize(os.path.join(root, f))
                                except Exception:
                                    pass
                        shutil.rmtree(item_path)
                except Exception:
                    pass
            cleared_paths.append(d)
            c_name = target_name_map.get(d, os.path.basename(d) or "Cache")
            cleaned_names.append(c_name)
        except Exception:
            pass

    return {
        "success": True,
        "reclaimed_bytes": reclaimed_bytes,
        "total_reclaimed_bytes": reclaimed_bytes,
        "reclaimed_str": format_bytes(reclaimed_bytes),
        "cleared_directories": cleared_paths,
        "cleaned_targets": cleaned_names,
        "count": len(cleared_paths)
    }