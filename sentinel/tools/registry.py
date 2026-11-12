"""Tool registry: typed argument models, permission tiers, schemas and dispatch.

Every tool the agent can call is declared once here, with a Pydantic model for
its arguments. The JSON schemas sent to the LLM providers are generated from
those models, and every call is validated (and lightly coerced, e.g. ``"42"``
→ ``42``) before the underlying function runs, so a malformed or hallucinated
tool call becomes a clean error instead of a ``TypeError``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentinel.tools import destructive, read_only, reversible

Tier = Literal["read_only", "reversible", "destructive"]

# Unknown tools get the strictest treatment.
DEFAULT_TIER: Tier = "destructive"


# --------------------------------------------------------------------------- #
# Argument models
# --------------------------------------------------------------------------- #


class ToolArgs(BaseModel):
    """Base for tool arguments. Unknown fields from the LLM are ignored."""

    model_config = ConfigDict(extra="ignore")


class NoArgs(ToolArgs):
    pass


class DiskUsageArgs(ToolArgs):
    path: str = Field("/", description="Path to check")


class TopMemoryArgs(ToolArgs):
    n: int = Field(10, ge=1, le=500, description="Number of processes")


class NetworkBandwidthArgs(ToolArgs):
    interval: float = Field(
        0.2, gt=0, le=10, description="Measurement sample duration in seconds (default 0.2)"
    )


class DiskHogsArgs(ToolArgs):
    custom_paths: list[str] | None = Field(
        None, description="Optional list of specific directories to inspect"
    )


class PidArgs(ToolArgs):
    pid: int = Field(..., ge=1, description="Process ID")


class SetPriorityArgs(ToolArgs):
    pid: int = Field(..., ge=1, description="Process ID")
    nice: int = Field(0, ge=-20, le=19, description="Nice value")


class PathArgs(ToolArgs):
    path: str = Field(..., min_length=1, description="Directory to clean")


class CleanCachesArgs(ToolArgs):
    targets: list[str] | None = Field(
        None,
        description="Caches to clean (e.g. ['pip', 'npm', 'xcode', 'homebrew', 'trash', 'all'])",
    )


class KillProcessArgs(ToolArgs):
    pid: int = Field(..., ge=1, description="Process ID")
    signal: int = Field(9, ge=1, le=64, description="Signal number")


class DeletePathArgs(ToolArgs):
    path: str = Field(..., min_length=1, description="Path to delete")


class ServiceArgs(ToolArgs):
    name: str = Field(..., min_length=1, description="Service name")


class ClosePortArgs(ToolArgs):
    port: int = Field(
        ..., ge=1, le=65535, description="Port number to close (e.g. 8080, 5000, 3000)"
    )
    signal: int = Field(
        15, ge=1, le=64, description="Signal number (default 15 SIGTERM, or 9 SIGKILL)"
    )


# --------------------------------------------------------------------------- #
# Tool table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    tier: Tier
    function: Callable[..., dict[str, Any]]
    args: type[ToolArgs] = NoArgs

    def json_schema(self) -> dict[str, Any]:
        """JSON schema for this tool's arguments, in the shape LLM APIs expect."""
        return _clean_schema(self.args.model_json_schema())


TOOLS: tuple[Tool, ...] = (
    # Read-only ------------------------------------------------------------- #
    Tool("list_processes",
         "List all running processes with PID, name, memory, CPU, and uptime",
         "read_only", read_only.list_processes),
    Tool("get_disk_usage",
         "Get disk usage for a given path (total, used, free, percent)",
         "read_only", read_only.get_disk_usage, DiskUsageArgs),
    Tool("get_cpu_usage",
         "Get system-wide CPU usage percentage",
         "read_only", read_only.get_cpu_usage),
    Tool("get_memory_usage",
         "Get total installed RAM, used RAM, free RAM, and swap memory statistics in GB "
         "and percentage",
         "read_only", read_only.get_memory_usage),
    Tool("get_system_info",
         "Get hardware architecture, OS platform, CPU core counts, and total installed RAM",
         "read_only", read_only.get_system_info),
    Tool("get_battery_and_thermal",
         "Get battery charge percentage, charging status, time remaining, cycle count, and "
         "thermal throttling state",
         "read_only", read_only.get_battery_and_thermal),
    Tool("get_top_memory_processes",
         "Get top N processes by memory usage",
         "read_only", read_only.get_top_memory_processes, TopMemoryArgs),
    Tool("list_open_ports",
         "List all open network ports with process info",
         "read_only", read_only.list_open_ports),
    Tool("get_gpu_usage",
         "Get GPU utilization percentage, device name, and memory/VRAM usage",
         "read_only", read_only.get_gpu_usage),
    Tool("get_network_bandwidth",
         "Get current real-time network upload/download bandwidth speed and total transfer "
         "bytes",
         "read_only", read_only.get_network_bandwidth, NetworkBandwidthArgs),
    Tool("analyze_disk_hogs",
         "Scan and analyze developer caches, build artifacts, and bloated directories "
         "(Xcode, pip, npm, Homebrew, trash)",
         "read_only", read_only.analyze_disk_hogs, DiskHogsArgs),
    # Reversible ------------------------------------------------------------ #
    Tool("pause_process",
         "Pause a process (SIGSTOP). Reversible with resume_process.",
         "reversible", reversible.pause_process, PidArgs),
    Tool("resume_process",
         "Resume a paused process (SIGCONT).",
         "reversible", reversible.resume_process, PidArgs),
    Tool("set_priority",
         "Set process priority (nice value: -20 to 19).",
         "reversible", reversible.set_priority, SetPriorityArgs),
    Tool("clear_cache_dir",
         "Clear cache directories (*.cache, __pycache__, node_modules, etc.).",
         "reversible", reversible.clear_cache_dir, PathArgs),
    Tool("clean_developer_caches",
         "Safely clean developer caches and bloated directories (e.g. 'pip', 'npm', "
         "'xcode', 'homebrew', 'trash', or 'all')",
         "reversible", reversible.clean_developer_caches, CleanCachesArgs),
    # Destructive ----------------------------------------------------------- #
    Tool("kill_process",
         "Kill a process (SIGKILL). DESTRUCTIVE - cannot be undone.",
         "destructive", destructive.kill_process, KillProcessArgs),
    Tool("delete_file",
         "Delete a file or directory. DESTRUCTIVE - cannot be fully undone.",
         "destructive", destructive.delete_file, DeletePathArgs),
    Tool("stop_service",
         "Stop a system service (systemctl/launchctl). DESTRUCTIVE.",
         "destructive", destructive.stop_service, ServiceArgs),
    Tool("close_port",
         "Close an open network port by terminating the process listening on it. "
         "DESTRUCTIVE - requires confirmation.",
         "destructive", destructive.close_port, ClosePortArgs),
)

