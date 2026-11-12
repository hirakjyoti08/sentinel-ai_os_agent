"""Rules and incident reports through the Agent, end to end."""

import json
from types import SimpleNamespace as NS

import psutil
import pytest
from conftest import audit_rows, text_reply

from sentinel.monitor.watcher import AnomalyEvent

PAUSE_RULE = "if any process uses over 90% CPU for 5 minutes, pause it and notify me"


def hot_metrics(pid, name="sleeper", cpu=180.0, mem=5.0):
    proc = {"pid": pid, "name": name, "cpu_percent": cpu, "memory_percent": mem}
    return NS(cpu_percent=97.0, memory_percent=60.0, disk_percent=40.0, gpu_percent=None,
              top_processes=[proc], top_memory_processes=[proc])


def save_rule(agent, text):
    draft = agent.chat(text)
    assert "Type **yes** to save it" in draft, draft
    assert agent.chat("yes").startswith("✅ Rule")
    return agent.rules.all()[-1]


# --------------------------------------------------------------------------- #
# Creating rules from chat
# --------------------------------------------------------------------------- #


def test_rule_is_only_saved_after_yes(make_agent, audit_db):
    agent, llm = make_agent()
    draft = agent.chat(PAUSE_RULE)
    assert "When any process uses above 90% CPU for 5 min: pause it, notify you" in draft
    assert "undone with Ctrl+U" in draft  # explains the automatic action
    assert agent.rules.all() == []

    saved = agent.chat("yes")
    assert saved.startswith("✅ Rule") and len(agent.rules.all()) == 1
    assert llm.calls == []  # parsed locally, no LLM round trip


def test_saying_no_discards_the_draft(make_agent, audit_db):
    agent, _ = make_agent()
    agent.chat(PAUSE_RULE)
    assert agent.chat("no") == "Okay, I discarded that rule."
    assert agent.rules.all() == [] and agent.pending_rule is None


def test_another_request_drops_the_draft_and_is_answered(make_agent, audit_db):
    agent, llm = make_agent(text_reply("Here's your answer."))
    agent.chat(PAUSE_RULE)
    assert agent.chat("tell me something useful about this machine") == "Here's your answer."
    assert agent.rules.all() == [] and agent.pending_rule is None


def test_destructive_rule_is_refused(make_agent, audit_db):
    agent, _ = make_agent()
    reply = agent.chat("if spotify uses over 50% cpu then kill it")
    assert "can't kill, delete or close" in reply and agent.pending_rule is None


def test_unusual_wording_goes_to_the_llm(make_agent, audit_db):
    agent, llm = make_agent(text_reply(json.dumps({
        "condition": {"scope": "system", "metric": "memory", "threshold": 85, "duration_seconds": 120},
        "actions": [{"type": "notify"}],
    })))
    draft = agent.chat("rule: warn me if my mac keeps running out of memory for a couple of minutes")
    assert "system memory is above 85% for 2 min" in draft
    assert len(llm.calls) == 1


def test_list_disable_enable_delete(make_agent, audit_db):
    agent, _ = make_agent()
    assert "no automation rules yet" in agent.chat("list rules")
    r = save_rule(agent, "if disk is above 90%, alert me")

    listing = agent.chat("show my rules")
    assert r.id in listing and "| active |" in listing

    assert agent.chat(f"disable rule {r.id}").startswith("⏸️ Disabled")
    assert agent.rules.get(r.id).enabled is False
    assert agent.chat("enable rule 1").startswith("▶️ Enabled")
    assert agent.chat("delete rule #1").startswith("🗑️ Deleted")
    assert agent.rules.all() == []
    assert "No rule 'zzz'" in agent.chat("delete rule zzz")


# --------------------------------------------------------------------------- #
# Rules acting on a real process
# --------------------------------------------------------------------------- #


