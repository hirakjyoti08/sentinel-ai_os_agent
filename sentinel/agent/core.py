"""The agent: routes requests, runs the LLM tool loop, and enforces safety.

Request flow for :meth:`Agent.chat`:

1. **Fast path** — greetings and destructive intents ("kill spotify",
   "close port 3000") are recognised locally (:mod:`sentinel.agent.intents`)
   and answered without an LLM round trip.
2. **LLM tool loop** — otherwise the request goes to the provider chain
   (:mod:`sentinel.llm.providers`). Tool calls are executed through
   :meth:`Agent._execute_tool_with_permissions` and fed back until the model
   answers in text. LLM-requested kills are turned into UI confirmations.
3. **Offline fallback** — if every provider fails, the same intent table
   answers common read-only questions locally.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from sentinel.agent import intents
from sentinel.agent.confirmations import confirmation_for_tool_call
from sentinel.agent.local_handlers import GUARDED_HANDLERS, HANDLERS, greeting_reply
from sentinel.agent.local_handlers import render_ascii_bar as _render_ascii_bar  # noqa: F401
from sentinel.agent.memory import AgentMemory
from sentinel.agent.prompts import (
    PLANNING_PROMPT,
    REFLECTION_PROMPT,
    SYSTEM_PROMPT,
    get_system_prompt,
)
from sentinel.agent.undo import undo_action
from sentinel.llm.providers import (
    AllProvidersFailed,
    FallbackChain,
    LLMResponse,
    build_default_chain,
)
from sentinel.monitor import incidents
from sentinel.rules import Firing, Rule, RuleEngine, RuleError, RuleStore, parse_rule
from sentinel.rules.models import LOWER_PRIORITY_NICE, format_duration
from sentinel.safety.circuit_breaker import CircuitBreaker
from sentinel.safety.permissions import confirm_action
from sentinel.storage.audit_log import (
    get_last_action,
    get_recent_actions,
    log_action,
    mark_undone,
    update_result,
)
from sentinel.tools.registry import execute_tool, get_tool_tier

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], None]

MAX_TOOL_ROUNDS = 5
HISTORY_TURNS = 4
TRIM_LIMIT = 20
EMPTY_REPLY = "I couldn't come up with an answer for that. Could you rephrase it?"

# Tools whose undo data can be reconstructed from their parameters alone.
_UNDO_FROM_PARAMS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "pause_process": lambda p: {"pid": p.get("pid"), "action": "pause_process"},
    "resume_process": lambda p: {"pid": p.get("pid"), "action": "resume_process"},
    "set_priority": lambda p: {"pid": p.get("pid")},
    "stop_service": lambda p: {"service": p.get("name")},
}


YES_WORDS = frozenset({"yes", "y", "yep", "yeah", "save", "save it", "confirm", "ok", "okay"})
NO_WORDS = frozenset({"no", "n", "nope", "cancel", "discard", "don't", "dont"})


@dataclass
class RuleRun:
    """One rule firing and what Sentinel did about it."""

    firing: Firing
    outcomes: list[str] = field(default_factory=list)

    @property
    def notify(self) -> bool:
        return any(a.type == "notify" for a in self.firing.rule.actions)

    @property
    def message(self) -> str:
        done = "; ".join(self.outcomes) if self.outcomes else "notified you"
        return f"⚙️ Rule {self.firing.rule.id} fired — {self.firing.summary}. Action: {done}."


def emit_words(text: str, on_token: TokenCallback | None, delay: float = 0.003) -> None:
    """Stream an already-complete reply word by word (for non-streamed sources)."""
    if not on_token:
        return
    words = text.split(" ")
    for i, word in enumerate(words):
        on_token(word + (" " if i < len(words) - 1 else ""))
        if delay:
            time.sleep(delay)


class Agent:
    """Natural-language OS assistant with tiered, audited tool execution."""

    def __init__(self, llm: FallbackChain | None = None, rules_path: str | Path | None = None):
        load_dotenv()
        self.memory = AgentMemory()
        self.circuit_breaker = CircuitBreaker()
        self.llm = llm if llm is not None else build_default_chain()
        self.rules = RuleStore(rules_path)
        self.rule_engine = RuleEngine(self.rules)
        self.pending_rule: Rule | None = None
        self.incidents: deque[incidents.IncidentReport] = deque(maxlen=20)

    # ------------------------------------------------------------------ #
    # LLM access
    # ------------------------------------------------------------------ #

    @property
    def llm_provider_status(self) -> dict[str, dict[str, Any]]:
        """``{provider: {configured, available, hint}}`` for the TUI status bar."""
        return self.llm.status()

    def _call_llm(
        self,
        messages: list[dict[str, Any]],
        *,
        use_tools: bool = True,
        on_token: TokenCallback | None = None,
    ) -> LLMResponse:
        return self.llm.complete(messages, use_tools=use_tools, on_token=on_token)

    def _build_llm_unavailable_message(self, raw_error: str) -> str:
        """Actionable guidance when every provider failed."""
        parts = []
        for name, state in self.llm_provider_status.items():
            if state.get("configured") and state.get("available"):
                parts.append(f"{name}: configured")
            elif state.get("configured"):
                parts.append(f"{name}: configured but unavailable")
            else:
                parts.append(f"{name}: not configured")
        return (
            "Error: All LLM providers are currently unavailable. "
            f"Status: {', '.join(parts)}. "
            "Fix: set NVIDIA_API_KEY in .env (from https://build.nvidia.com), or set "
            "GROQ_API_KEY and/or GEMINI_API_KEY, then restart the app. "
            f"Details: {raw_error}"
        )

    # ------------------------------------------------------------------ #
    # Local routing
    # ------------------------------------------------------------------ #

    def _routing_context(self, user_input: str) -> intents.RoutingContext:
        # The current turn is already in memory (with an empty reply); skip it.
        previous = self.memory.get_recent_context(n=3)[:-1]
        return intents.RoutingContext.from_input(
            user_input, [item.get("agent", "") for item in previous]
        )

    def _run_local_intents(self, user_input: str, *, fast_path_only: bool) -> str | None:
        ctx = self._routing_context(user_input)
        for name in intents.matching(ctx, fast_path_only=fast_path_only):
            if name in intents.AGENT_INTENTS:
                reply = self._handle_agent_intent(name, ctx.text, user_input)
            elif name in GUARDED_HANDLERS:
                reply = GUARDED_HANDLERS[name](ctx.text, self._execute_tool_with_permissions)
            else:
                reply = HANDLERS[name](ctx.text)
            if reply:
                logger.info("Handled locally as intent %r", name)
                return reply
        return None

    def _try_local_fallback(self, user_input: str) -> str | None:
        """Answer common requests without an LLM (used when providers are down)."""
        return self._run_local_intents(user_input, fast_path_only=False)

    # ------------------------------------------------------------------ #
    # Automation rules
    # ------------------------------------------------------------------ #

    def _handle_agent_intent(self, name: str, text: str, user_input: str) -> str | None:
        if name == "add_rule":
            return self._draft_rule(user_input)
        if name == "list_rules":
            return self._list_rules()
        if name == "delete_rule":
            ref = intents.DELETE_RULE_RE.match(text).group(1)
            rule = self.rules.get(ref)
            if not rule or not self.rules.remove(rule.id):
                return f"No rule '{ref}'. Type 'list rules' to see them."
            self.rule_engine.reset(rule.id)
            return f"🗑️ Deleted rule {rule.id}: {rule.describe()}."
        if name == "toggle_rule":
            verb, ref = intents.TOGGLE_RULE_RE.match(text).groups()
            enable = verb in ("enable", "resume", "turn on")
            rule = self.rules.get(ref)
            if not rule:
                return f"No rule '{ref}'. Type 'list rules' to see them."
            self.rules.set_enabled(rule.id, enable)
            self.rule_engine.reset(rule.id)
            return f"{'▶️ Enabled' if enable else '⏸️ Disabled'} rule {rule.id}: {rule.describe()}."
        if name == "incident_report":
            if not self.incidents:
                return "No alerts have fired since Sentinel started, so there's no incident report yet."
            return self.incidents[-1].to_markdown()
        return None

    def _draft_rule(self, user_input: str) -> str:
        """Parse a rule and ask for confirmation; it's saved only after the user says yes."""
        try:
            rule = parse_rule(user_input, self.llm)
        except RuleError as e:
            return f"⚠️ {e}"
        self.pending_rule = rule
        acts = {a.type for a in rule.actions}
        lines = [
            "Here's the rule I understood:",
            f"**{rule.describe()}.**",
            f"It will trigger at most once every {format_duration(rule.cooldown_seconds)} per "
            f"{'process' if rule.condition.scope == 'process' else 'rule'}.",
        ]
        if acts & {"pause_process", "lower_priority"}:
            lines.append("Saving it lets Sentinel take that action on its own. Each one is logged in "
                         "the audit trail, and protected system processes and Sentinel itself are "
                         "never touched.")
        if "pause_process" in acts:
            lines.append("A pause can be undone with Ctrl+U.")
        if "lower_priority" in acts:
            lines.append("Note: a lowered priority can't be raised back without admin rights; it "
                         "resets when the process restarts.")
        lines.append("Type **yes** to save it, or anything else to cancel.")
        return "\n".join(lines)

    def _list_rules(self) -> str:
        rules = self.rules.all()
        if not rules:
            return ("You have no automation rules yet. Try: 'if any process uses over 90% CPU "
                    "for 5 minutes, pause it and notify me'.")
        lines = ["| # | ID | Rule | Status |", "|:---:|:---:|---|:---:|"]
        for i, r in enumerate(rules, 1):
            lines.append(f"| {i} | {r.id} | {r.describe()} | {'active' if r.enabled else 'disabled'} |")
        lines.append("\nManage with 'disable rule <id>', 'enable rule <id>' or 'delete rule <id>'.")
        return "\n".join(lines)

    def run_rules(self, metrics: Any) -> list[RuleRun]:
        """Evaluate rules against a telemetry snapshot and carry out their actions.

        Called from the watcher thread on every tick. Actions go through the
        normal permission pipeline with ``confirmed=True`` — the user approved
        them when saving the rule — so they are tier-checked, audited and
        undoable, and protected processes are still refused by the tools.
        """
        runs = []
        for firing in self.rule_engine.evaluate(metrics):
            run = RuleRun(firing)
            for action in firing.rule.actions:
                if action.type == "notify":
                    continue
                if not firing.target_pid:
                    run.outcomes.append(f"no process to {action.type.replace('_', ' ')}")
                    continue
                tool, params, verb = {
                    "pause_process": ("pause_process", {"pid": firing.target_pid}, "paused"),
                    "lower_priority": ("set_priority",
                                       {"pid": firing.target_pid, "nice": LOWER_PRIORITY_NICE},
                                       "lowered priority of"),
                }[action.type]
                result = self._execute_tool_with_permissions(tool, params, confirmed=True)
                target = f"{firing.target_name} (PID {firing.target_pid})"
                run.outcomes.append(f"{verb} {target}" if result.get("success")
                                    else f"couldn't act on {target}: {result.get('error')}")
            logger.info(run.message)
            runs.append(run)
        return runs

    # ------------------------------------------------------------------ #
    # Incident reports
    # ------------------------------------------------------------------ #

    def create_incident_report(self, anomaly: Any, metrics: Any) -> incidents.IncidentReport | None:
        """Explain an alert. Blocking (may call the LLM) — run it off the UI thread."""
        mode = incidents.report_mode()
        if mode == "off":
            return None
        try:
            recent = get_recent_actions(limit=10)
        except Exception:
            recent = []
        ctx = incidents.gather_context(anomaly, metrics, recent)
        report = incidents.build_report(ctx, self.llm if mode == "llm" else None)
        self.incidents.append(report)
        return report

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #

    def _finish(self, user_input: str, reply: str, on_token: TokenCallback | None,
                tool_calls: list | None = None, *, already_streamed: bool = False) -> str:
        if not already_streamed:
            emit_words(reply, on_token)
        self.memory.add_interaction(user_input, reply, tool_calls or [])
        return reply

    def _build_messages(self, user_input: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": get_system_prompt()}]
        for turn in self.memory.get_recent_context(n=HISTORY_TURNS)[:-1]:
            if turn.get("user"):
                messages.append({"role": "user", "content": turn["user"]})
            if turn.get("agent"):
                messages.append({"role": "assistant", "content": turn["agent"]})
        messages.append({"role": "user", "content": user_input})
        return messages

    def chat(self, user_input: str, on_token: TokenCallback | None = None) -> str:
        """Answer one user message, calling tools as needed, optionally streaming tokens."""
        logger.info("Chat request: %s", user_input[:100])
        self.memory.add_interaction(user_input, "")
        text = user_input.lower().strip()

        # 0. Answer to "save this rule?"
        if self.pending_rule is not None:
            rule, self.pending_rule = self.pending_rule, None
            if text in YES_WORDS:
                self.rules.add(rule)
                return self._finish(user_input, (
                    f"✅ Rule {rule.id} saved and active: {rule.describe()}.\n"
                    "It runs locally every 2 seconds. Type 'list rules' to see all rules, "
                    f"'disable rule {rule.id}' to pause it or 'delete rule {rule.id}' to remove it."
                ), on_token)
            if text in NO_WORDS:
                return self._finish(user_input, "Okay, I discarded that rule.", on_token)
            # Anything else: drop the draft and treat this as a new request.

        # 1. Fast path: rules, greetings and destructive intents.
        if text in intents.FAST_GREETINGS and (greeting := greeting_reply(text)):
            return self._finish(user_input, greeting, on_token)
        if fast := self._run_local_intents(user_input, fast_path_only=True):
            self.memory.add_interaction(user_input, fast, [])
            return fast

        # 2. LLM tool loop.
        messages = self._build_messages(user_input)
        executed: list[dict[str, Any]] = []
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            try:
                response = self._call_llm(messages, on_token=on_token)
            except AllProvidersFailed as e:
                # 3. Offline fallback.
                logger.warning("LLM unavailable, using local fallback: %s", e)
                if local := self._try_local_fallback(user_input):
                    return self._finish(user_input, local, on_token)
                unavailable = self._build_llm_unavailable_message(str(e))
                emit_words(unavailable, on_token)
                return unavailable

            if not response.tool_calls:
                reply = response.final_text or EMPTY_REPLY
                logger.info("Chat response (%s): %s", response.provider, reply[:200])
                return self._finish(user_input, reply, on_token, executed,
                                    already_streamed=response.streamed and bool(response.text))

            messages.append({
                "role": "assistant",
                "content": response.text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": c.raw_arguments}}
                    for c in response.tool_calls
                ],
            })
            for call in response.tool_calls:
                logger.info("Tool call #%d (%s): %s(%s)", round_no, response.provider,
                            call.name, call.arguments)
                if confirmation := confirmation_for_tool_call(call.name, call.arguments):
                    return confirmation
                result = self._execute_tool_with_permissions(call.name, call.arguments)
                executed.append({"tool": call.name, "args": call.arguments, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(self._trim_tool_result_for_llm(call.name, result),
                                          default=str),
                })

        logger.warning("Max tool calls reached")
        return "Max tool calls reached. Please try a more specific request."

    def _trim_tool_result_for_llm(self, tool_name: str, result: Any) -> Any:
        """Cap huge tool outputs (hundreds of processes/ports) before sending to the LLM."""
        if not isinstance(result, dict):
            return result
        key, total_key, label = {
            "list_processes": ("processes", "total_processes", "processes"),
            "list_open_ports": ("connections", "total_ports", "ports"),
        }.get(tool_name, (None, None, None))
        items = result.get(key) if key else None
        if not items or len(items) <= TRIM_LIMIT:
            return result
        trimmed = dict(result)
        trimmed[key] = items[:TRIM_LIMIT]
        trimmed[total_key] = len(items)
        if tool_name == "list_processes":
            trimmed["note"] = (f"Showing top {TRIM_LIMIT} processes by resource usage out of "
                               f"{len(items)} total active processes.")
        else:
            trimmed["note"] = f"Showing first {TRIM_LIMIT} {label} out of {len(items)} open ports."
        return trimmed

    # ------------------------------------------------------------------ #
    # Safety pipeline
    # ------------------------------------------------------------------ #

    def _execute_tool_with_permissions(
        self, tool_name: str, params: dict[str, Any], confirmed: bool = False
    ) -> dict[str, Any]:
        """Run a tool through tier checks, confirmation, the circuit breaker and the audit log.

        Order matters: the circuit breaker and confirmation run *before*
        anything is logged or executed, and the log entry is written *before*
        the tool runs so a crash mid-action still leaves a record.
        """
        tier = get_tool_tier(tool_name)

        if tier == "destructive" and self.circuit_breaker.is_triggered():
            return {"success": False,
                    "error": "Circuit breaker triggered: too many destructive actions. Please wait."}
        if not confirmed and not confirm_action(tier, tool_name, params):
            return {"success": False, "error": "User cancelled or confirmation required"}

        log_id = log_action(tool_name, params, tier, None)
        try:
            result = execute_tool(tool_name, params)
        except Exception as e:
            error = {"success": False, "error": str(e)}
            update_result(log_id, error)
            return error

        undo_data = result.get("undo_data") if isinstance(result, dict) else None
        if not undo_data and tier != "read_only" and tool_name in _UNDO_FROM_PARAMS:
            undo_data = _UNDO_FROM_PARAMS[tool_name](params)
        update_result(log_id, result, undo_data=undo_data)

        if tier == "destructive" and result.get("success"):
            self.circuit_breaker.record_action()
        return result

    def undo_last(self) -> str:
        """Undo the most recent reversible or destructive action in the audit log."""
        last = get_last_action(reversible_only=True)
        if not last:
            return "No reversible or destructive actions to undo."
        succeeded, message = undo_action(last)
        if succeeded:
            mark_undone(last["id"])
        return message

    # ------------------------------------------------------------------ #
    # Multi-step goals (Plan → Act → Observe → Reflect)
    # ------------------------------------------------------------------ #

    def run_goal(self, goal: str, max_steps: int = 10, dry_run: bool = False) -> str:
        """Plan a multi-step goal, then execute it with reflection between steps."""
        self.memory.add_interaction(goal, "")
        plan = self._create_plan(goal)
        if dry_run:
            lines = ["Plan (dry run):"]
            for i, step in enumerate(plan.get("steps", []), 1):
                lines.append(f"  {i}. {step['tool']}({step['params']}) - {step.get('reason', '')}")
            return "\n".join(lines) + "\n"
        return self._execute_plan(goal, plan, max_steps)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _create_plan(self, goal: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": f"{PLANNING_PROMPT}\n\nGoal: {goal}"},
        ]
        response = self._call_llm(messages, use_tools=False)
        return self._extract_json(response.final_text) or {"steps": []}

    def _execute_plan(self, goal: str, plan: dict[str, Any], max_steps: int) -> str:
        steps = plan.get("steps", [])
        executed: list[dict[str, Any]] = []
        for i, step in enumerate(steps[:max_steps]):
            result = self._execute_tool_with_permissions(step.get("tool"), step.get("params", {}))
            executed.append({"step": step, "result": result})
            reflection = self._reflect(goal, executed, i == len(steps) - 1)
            if reflection.get("decision") == "done":
                return reflection.get("summary", "Goal completed.")
            if reflection.get("decision") == "error":
                return f"Error: {reflection.get('summary', 'Unknown error')}"
        return f"Completed {len(executed)} steps. Goal may need more steps."

    def _reflect(self, goal: str, executed: list[dict[str, Any]], is_last_step: bool) -> dict[str, Any]:
        summary = [
            f"{s.get('step', {}).get('tool', 'unknown')}: "
            f"{'ok' if s.get('result', {}).get('success') else 'failed'}"
            for s in executed
        ]
        # Cheap local heuristic first, to avoid an LLM round trip per step.
        local = self._try_local_fallback(f"reflect on goal: {goal}, steps: {summary}, last: {is_last_step}")
        if local and not local.startswith("__CONFIRM_KILL__"):
            if is_last_step or "failed" in str(summary).lower():
                return {"decision": "done", "summary": f"Completed {len(executed)} steps."}
            return {"decision": "continue", "summary": "Continue to next step."}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": (
                f"{REFLECTION_PROMPT}\n\nGoal: {goal}\n\nExecuted: {'; '.join(summary)}"
                f"\n\nIs last planned step: {is_last_step}"
            )},
        ]
        response = self._call_llm(messages, use_tools=False)
        return self._extract_json(response.final_text) or {
            "decision": "done" if is_last_step else "continue",
            "summary": "Completed.",
        }
