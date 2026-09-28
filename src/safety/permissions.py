import sys
from typing import Tuple, Callable, Optional


TIERS = ["read_only", "reversible", "destructive"]
_TOOL_TIERS = None

# Critical OS system processes that must NEVER be terminated or paused
PROTECTED_PROCESS_NAMES = {
    # Unix & Darwin Kernel/Init
    "kernel_task", "launchd", "init", "systemd",
    # macOS Window Server & Core Desktop UI
    "windowserver", "loginwindow", "finder", "dock", "systemuiserver", "controlcenter",
    # Critical System Daemons
    "securityd", "powerd", "diskarbitrationd", "coreaudiod", "syslogd", "kextd",
    "distnoted", "opendirectoryd", "notifyd", "cfprefsd", "fseventsd", "mds",
    # Linux Core Services
    "dbus-daemon", "xorg", "wayland", "gnome-shell", "systemd-journald", "systemd-udevd",
}


def is_protected_process(pid: int, name: Optional[str] = None) -> Tuple[bool, str]:
    """Check if a process is a protected system process that must not be killed or paused."""
    if pid <= 1:
        return True, f"PID {pid} is an essential system init process"

    proc_name = name
    if not proc_name:
        try:
            import psutil
            proc_name = psutil.Process(pid).name()
        except Exception:
            pass

    if proc_name and proc_name.lower().strip() in PROTECTED_PROCESS_NAMES:
        return True, f"'{proc_name}' (PID {pid}) is a critical OS system process required for system stability"

    return False, ""


def get_tool_tier(tool_name: str) -> str:
    """Get the tier for a tool."""
    global _TOOL_TIERS
    if _TOOL_TIERS is None:
        from src.tools.registry import get_tool_tiers
        _TOOL_TIERS = get_tool_tiers()
    return _TOOL_TIERS.get(tool_name, "destructive")  # Default to most restrictive


def requires_confirmation(tier: str) -> bool:
    """Check if a tier requires user confirmation."""
    return tier in ["reversible", "destructive"]


def format_confirmation_prompt(tier: str, tool_name: str, params: dict) -> str:
    """Format a confirmation prompt for the user."""
    action_desc = _describe_action(tool_name, params)
    
    if tier == "reversible":
        return f"⚠️  Reversible action: {action_desc}\nContinue? [y/N]: "
    elif tier == "destructive":
        return f"🔴 DESTRUCTIVE action: {action_desc}\nType 'yes' to confirm: "
    return ""


def _describe_action(tool_name: str, params: dict) -> str:
    """Generate human-readable description of an action."""
    descriptions = {
        "pause_process": f"pause process PID {params.get('pid')}",
        "resume_process": f"resume process PID {params.get('pid')}",
        "set_priority": f"set priority of PID {params.get('pid')} to nice={params.get('nice')}",
        "clear_cache_dir": f"clear cache in {params.get('path')}",
        "kill_process": f"KILL process PID {params.get('pid')} (signal {params.get('signal', 9)})",
        "delete_file": f"DELETE {params.get('path')}",
        "stop_service": f"STOP service {params.get('name')}",
    }
    return descriptions.get(tool_name, f"execute {tool_name} with {params}")


def confirm_action(
    tier: str,
    tool_name: str,
    params: dict,
    confirm_callback: Optional[Callable[[str, str, dict], bool]] = None
) -> bool:
    """Prompt user for confirmation. Returns True if confirmed."""
    if not requires_confirmation(tier):
        return True
    
    if confirm_callback is not None:
        return confirm_callback(tier, tool_name, params)
    
    if not sys.stdin or not sys.stdin.isatty():
        return False

    prompt = format_confirmation_prompt(tier, tool_name, params)
    try:
        if tier == "destructive":
            response = input(prompt).strip().lower()
            return response == "yes"
        else:
            response = input(prompt).strip().lower()
            return response == "y"
    except (EOFError, OSError):
        return False