def test_pause_rule_pauses_process_audits_and_can_be_undone(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    save_rule(agent, "if any process uses over 90% cpu, pause it and notify me")

    (run,) = agent.run_rules(hot_metrics(sleeper.pid))
    assert sleeper.status() == psutil.STATUS_STOPPED
    assert run.notify and run.outcomes == [f"paused sleeper (PID {sleeper.pid})"]
    assert "Rule" in run.message and "paused sleeper" in run.message

    (row,) = audit_rows(audit_db)
    assert (row["action"], row["tier"], row["status"]) == ("pause_process", "reversible", "executed")

    assert agent.undo_last().startswith("Undid pause_process")
    assert sleeper.status() != psutil.STATUS_STOPPED

    # Cooldown: the same process doesn't get paused again on the next tick.
    assert agent.run_rules(hot_metrics(sleeper.pid)) == []


def test_lower_priority_rule_renices_and_warns_it_is_one_way(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    draft = agent.chat("if any process uses over 90% cpu, lower its priority")
    assert "can't be raised back without admin rights" in draft
    agent.chat("yes")
    (run,) = agent.run_rules(hot_metrics(sleeper.pid))
    assert sleeper.nice() == 10 and not run.notify

    reply = agent.undo_last()
    if sleeper.nice() == 10:  # unprivileged (the normal case): explained, not a bare error
        assert "needs admin rights" in reply
    else:  # running as root: undo really restores it
        assert reply.startswith("Undid set_priority")


def test_rule_on_vanished_process_reports_failure(make_agent, audit_db):
    agent, _ = make_agent()
    save_rule(agent, "if any process uses over 90% cpu, pause it")
    (run,) = agent.run_rules(hot_metrics(99_999_999, name="ghost"))
    assert run.outcomes[0].startswith("couldn't act on ghost (PID 99999999)")


def test_notify_only_rule_takes_no_action(make_agent, audit_db, sleeper):
    agent, _ = make_agent()
    save_rule(agent, "if cpu is above 90%, notify me")
    (run,) = agent.run_rules(hot_metrics(sleeper.pid))
    assert run.notify and run.outcomes == []
    assert sleeper.status() != psutil.STATUS_STOPPED and audit_rows(audit_db) == []


# --------------------------------------------------------------------------- #
# Incident reports
# --------------------------------------------------------------------------- #


@pytest.fixture
def anomaly(sleeper):
    return AnomalyEvent("cpu", "High CPU usage: 97.0%", offending_pid=sleeper.pid,
                        offending_name="sleeper", offending_metric="88.0% CPU")


def incident_metrics():
    return NS(cpu_percent=97.0, memory_percent=60.0, memory_used_gb=9.6, memory_total_gb=16.0,
              disk_percent=40.0, disk_free_gb=300.0, gpu_percent=None, cpu_cores=10,
              uptime_hours=12.0, open_ports=[], top_processes=[])


def test_no_report_before_any_alert(make_agent, audit_db):
    agent, _ = make_agent()
    assert "no incident report yet" in agent.chat("explain the last alert")


def test_report_is_written_and_retrievable_from_chat(make_agent, audit_db, anomaly, monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    agent, llm = make_agent(text_reply(json.dumps({
        "title": "Runaway script", "what_happened": "CPU hit 97%.",
        "likely_cause": "A just-started Python script is busy.", "recommendation": "Type 'show top 5'.",
    })))
    report = agent.create_incident_report(anomaly, incident_metrics())
    assert report.source == "llm" and report.title == "Runaway script"
    assert "88.0% CPU" in llm.calls[0]["messages"][0]["content"]

    md = agent.chat("what caused that alert?")
    assert md.startswith("🧾 **Incident report — Runaway script")
    assert len(llm.calls) == 1  # answered from history, not a new LLM call


def test_report_falls_back_to_local_when_llm_down(make_agent, audit_db, anomaly, monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "llm")
    agent, _ = make_agent()  # every LLM call fails
    report = agent.create_incident_report(anomaly, incident_metrics())
    assert report.source == "local" and "started only" in report.likely_cause


def test_reports_can_be_turned_off(make_agent, audit_db, anomaly, monkeypatch):
    monkeypatch.setenv("SENTINEL_INCIDENT_REPORTS", "off")
    agent, _ = make_agent()
    assert agent.create_incident_report(anomaly, incident_metrics()) is None
    assert not agent.incidents
