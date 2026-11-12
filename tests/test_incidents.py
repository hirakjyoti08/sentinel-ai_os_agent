"""Root-cause incident reports: context gathering, redaction and report building."""

import json
from types import SimpleNamespace as NS

import pytest
from conftest import ScriptedLLM, text_reply

from sentinel.monitor.incidents import (
    IncidentContext,
    build_report,
    gather_context,
    local_report,
    redact_cmdline,
    rule_friendly_name,
)
from sentinel.monitor.watcher import AnomalyEvent
from sentinel.rules import parse_rule_locally


def fake_metrics(**over):
    base = dict(cpu_percent=96.0, memory_percent=60.0, memory_used_gb=9.6, memory_total_gb=16.0,
                disk_percent=50.0, disk_free_gb=200.0, gpu_percent=None, cpu_cores=10,
                uptime_hours=30.0, open_ports=[], top_processes=[])
    base.update(over)
    return NS(**base)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (["/usr/bin/python3", "/Users/me/app/server.py", "--port", "8000"],
         "python3 server.py --port 8000"),
        (["node", "--api-key", "sk-live-abc", "index.js"], "node --api-key *** index.js"),
        (["curl", "--password=hunter2", "https://x"], "curl --password=*** x"),
        (["app", "TOKEN=abcdefghijklmnopqrstuvwxyz123456"], "app TOKEN=***"),
        (["app", "ghp_0123456789abcdefghijklmnopqrstuvwxyz"], "app ***"),
        (["a", "1", "2", "3", "4", "5", "6", "7", "8"], "a 1 2 3 4 5 6 (+2 more args)"),
        ([], ""),
    ],
)
def test_redact_cmdline(cmdline, expected):
    assert redact_cmdline(cmdline) == expected


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


def test_gather_context_describes_a_real_culprit(sleeper):
    anomaly = AnomalyEvent("cpu", "High CPU usage: 96.0%", offending_pid=sleeper.pid,
                           offending_name=sleeper.name(), offending_metric="88.0% CPU")
    m = fake_metrics(
        open_ports=[{"pid": sleeper.pid, "port": 8123}, {"pid": 1, "port": 22}],
        top_processes=[{"pid": sleeper.pid, "name": "py", "cpu_percent": 88},
                       {"pid": 777, "name": "slack", "cpu_percent": 5, "memory_percent": 3}],
    )
    ctx = gather_context(anomaly, m, recent_actions=[])
    c = ctx.culprit
    assert c["pid"] == sleeper.pid and c["share"] == "88.0% CPU"
    assert c["listening_ports"] == [8123]
    assert c["running_for_seconds"] < 60 and "python" in c["command"].lower()
    assert [p["pid"] for p in ctx.other_top_processes] == [777]  # culprit not repeated
    assert "environ" not in json.dumps(c)


def test_gather_context_survives_vanished_process():
    anomaly = AnomalyEvent("cpu", "High CPU", offending_pid=99_999_999, offending_name="gone")
    assert gather_context(anomaly, fake_metrics(), []).culprit is None


def test_only_recent_non_read_only_actions_are_included():
    import time
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
    actions = [
        {"action": "pause_process", "tier": "reversible", "status": "executed", "timestamp": now},
        {"action": "get_cpu_usage", "tier": "read_only", "status": "executed", "timestamp": now},
        {"action": "kill_process", "tier": "destructive", "status": "executed", "timestamp": old},
    ]
    ctx = gather_context(AnomalyEvent("cpu", "x"), fake_metrics(), actions)
    assert [a["action"] for a in ctx.recent_actions] == ["pause_process"]


# --------------------------------------------------------------------------- #
# Local analysis
# --------------------------------------------------------------------------- #


def ctx(category="cpu", culprit=None, **kw):
    system = {"cpu_percent": 96, "memory_percent": 60, "memory_used_gb": 9.6, "memory_total_gb": 16,
              "disk_percent": 97, "disk_free_gb": 3.1, "gpu_percent": None, "cpu_cores": 10,
              "uptime_hours": 30}
    return IncidentContext(category=category, alert=kw.pop("alert", "High CPU usage: 96.0%"),
                           detected_at=0, system=system, culprit=culprit, **kw)


