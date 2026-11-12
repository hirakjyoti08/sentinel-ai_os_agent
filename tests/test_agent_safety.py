"""The safety-critical paths, driven end to end with a scripted LLM.

These tests run real tools against a disposable child process and a
temporary audit database, so they check what actually happens to the
system, not just what the agent says.
"""

import json

import psutil
import pytest
from conftest import audit_rows, text_reply, tool_call

from sentinel.agent.confirmations import parse_confirm_kill
from sentinel.agent.intents import RoutingContext, route
from sentinel.llm.providers import AllProvidersFailed

NO_INTENT = "tell me something useful about this machine"


def test_prompt_used_for_llm_tests_has_no_local_intent():
    assert route(RoutingContext.from_input(NO_INTENT)) is None


# --------------------------------------------------------------------------- #
# Tool loop
# --------------------------------------------------------------------------- #


def test_read_only_tool_runs_and_result_goes_back_to_llm(make_agent, audit_db):
    agent, llm = make_agent(tool_call("get_cpu_usage"), text_reply("CPU looks fine."))
    assert agent.chat(NO_INTENT) == "CPU looks fine."

    second_turn = llm.calls[1]["messages"]
    assert second_turn[-2]["tool_calls"][0]["function"]["name"] == "get_cpu_usage"
    tool_msg = second_turn[-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_0"
    assert "cpu_percent" in json.loads(tool_msg["content"])

    (row,) = audit_rows(audit_db)
    assert row["action"] == "get_cpu_usage" and row["tier"] == "read_only"


def test_streamed_reply_is_not_emitted_twice(make_agent, audit_db):
    agent, _ = make_agent(text_reply("streamed answer", streamed=True))
    tokens = []
    agent.chat(NO_INTENT, on_token=tokens.append)
    assert tokens == ["streamed answer"]


def test_tool_loop_stops_after_max_rounds(make_agent, audit_db):
    agent, llm = make_agent(*[tool_call("get_cpu_usage") for _ in range(10)])
    assert "Max tool calls reached" in agent.chat(NO_INTENT)
    assert len(llm.calls) == 5


# --------------------------------------------------------------------------- #
# Destructive and reversible actions requested by the LLM
# --------------------------------------------------------------------------- #


def test_llm_kill_request_becomes_confirmation_and_kills_nothing(make_agent, audit_db, sleeper):
    agent, llm = make_agent(tool_call("kill_process", pid=sleeper.pid))
    reply = agent.chat(NO_INTENT)

    payload = parse_confirm_kill(reply)
    assert payload == {
        "type": "confirm_kill", "pid": sleeper.pid, "name": sleeper.name(),
        "reason": f"kill requested by assistant for PID {sleeper.pid}", "metric": "llm_requested",
    }
    assert sleeper.is_running() and sleeper.status() != psutil.STATUS_ZOMBIE
    assert audit_rows(audit_db) == []
    assert len(llm.calls) == 1


def test_llm_kill_with_string_pid_is_still_intercepted(make_agent, audit_db, sleeper):
    agent, _ = make_agent(tool_call("kill_process", pid=str(sleeper.pid)))
    assert parse_confirm_kill(agent.chat(NO_INTENT))["pid"] == sleeper.pid
    assert sleeper.is_running()


def test_reversible_tool_without_confirmation_is_refused(make_agent, audit_db, sleeper):
    agent, llm = make_agent(tool_call("pause_process", pid=sleeper.pid), text_reply("ok"))
    agent.chat(NO_INTENT)

    assert sleeper.status() != psutil.STATUS_STOPPED
    assert "confirmation required" in llm.calls[1]["messages"][-1]["content"]
    assert audit_rows(audit_db) == []


def test_unknown_tool_is_treated_as_destructive(make_agent, audit_db):
    agent, llm = make_agent(tool_call("format_disk", device="/dev/disk0"), text_reply("ok"))
    agent.chat(NO_INTENT)
    assert "confirmation required" in llm.calls[1]["messages"][-1]["content"]
    assert audit_rows(audit_db) == []


# --------------------------------------------------------------------------- #
# Permission pipeline
# --------------------------------------------------------------------------- #


def test_confirmed_pause_is_logged_with_undo_data_then_undone(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    result = agent._execute_tool_with_permissions("pause_process", {"pid": sleeper.pid}, confirmed=True)
    assert result["success"] and sleeper.status() == psutil.STATUS_STOPPED

    (row,) = audit_rows(audit_db)
    assert row["tier"] == "reversible" and row["status"] == "executed"
    assert json.loads(row["undo_data"])["pid"] == sleeper.pid

    assert agent.undo_last().startswith("Undid pause_process")
    assert sleeper.status() != psutil.STATUS_STOPPED
    assert audit_rows(audit_db)[0]["status"] == "undone"


def test_protected_process_is_refused_even_when_confirmed(make_agent, audit_db):
    agent, _ = make_agent()
    result = agent._execute_tool_with_permissions("pause_process", {"pid": 1}, confirmed=True)
    assert not result["success"] and "denied" in result["error"].lower()
    assert audit_rows(audit_db)[0]["status"] == "failed"


def test_circuit_breaker_blocks_destructive_actions(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    for _ in range(agent.circuit_breaker.max_actions):
        agent.circuit_breaker.record_action()

    result = agent._execute_tool_with_permissions("kill_process", {"pid": sleeper.pid}, confirmed=True)
    assert not result["success"] and "Circuit breaker" in result["error"]
    assert sleeper.is_running()
    assert audit_rows(audit_db) == []

    # Read-only tools are never rate limited.
    assert agent._execute_tool_with_permissions("get_cpu_usage", {})["success"]


def test_confirmed_kill_counts_towards_circuit_breaker(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    result = agent._execute_tool_with_permissions("kill_process", {"pid": sleeper.pid}, confirmed=True)
    assert result["success"]
    assert agent.circuit_breaker.get_remaining_actions() == agent.circuit_breaker.max_actions - 1
    (row,) = audit_rows(audit_db)
    assert row["tier"] == "destructive" and json.loads(row["undo_data"])["pid"] == sleeper.pid


def test_invalid_arguments_fail_cleanly_and_are_audited(make_agent, audit_db):
    agent, _ = make_agent()
    result = agent._execute_tool_with_permissions("get_top_memory_processes", {"n": "lots"})
    assert not result["success"] and "Invalid arguments" in result["error"]
    assert audit_rows(audit_db)[0]["status"] == "failed"


def test_undo_with_nothing_to_undo(make_agent, audit_db):
    agent, _ = make_agent()
    assert agent.undo_last() == "No reversible or destructive actions to undo."


# --------------------------------------------------------------------------- #
# Fast path and offline fallback
# --------------------------------------------------------------------------- #


def test_kill_by_name_uses_fast_path_without_llm(make_agent, audit_db, sleeper):
    agent, llm = make_agent()
    payload = parse_confirm_kill(agent.chat(f"kill pid {sleeper.pid}"))
    assert payload["pid"] == sleeper.pid and payload["metric"] == "user_requested"
    assert llm.calls == [] and sleeper.is_running()


def test_fast_path_refuses_protected_process(make_agent, audit_db):
    agent, llm = make_agent()
    assert "Cannot kill system process" in agent.chat("kill pid 1")
    assert llm.calls == []


def test_offline_fallback_answers_known_intents(make_agent, audit_db):
    agent, _ = make_agent(AllProvidersFailed(["down"]))
    assert "CPU Utilization" in agent.chat("what is my cpu usage")


def test_offline_with_unknown_request_explains_how_to_fix(make_agent, audit_db):
    agent, _ = make_agent(AllProvidersFailed(["Nemotron not configured"]))
    reply = agent.chat(NO_INTENT)
    assert reply.startswith("Error: All LLM providers are currently unavailable")
    assert "Nemotron not configured" in reply


@pytest.fixture
def fake_cache_cleaner(monkeypatch):
    """Swap the real cleanup tool in the registry so nothing on disk is touched."""
    import dataclasses

    from sentinel.tools import registry

    ran = []
    real = registry.get_tool("clean_developer_caches")
    fake = dataclasses.replace(real, function=lambda **kw: ran.append(kw) or {
        "success": True, "count": 1, "reclaimed_str": "1 B"})
    monkeypatch.setitem(registry._BY_NAME, "clean_developer_caches", fake)
    return ran


def test_offline_cache_cleanup_goes_through_permissions(make_agent, audit_db, fake_cache_cleaner):
    agent, _ = make_agent(AllProvidersFailed(["down"]))
    reply = agent.chat("clean developer caches all")
    assert fake_cache_cleaner == []  # reversible tier needs confirmation; none was given
    assert "Error cleaning caches" in reply


def test_offline_cache_cleanup_runs_once_confirmed(make_agent, audit_db, fake_cache_cleaner, monkeypatch):
    monkeypatch.setattr("sentinel.agent.core.confirm_action", lambda *a, **k: True)
    agent, _ = make_agent(AllProvidersFailed(["down"]))
    assert "Successfully cleaned 1" in agent.chat("clean developer caches pip")
    assert fake_cache_cleaner == [{"targets": ["pip"]}]
    assert audit_rows(audit_db)[0]["action"] == "clean_developer_caches"


@pytest.mark.parametrize("text", ["hi", "hello", "who are you"])
def test_greetings_never_reach_the_llm(make_agent, audit_db, text):
    agent, llm = make_agent()
    assert agent.chat(text)
    assert llm.calls == []
