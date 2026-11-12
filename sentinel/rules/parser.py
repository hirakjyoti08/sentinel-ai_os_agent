"""Natural language → :class:`Rule`.

A deterministic parser handles the common phrasings instantly and offline;
anything it can't read is sent to the LLM, whose JSON is validated against
the same Pydantic model. Destructive requests are refused either way.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from sentinel.rules.models import Rule, RuleError

DESTRUCTIVE_WORDS = re.compile(r"\b(kill|terminate|delete|remove|close|force[- ]quit|shut\s*down)\b")
NO_DESTRUCTIVE_MSG = (
    "Rules can't kill, delete or close anything automatically — they can only notify you, "
    "pause a process (undoable with Ctrl+U) or lower its priority. Try e.g. \"…, pause it and "
    "notify me\"."
)

_METRICS = (
    ("cpu", r"\b(cpu|processor)\b"),
    ("memory", r"\b(memory|ram|mem)\b"),
    ("disk", r"\b(disk|storage)\b"),
    ("gpu", r"\b(gpu|graphics|vram)\b"),
)
_THRESHOLD = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)")
_BELOW = re.compile(r"\b(below|under|less than|drops? (?:below|under)|lower than)\b")
_DURATION = re.compile(
    r"\bfor\s+(?:more than\s+|over\s+|at least\s+)?(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_ANY_PROCESS = re.compile(r"\b(any|a|some|each|every)\s+(process|app|application|program)\b")
_NAMED_PROCESS = re.compile(
    r"^(?:if|when|whenever)\s+([a-z0-9][\w.\- ]{0,40}?)\s+(?:uses|is using|takes|goes|exceeds|hits|consumes)\b"
)
_NOT_A_NAME = {"cpu", "memory", "ram", "disk", "gpu", "storage", "system", "the system", "my cpu",
               "my memory", "my ram", "my disk", "the cpu", "usage", "load", "my mac", "my pc",
               "the computer", "my computer"}
_NOTIFY = re.compile(r"\b(notify|alert|tell|let me know|warn|ping|message|remind)\b")
_PAUSE = re.compile(r"\b(pause|suspend|freeze|stop)\b")
_LOWER = re.compile(r"\b(lower (?:its |the )?priority|deprioriti[sz]e|renice|throttle|slow (?:it )?down)\b")


def _strip_prefix(text: str) -> str:
    return re.sub(r"^(?:rule\s*:|add (?:a )?rule\s*:?|create (?:a )?rule\s*:?|new rule\s*:?)\s*", "", text).strip()


def parse_rule_locally(text: str) -> Rule | None:
    """Parse common phrasings without an LLM. ``None`` if unsure."""
    raw = _strip_prefix(text.strip())
    lowered = raw.lower()

    metric = next((m for m, pat in _METRICS if re.search(pat, lowered)), None)
    threshold = _THRESHOLD.search(lowered)
    if metric is None or threshold is None:
        return None

    if " then " in lowered or "," in lowered:
        split_at = max(lowered.rfind(","), lowered.find(" then "))
        action_part = lowered[split_at:]
    else:
        action_part = lowered
    if DESTRUCTIVE_WORDS.search(action_part):
        raise RuleError(NO_DESTRUCTIVE_MSG)

    process_name = None
    scope = "system"
    if _ANY_PROCESS.search(lowered):
        scope = "process"
    elif m := _NAMED_PROCESS.match(lowered):
        candidate = m.group(1).strip()
        if candidate not in _NOT_A_NAME and not re.search(r"\b(cpu|memory|ram|disk|gpu)\b", candidate):
            scope, process_name = "process", candidate

    duration = 0
    if d := _DURATION.search(lowered):
        duration = int(float(d.group(1)) * _UNIT_SECONDS[d.group(2)[0]])

    actions = []
    if _PAUSE.search(action_part):
        actions.append({"type": "pause_process"})
    if _LOWER.search(action_part):
        actions.append({"type": "lower_priority"})
    if _NOTIFY.search(action_part) or not actions:
        actions.append({"type": "notify"})

    try:
        return Rule(
            text=raw,
            condition={
                "scope": scope,
                "metric": metric,
                "op": "<" if _BELOW.search(lowered) else ">",
                "threshold": float(threshold.group(1)),
                "duration_seconds": duration,
                "process_name": process_name,
            },
            actions=actions,
        )
    except ValidationError as e:
        raise RuleError(friendly_validation_error(e)) from None


LLM_PROMPT = """Convert the user's automation request into a JSON rule for a system monitor.

Return ONLY a JSON object with these keys:
  "condition": {{"scope": "system"|"process", "metric": "cpu"|"memory"|"disk"|"gpu",
                "op": ">"|"<", "threshold": <percent number>, "duration_seconds": <int>,
                "process_name": <lower-case substring or null>}},
  "actions": [{{"type": "notify"|"pause_process"|"lower_priority"}}, ...],
  "cooldown_seconds": <int, default 300>

Rules:
- "scope": "process" when the user talks about a process/app (any, or a named one);
  "system" for overall machine usage. Per-process rules only support cpu/memory and ">".
- Durations: convert minutes/hours to seconds; 0 if not given.
- If no action is stated, use notify.
- Only the three action types above exist. If the user asks to kill, delete, close or
  otherwise do something irreversible, return {{"error": "destructive"}} instead.
- If the request isn't a monitoring rule at all, return {{"error": "not_a_rule"}}.

User request: {text}"""


def _extract_json(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def friendly_validation_error(err: ValidationError) -> str:
    first = err.errors()[0]
    msg = first["msg"].removeprefix("Value error, ")
    return f"I couldn't turn that into a valid rule: {msg}."


def parse_rule_with_llm(text: str, llm: Any) -> Rule:
    """Ask the LLM for a structured rule and validate it. Raises :class:`RuleError`."""
    raw = _strip_prefix(text.strip())
    try:
        response = llm.complete([{"role": "user", "content": LLM_PROMPT.format(text=raw)}],
                                use_tools=False)
    except RuntimeError as e:  # AllProvidersFailed
        raise RuleError("I couldn't reach an LLM to read that rule, and the offline parser "
                        "didn't recognise it. Include a resource and a percentage, e.g. "
                        "\"if memory is above 90%, notify me\".") from e
    data = _extract_json(response.final_text)
    if data is None:
        raise RuleError("I couldn't understand that as a rule. Try e.g. "
                        "\"if any process uses over 90% CPU for 5 minutes, pause it and notify me\".")
    if data.get("error") == "destructive":
        raise RuleError(NO_DESTRUCTIVE_MSG)
    if data.get("error"):
        raise RuleError("That doesn't look like a monitoring rule. Rules watch CPU, memory, "
                        "disk or GPU usage and react when a threshold is crossed.")
    data = {k: v for k, v in data.items() if k in ("condition", "actions", "cooldown_seconds")}
    try:
        return Rule(text=raw, **data)
    except (ValidationError, TypeError) as e:
        if isinstance(e, ValidationError):
            raise RuleError(friendly_validation_error(e)) from None
        raise RuleError("I couldn't turn that into a valid rule.") from None


def parse_rule(text: str, llm: Any | None = None) -> Rule:
    """Local parser first, LLM for anything it can't read. Raises :class:`RuleError`."""
    rule = parse_rule_locally(text)
    if rule is not None:
        return rule
    if llm is None:
        raise RuleError("I couldn't read that rule offline. Include a resource (CPU, memory, "
                        "disk, GPU) and a percentage, e.g. \"if memory is above 90%, notify me\".")
    return parse_rule_with_llm(text, llm)
