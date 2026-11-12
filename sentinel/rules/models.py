"""Structured automation rules.

A rule is *condition → actions*, e.g. "any process above 90% CPU for 5
minutes → pause it and notify me". Rules are created from natural language,
confirmed by the user once, then evaluated locally on every telemetry tick.

Safety: the action set is deliberately limited to non-destructive steps:
``notify`` (changes nothing), ``pause_process`` (undoable with Ctrl+U) and
``lower_priority`` (renice to 10; harmless, but raising it back needs admin
rights, so it lasts until the process restarts). Killing, deleting or
closing anything can't be expressed in a rule.
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scope = Literal["system", "process"]
Metric = Literal["cpu", "memory", "disk", "gpu"]
Op = Literal[">", "<"]
ActionType = Literal["notify", "pause_process", "lower_priority"]

PROCESS_ACTIONS: frozenset[str] = frozenset({"pause_process", "lower_priority"})
LOWER_PRIORITY_NICE = 10
METRIC_LABELS = {"cpu": "CPU", "memory": "memory", "disk": "disk", "gpu": "GPU"}


class RuleError(ValueError):
    """A rule that can't be created, with a message suitable for the user."""


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Scope = Field(
        description="'system' for machine-wide usage, 'process' to check each process"
    )
    metric: Metric = Field(description="Resource to watch")
    op: Op = Field(">", description="'>' for above the threshold, '<' for below")
    threshold: float = Field(ge=0, le=10_000, description="Percent value to compare against")
    duration_seconds: int = Field(
        0, ge=0, le=86_400, description="How long the condition must hold before firing"
    )
    process_name: str | None = Field(
        None, description="Only processes whose name contains this text (process scope only)"
    )

    @model_validator(mode="after")
    def _check(self) -> Condition:
        if self.scope == "process":
            if self.metric not in ("cpu", "memory"):
                raise ValueError("per-process rules can watch cpu or memory only")
            if self.op != ">":
                raise ValueError("per-process rules can only use '>'")
        else:
            if self.threshold > 100:
                raise ValueError("system-wide thresholds are percentages (0-100)")
            if self.process_name:
                raise ValueError("process_name needs scope 'process'")
        if self.process_name is not None:
            self.process_name = self.process_name.strip().lower() or None
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType = Field(
        description="'notify' (alert only), 'pause_process' (SIGSTOP, undoable) or "
        "'lower_priority' (renice to 10)"
    )


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:6])
    text: str = Field(description="The user's original wording")
    condition: Condition
    actions: list[Action] = Field(min_length=1)
    cooldown_seconds: int = Field(300, ge=10, le=86_400)
    enabled: bool = True
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def _check(self) -> Rule:
        targets_process = any(a.type in PROCESS_ACTIONS for a in self.actions)
        if targets_process and self.condition.metric not in ("cpu", "memory"):
            raise ValueError("pause/lower priority need a cpu or memory condition to pick a process")
        if targets_process and self.condition.op == "<":
            raise ValueError("pause/lower priority only make sense for usage above a threshold")
        seen: set[str] = set()
        self.actions = [a for a in self.actions if not (a.type in seen or seen.add(a.type))]
        return self

    # ------------------------------------------------------------------ #

    def describe(self) -> str:
        """One-line human summary, e.g. "When any process uses > 90% CPU for 5 min: pause it, notify"."""
        c = self.condition
        metric = METRIC_LABELS[c.metric]
        comparison = "above" if c.op == ">" else "below"
        if c.scope == "process":
            who = f"a process matching '{c.process_name}'" if c.process_name else "any process"
            subject = f"{who} uses {comparison} {c.threshold:g}% {metric}"
        else:
            subject = f"system {metric} is {comparison} {c.threshold:g}%"
        if c.duration_seconds:
            subject += f" for {format_duration(c.duration_seconds)}"
        actions = []
        for a in self.actions:
            actions.append({
                "notify": "notify you",
                "pause_process": "pause it" if c.scope == "process" else "pause the top consumer",
                "lower_priority": "lower its priority" if c.scope == "process"
                else "lower the top consumer's priority",
            }[a.type])
        return f"When {subject}: {', '.join(actions)}"


def format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} hour" + ("s" if n != 1 else "")
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} min"
    return f"{seconds}s"
