"""Routing table: which request goes to which local intent, and in what priority."""

import pytest

from sentinel.agent.intents import AGENT_INTENTS, INTENT_NAMES, RoutingContext, route
from sentinel.agent.local_handlers import GUARDED_HANDLERS, HANDLERS


def ctx(text: str, *replies: str) -> RoutingContext:
    return RoutingContext.from_input(text, replies)


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        # Greetings and identity
        ("hi", "greeting"),
        ("Hello there!", "greeting"),
        ("good morning sentinel", "greeting"),
        ("who are you", "greeting"),
        # Health overview
        ("how is my pc", "system_health"),
        ("give me a system health check", "system_health"),
        # Destructive intents, most specific first
        ("kill highest cpu", "kill_highest_cpu"),
        ("kill cpu hog", "kill_highest_cpu"),
        ("kill highest memory", "kill_highest_memory"),
        ("kill high ram", "kill_highest_memory"),
        ("close port 3000", "close_port"),
        ("can you stop whatever is on port 8080", "close_port"),
        ("free port 5000", "close_port"),
        ("kill spotify", "kill_process"),
        ("kill pid 4242", "kill_process"),
        ("PID: 4242", "kill_process"),
        ("terminate node", "kill_process"),
        ("kill all chrome", "kill_process"),
        # Read-only telemetry
        ("which process is using the most cpu", "top_cpu_question"),
        ("show top 5", "top_n_processes"),
        ("what is my cpu usage", "cpu_usage"),
        ("battery status", "battery_thermal"),
        ("is my mac overheating", "battery_thermal"),
        ("top 3 memory processes", "memory"),
        ("how much ram is used", "memory"),
        ("free up disk space", "disk_hogs"),
        ("find large folders", "disk_hogs"),
        ("clean developer caches all", "clean_caches"),
        ("purge caches npm", "clean_caches"),
        ("how much storage do i have", "disk_usage"),
        ("gpu usage", "gpu"),
        ("show running processes", "list_processes"),
        ("what is my network speed", "network_bandwidth"),
        ("inspect 1234", "inspect_process"),
        ("list open ports", "open_ports"),
        # Nothing local: goes to the LLM
        ("can i run android studio here", None),
        ("explain what a zombie process is", None),
        ("write me a poem", None),
    ],
)
def test_route(text, intent):
    assert route(ctx(text)) == intent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "kill highest cpu" must not be treated as killing a process named "highest cpu"
        ("kill highest cpu", "kill_highest_cpu"),
        # port requests beat generic kill-by-name
        ("kill port 3000", "close_port"),
        # "top 5 memory" is a memory question, not top-5-by-CPU
        ("show top 5 memory hogs", "memory"),
        # cpu with "process" in it is a top-process question, not the CPU scalar
        ("what process uses most cpu", "top_cpu_question"),
    ],
)
def test_priority_order(text, expected):
    assert route(ctx(text)) == expected


def test_bare_number_is_a_pid_only_after_agent_asked():
    assert route(ctx("4242")) is None
    assert route(ctx("4242", "Multiple processes match. Please specify PID.")) == "kill_process"


@pytest.mark.parametrize("text", ["what is my cpu usage", "how is my pc", "list open ports", "hi"])
def test_fast_path_ignores_read_only_intents(text):
    assert route(ctx(text), fast_path_only=True) is None


@pytest.mark.parametrize("text", ["kill spotify", "close port 3000", "kill highest memory"])
def test_fast_path_catches_destructive_intents(text):
    assert route(ctx(text), fast_path_only=True) is not None


def test_suggested_cleanup_command_actually_cleans():
    # The disk-hogs reply tells the user to type this; it used to loop back to disk_hogs.
    assert route(ctx("clean developer caches all")) == "clean_caches"


def test_every_intent_has_exactly_one_handler():
    groups = [set(HANDLERS), set(GUARDED_HANDLERS), set(AGENT_INTENTS)]
    assert set(INTENT_NAMES) == set().union(*groups)
    assert sum(len(g) for g in groups) == len(INTENT_NAMES)
