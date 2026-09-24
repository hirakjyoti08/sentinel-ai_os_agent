# Tool Registry - Definitions, Schemas, and Dispatcher

from typing import Dict, List, Any, Callable
from src.tools import read_only, reversible, destructive


# Tool schema definition matching Gemini function calling format
TOOL_DEFINITIONS = [
    # Read-only tools
    {
        "name": "list_processes",
        "description": "List all running processes with PID, name, memory, CPU, and uptime",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.list_processes
    },
    {
        "name": "get_disk_usage",
        "description": "Get disk usage for a given path (total, used, free, percent)",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to check"}},
            "required": []
        },
        "tier": "read_only",
        "function": read_only.get_disk_usage
    },
    {
        "name": "get_cpu_usage",
        "description": "Get system-wide CPU usage percentage",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.get_cpu_usage
    },
    {
        "name": "get_memory_usage",
        "description": "Get total installed RAM, used RAM, free RAM, and swap memory statistics in GB and percentage",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.get_memory_usage
    },
    {
        "name": "get_system_info",
        "description": "Get hardware architecture, OS platform, CPU core counts, and total installed RAM",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.get_system_info
    },
    {
        "name": "get_top_memory_processes",
        "description": "Get top N processes by memory usage",
        "parameters": {
            "type": "object",
            "properties": {"n": {"type": "integer", "description": "Number of processes"}},
            "required": []
        },
        "tier": "read_only",
        "function": read_only.get_top_memory_processes
    },
    {
        "name": "list_open_ports",
        "description": "List all open network ports with process info",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.list_open_ports
    },
    {
        "name": "get_gpu_usage",
        "description": "Get GPU utilization percentage, device name, and memory/VRAM usage",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "tier": "read_only",
        "function": read_only.get_gpu_usage
    },
    {
        "name": "get_network_bandwidth",
        "description": "Get current real-time network upload/download bandwidth speed and total transfer bytes",
        "parameters": {
            "type": "object",
            "properties": {
                "interval": {"type": "number", "description": "Measurement sample duration in seconds (default 0.2)"}
            },
            "required": []
        },
        "tier": "read_only",
        "function": read_only.get_network_bandwidth
    },
    # Reversible tools
    {
        "name": "pause_process",
        "description": "Pause a process (SIGSTOP). Reversible with resume_process.",
        "parameters": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "description": "Process ID"}},
            "required": ["pid"]
        },
        "tier": "reversible",
        "function": reversible.pause_process
    },
    {
        "name": "resume_process",
        "description": "Resume a paused process (SIGCONT).",
        "parameters": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "description": "Process ID"}},
            "required": ["pid"]
        },
        "tier": "reversible",
        "function": reversible.resume_process
    },
    {
        "name": "set_priority",
        "description": "Set process priority (nice value: -20 to 19).",
        "parameters": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "Process ID"},
                "nice": {"type": "integer", "description": "Nice value", "default": 0}
            },
            "required": ["pid"]
        },
        "tier": "reversible",
        "function": reversible.set_priority
    },
    {
        "name": "clear_cache_dir",
        "description": "Clear cache directories (*.cache, __pycache__, node_modules, etc.).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory to clean"}},
            "required": ["path"]
        },
        "tier": "reversible",
        "function": reversible.clear_cache_dir
    },
    # Destructive tools
    {
        "name": "kill_process",
        "description": "Kill a process (SIGKILL). DESTRUCTIVE - cannot be undone.",
        "parameters": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "Process ID"},
                "signal": {"type": "integer", "description": "Signal number", "default": 9}
            },
            "required": ["pid"]
        },
        "tier": "destructive",
        "function": destructive.kill_process
    },
    {
        "name": "delete_file",
        "description": "Delete a file or directory. DESTRUCTIVE - cannot be fully undone.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to delete"}},
            "required": ["path"]
        },
        "tier": "destructive",
        "function": destructive.delete_file
    },
    {
        "name": "stop_service",
        "description": "Stop a system service (systemctl/launchctl). DESTRUCTIVE.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Service name"}},
            "required": ["name"]
        },
        "tier": "destructive",
        "function": destructive.stop_service
    },
    {
        "name": "close_port",
        "description": "Close an open network port by terminating the process listening on it. DESTRUCTIVE - requires confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "Port number to close (e.g. 8080, 5000, 3000)"},
                "signal": {"type": "integer", "description": "Signal number (default 15 SIGTERM, or 9 SIGKILL)", "default": 15}
            },
            "required": ["port"]
        },
        "tier": "destructive",
        "function": destructive.close_port
    },
]


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return tool schemas in OpenAI function calling format (for Groq)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"]
            }
        }
        for t in TOOL_DEFINITIONS
    ]


def get_gemini_tool_schemas() -> List[Dict[str, Any]]:
    """Return tool schemas in Gemini function calling format."""
    def _strip_unsupported_fields(schema: Any) -> Any:
        if isinstance(schema, dict):
            return {
                key: _strip_unsupported_fields(value)
                for key, value in schema.items()
                if key != "default"
            }
        if isinstance(schema, list):
            return [_strip_unsupported_fields(item) for item in schema]
        return schema

    return [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": _strip_unsupported_fields(t["parameters"]),
        }
        for t in TOOL_DEFINITIONS
    ]


def get_tool_tiers() -> Dict[str, str]:
    """Return mapping of tool name to tier."""
    return {t["name"]: t["tier"] for t in TOOL_DEFINITIONS}


def get_tool_function(name: str) -> Callable:
    """Get the Python function for a tool by name."""
    for t in TOOL_DEFINITIONS:
        if t["name"] == name:
            return t["function"]
    raise ValueError(f"Tool not found: {name}")


def execute_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool by name with parameters."""
    func = get_tool_function(name)
    return func(**params)