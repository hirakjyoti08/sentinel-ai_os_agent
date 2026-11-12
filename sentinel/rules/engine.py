"""Evaluate rules against telemetry snapshots — locally, no LLM.

The engine is a pure state machine over :class:`SystemMetrics`: it tracks how
long each (rule, target) condition has held, fires once the duration is met,
then stays quiet for the rule's cooldown. Executing the actions is the
caller's job (see :meth:`sentinel.agent.core.Agent.run_rules`).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psutil

from sentinel.rules.models import METRIC_LABELS, Rule
from sentinel.rules.store import RuleStore


def _own_process_family() -> frozenset[int]:
    """Sentinel and its ancestors (shell, terminal app): pausing any of them freezes Sentinel."""
    pids = {os.getpid()}
    try:
        pids.update(p.pid for p in psutil.Process().parents())
    except psutil.Error:
        pass
    return frozenset(pids)


NEVER_TARGET = _own_process_family()


@dataclass(frozen=True)
class Firing:
    rule: Rule
    value: float  # observed value that met the condition
    target_pid: int | None  # process to act on (None for e.g. disk rules)
    target_name: str | None

    @property
    def summary(self) -> str:
        c = self.rule.condition
        metric = METRIC_LABELS[c.metric]
        who = f"{self.target_name} (PID {self.target_pid})" if self.target_pid else "System"
        if c.scope == "system":
            line = f"System {metric} at {self.value:.1f}% (rule: {c.op} {c.threshold:g}%)"
            if self.target_pid:
                line += f"; top consumer {who}"
            return line
        return f"{who} at {self.value:.1f}% {metric} (rule: > {c.threshold:g}%)"


def _proc_value(proc: dict[str, Any], metric: str) -> float:
    return float(proc.get("cpu_percent" if metric == "cpu" else "memory_percent") or 0.0)


def _process_list(metrics: Any, metric: str) -> list[dict[str, Any]]:
    if metric == "memory":
        return list(getattr(metrics, "top_memory_processes", None) or metrics.top_processes or [])
    return list(metrics.top_processes or [])


def _system_value(metrics: Any, metric: str) -> float | None:
    return {
        "cpu": metrics.cpu_percent,
        "memory": metrics.memory_percent,
        "disk": metrics.disk_percent,
        "gpu": metrics.gpu_percent,
    }[metric]


def _candidates(rule: Rule, metrics: Any) -> Iterator[tuple[str, float, int | None, str | None]]:
    """``(key, value, pid, name)`` for everything this rule should look at."""
    c = rule.condition
    procs = [p for p in _process_list(metrics, c.metric)
             if p.get("pid") and p["pid"] > 1 and p["pid"] not in NEVER_TARGET] if c.metric in ("cpu", "memory") else []
    if c.scope == "system":
        value = _system_value(metrics, c.metric)
        if value is None:
            return
        top = max(procs, key=lambda p: _proc_value(p, c.metric), default=None)
        yield "system", float(value), (top or {}).get("pid"), (top or {}).get("name")
        return
    for p in procs:
        name = (p.get("name") or "").lower()
        if c.process_name and c.process_name not in name:
            continue
        yield f"pid:{p['pid']}", _proc_value(p, c.metric), p["pid"], p.get("name")


class RuleEngine:
    def __init__(self, store: RuleStore, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self._since: dict[tuple[str, str], float] = {}  # condition first seen true
        self._last_fired: dict[tuple[str, str], float] = {}

    def evaluate(self, metrics: Any) -> list[Firing]:
        now = self.clock()
        firings: list[Firing] = []
        live: set[tuple[str, str]] = set()
        for rule in self.store.enabled():
            c = rule.condition
            for key, value, pid, name in _candidates(rule, metrics):
                k = (rule.id, key)
                met = value > c.threshold if c.op == ">" else value < c.threshold
                if not met:
                    continue
                live.add(k)
                start = self._since.setdefault(k, now)
                if now - start < c.duration_seconds:
                    continue
                if now - self._last_fired.get(k, float("-inf")) < rule.cooldown_seconds:
                    continue
                self._last_fired[k] = now
                firings.append(Firing(rule, value, pid, name))
        # Conditions that stopped holding (or processes that exited) start over.
        for k in list(self._since):
            if k not in live:
                del self._since[k]
        return firings

    def reset(self, rule_id: str | None = None) -> None:
        for d in (self._since, self._last_fired):
            for k in [k for k in d if rule_id is None or k[0] == rule_id]:
                del d[k]
