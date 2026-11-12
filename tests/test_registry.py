"""Tool registry: typed arguments, generated schemas and tiers."""

import pytest

from sentinel.tools.registry import (
    TOOLS,
    execute_tool,
    get_gemini_tool_schemas,
    get_tool_schemas,
    get_tool_tier,
    validate_tool_args,
)

EXPECTED_TIERS = {
    "read_only": {
        "list_processes", "get_disk_usage", "get_cpu_usage", "get_memory_usage",
        "get_system_info", "get_battery_and_thermal", "get_top_memory_processes",
        "list_open_ports", "get_gpu_usage", "get_network_bandwidth", "analyze_disk_hogs",
    },
    "reversible": {
        "pause_process", "resume_process", "set_priority", "clear_cache_dir",
        "clean_developer_caches",
    },
    "destructive": {"kill_process", "delete_file", "stop_service", "close_port"},
}


def test_every_tool_has_the_expected_tier():
    actual: dict[str, set[str]] = {}
    for t in TOOLS:
        actual.setdefault(t.tier, set()).add(t.name)
    assert actual == EXPECTED_TIERS


def test_unknown_tools_default_to_destructive():
    assert get_tool_tier("rm_rf_everything") == "destructive"


def test_schemas_are_generated_from_models():
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in get_tool_schemas()}
    assert by_name["kill_process"] == {
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "Process ID"},
            "signal": {"type": "integer", "description": "Signal number", "default": 9},
        },
        "required": ["pid"],
    }
    assert by_name["list_processes"] == {"type": "object", "properties": {}, "required": []}
    # Optional lists are plain arrays, not anyOf[..., null]
    assert by_name["analyze_disk_hogs"]["properties"]["custom_paths"] == {
        "type": "array", "items": {"type": "string"},
        "description": "Optional list of specific directories to inspect",
    }


def test_gemini_schemas_have_no_defaults():
    def has_default(node):
        if isinstance(node, dict):
            return "default" in node or any(has_default(v) for v in node.values())
        return isinstance(node, list) and any(has_default(v) for v in node)

    assert not has_default(get_gemini_tool_schemas())


def test_arguments_are_coerced_and_extras_dropped():
    assert validate_tool_args("get_top_memory_processes", {"n": "3", "made_up": True}) == {"n": 3}
    assert validate_tool_args("set_priority", {"pid": 42}) == {"pid": 42, "nice": 0}


@pytest.mark.parametrize(
    ("tool", "params", "problem"),
    [
        ("kill_process", {}, "pid: Field required"),
        ("kill_process", {"pid": "abc"}, "pid: Input should be a valid integer"),
        ("kill_process", {"pid": 0}, "pid: Input should be greater than or equal to 1"),
        ("set_priority", {"pid": 42, "nice": 50}, "nice: Input should be less than or equal to 19"),
        ("close_port", {"port": 70000}, "port: Input should be less than or equal to 65535"),
        ("delete_file", {"path": ""}, "path: String should have at least 1 character"),
    ],
)
def test_invalid_arguments_return_errors_instead_of_raising(tool, params, problem):
    result = execute_tool(tool, params)
    assert result["success"] is False
    assert result["error"].startswith(f"Invalid arguments for {tool}: ")
    assert problem in result["error"]


def test_unknown_tool_raises():
    with pytest.raises(ValueError, match="Tool not found"):
        execute_tool("nope", {})
