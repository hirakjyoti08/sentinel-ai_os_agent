"""Shared fixtures: a scripted LLM, an isolated audit DB and a disposable process."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from typing import Any

import psutil
import pytest

from sentinel.llm.providers import AllProvidersFailed, LLMResponse, ToolCall
from sentinel.storage import audit_log


class ScriptedLLM:
    """Stands in for ``FallbackChain``: replays canned responses, records every call."""

    def __init__(self, responses: Sequence[LLMResponse | Exception] = ()):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, *, use_tools=True, on_token=None) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "use_tools": use_tools})
        if not self.responses:
            raise AllProvidersFailed(["script exhausted"])
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if nxt.streamed and on_token:
            on_token(nxt.text)
        return nxt

    def status(self) -> dict[str, dict[str, Any]]:
        return {"fake": {"configured": True, "available": True, "hint": ""}}


def tool_call(name: str, call_id: str = "call_0", **arguments: Any) -> LLMResponse:
    import json

    return LLMResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments,
                             raw_arguments=json.dumps(arguments))],
        provider="fake",
    )


def text_reply(text: str, *, streamed: bool = False) -> LLMResponse:
    return LLMResponse(text=text, provider="fake", streamed=streamed)


@pytest.fixture
def audit_db(tmp_path, monkeypatch) -> Iterator[str]:
    """Point the audit log at a fresh SQLite file for this test."""
    path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DB_PATH", path)
    audit_log.init_db()
    yield path


def audit_rows(path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]
    conn.close()
    return rows


@pytest.fixture
def sleeper() -> Iterator[psutil.Process]:
    """A harmless child process that tests may pause, resume or kill."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    p = psutil.Process(proc.pid)
    yield p
    try:
        p.resume()
    except psutil.Error:
        pass
    proc.kill()
    proc.wait(timeout=5)


@pytest.fixture
def make_agent(monkeypatch):
    """Build an ``Agent`` wired to a :class:`ScriptedLLM` (no network)."""
    monkeypatch.setenv("SENTINEL_DESKTOP_ALERTS", "false")
    from sentinel.agent.core import Agent

    def _make(*responses: LLMResponse | Exception) -> tuple[Agent, ScriptedLLM]:
        llm = ScriptedLLM(responses)
        agent = Agent(llm=llm)  # type: ignore[arg-type]
        monkeypatch.setattr("sentinel.agent.core.emit_words", lambda text, cb, delay=0: cb and cb(text))
        return agent, llm

    return _make