def culprit(**over):
    base = {"pid": 4242, "name": "Google Chrome Helper (Renderer)", "running_for": "2 min",
            "running_for_seconds": 120, "cpu_percent": 88, "memory_mb": 900, "child_processes": 0,
            "listening_ports": [], "share": "88.0% CPU"}
    base.update(over)
    return base


def test_recently_started_process_is_called_out():
    r = local_report(ctx(culprit=culprit()))
    assert "started only 2 min ago" in r.likely_cause
    assert r.title == "High CPU from Google Chrome Helper (Renderer)" and r.source == "local"


def test_child_processes_and_ports_are_called_out():
    r = local_report(ctx(culprit=culprit(running_for_seconds=9000, child_processes=24,
                                          listening_ports=[3000])))
    assert "24 child processes" in r.likely_cause and "port(s) 3000" in r.likely_cause


def test_disk_report_suggests_cleanup():
    r = local_report(ctx("disk", alert="High disk usage: 97%"))
    assert "3.1 GB free" in r.what_happened and "free up disk space" in r.recommendation


def test_suggested_rule_is_parseable():
    # The report suggests a rule; typing it back must actually work.
    import re
    r = local_report(ctx(culprit=culprit()))
    suggestion = re.search(r"rule such as '(.+?)'\.?$", r.recommendation).group(1)
    rule = parse_rule_locally(suggestion)
    assert rule.condition.process_name == "google chrome"
    assert [a.type for a in rule.actions] == ["pause_process", "notify"]


def test_rule_friendly_name():
    assert rule_friendly_name("Google Chrome Helper (Renderer)") == "google chrome"
    assert rule_friendly_name("node") == "node"


def test_recent_action_is_mentioned():
    r = local_report(ctx(culprit=culprit(), recent_actions=[
        {"action": "set_priority", "minutes_ago": 3.0, "status": "executed"}]))
    assert "'set_priority' was run 3 min ago" in r.likely_cause


# --------------------------------------------------------------------------- #
# LLM report
# --------------------------------------------------------------------------- #


def test_llm_report_is_used_and_sent_redacted_facts(monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    llm = ScriptedLLM([text_reply(json.dumps({
        "title": "Chrome tab spinning", "what_happened": "CPU hit 96%.",
        "likely_cause": "A renderer is looping.", "recommendation": "Type 'kill process 4242'.",
    }))])
    c = ctx(culprit=culprit(command="Google Chrome --token=***"))
    r = build_report(c, llm)
    assert (r.source, r.title, r.likely_cause) == ("llm", "Chrome tab spinning", "A renderer is looping.")
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "--token=***" in prompt and llm.calls[0]["use_tools"] is False


def test_unclear_llm_cause_is_replaced_by_local_observations(monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    llm = ScriptedLLM([text_reply(json.dumps({
        "title": "t", "what_happened": "w", "likely_cause": "Unclear.", "recommendation": "r"}))])
    r = build_report(ctx(culprit=culprit()), llm)
    assert "started only 2 min ago" in r.likely_cause
    assert "started only 2 min ago" in llm.calls[0]["messages"][0]["content"]  # given as observation


@pytest.mark.parametrize("reply", ["not json", '{"title": "only a title"}'])
def test_bad_llm_output_falls_back_per_field(monkeypatch, reply):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    r = build_report(ctx(culprit=culprit()), ScriptedLLM([text_reply(reply)]))
    assert r.likely_cause  # never empty
    assert r.recommendation


def test_llm_failure_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    assert build_report(ctx(culprit=culprit()), ScriptedLLM([])).source == "local"


def test_local_mode_never_calls_the_llm(monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "local")
    llm = ScriptedLLM([text_reply("{}")])
    assert build_report(ctx(culprit=culprit()), llm).source == "local"
    assert llm.calls == []


def test_markdown_rendering():
    md = local_report(ctx(culprit=culprit())).to_markdown()
    assert md.startswith("🧾 **Incident report — High CPU")
    assert "**Likely cause:**" in md and "local analysis" in md