_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


# --------------------------------------------------------------------------- #
# Schema generation
# --------------------------------------------------------------------------- #


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Turn a Pydantic JSON schema into the compact form LLM tool APIs accept.

    Drops ``title`` keys, collapses ``X | None`` into ``X`` (optional-ness is
    already expressed by ``required``) and always emits ``required``.
    """

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(n) for n in node]
        if not isinstance(node, dict):
            return node
        node = {k: v for k, v in node.items() if k != "title"}
        any_of = node.get("anyOf")
        if any_of and any(opt == {"type": "null"} for opt in any_of):
            non_null = [opt for opt in any_of if opt != {"type": "null"}]
            if len(non_null) == 1:
                merged = {**non_null[0], **{k: v for k, v in node.items() if k != "anyOf"}}
                if merged.get("default", 0) is None:
                    merged.pop("default")
                node = merged
        return {k: clean(v) for k, v in node.items()}

    cleaned = clean(schema)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength"):
        for prop in cleaned.get("properties", {}).values():
            prop.pop(key, None)  # keep schemas provider-neutral; Pydantic enforces bounds
    cleaned.setdefault("properties", {})
    cleaned["required"] = cleaned.get("required", [])
    return {"type": "object", "properties": cleaned["properties"], "required": cleaned["required"]}


def get_tool_schemas() -> list[dict[str, Any]]:
    """Tool schemas in OpenAI function-calling format (Nemotron, Groq)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.json_schema(),
            },
        }
        for t in TOOLS
    ]


def get_gemini_tool_schemas() -> list[dict[str, Any]]:
    """Tool schemas in Gemini function-declaration format (no ``default`` keys)."""

    def strip_defaults(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip_defaults(v) for k, v in node.items() if k != "default"}
        if isinstance(node, list):
            return [strip_defaults(n) for n in node]
        return node

    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": strip_defaults(copy.deepcopy(t.json_schema())),
        }
        for t in TOOLS
    ]


# --------------------------------------------------------------------------- #
# Lookup and dispatch
# --------------------------------------------------------------------------- #


def get_tool_tiers() -> dict[str, str]:
    """Mapping of tool name to permission tier."""
    return {t.name: t.tier for t in TOOLS}


def get_tool_tier(tool_name: str) -> str:
    """Tier for a tool. Unknown tools default to the most restrictive tier."""
    tool = _BY_NAME.get(tool_name)
    return tool.tier if tool else DEFAULT_TIER


def get_tool(name: str) -> Tool:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"Tool not found: {name}") from None


def get_tool_function(name: str) -> Callable[..., dict[str, Any]]:
    """The Python function behind a tool."""
    return get_tool(name).function


def validate_tool_args(name: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and coerce raw (LLM-supplied) arguments for a tool.

    Raises ``ValueError`` for unknown tools and ``pydantic.ValidationError``
    for bad arguments.
    """
    tool = get_tool(name)
    model = tool.args.model_validate(params or {})
    return model.model_dump(exclude_unset=False)


def _format_validation_error(name: str, err: ValidationError) -> str:
    problems = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "arguments"
        problems.append(f"{loc}: {e['msg']}")
    return f"Invalid arguments for {name}: " + "; ".join(problems)


def execute_tool(name: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Validate arguments and run a tool.

    Returns ``{"success": False, "error": ...}`` for invalid arguments; raises
    ``ValueError`` only for tool names that don't exist.
    """
    tool = get_tool(name)
    try:
        args = validate_tool_args(name, params)
    except ValidationError as err:
        return {"success": False, "error": _format_validation_error(name, err)}
    return tool.function(**args)
