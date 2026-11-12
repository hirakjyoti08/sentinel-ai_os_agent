"""Root-cause reports for resource alerts.

When the watcher raises an anomaly, :func:`gather_context` collects the
signals that usually explain it — what the culprit is, how long it has been
running, whether it forked a swarm of children or opened ports, what else is
busy, and what was done recently — and :func:`build_report` turns that into a
short incident report: what happened, the likely cause, and what to do.

The LLM writes the report when one is reachable; otherwise a deterministic
local analysis is used, so an alert always comes with an explanation.

Privacy: command lines can carry secrets (tokens, passwords), so they are
redacted before anything leaves the machine. Set ``SENTINEL_INCIDENT_REPORTS``
to ``local`` to never send incident data to an LLM, or ``off`` to disable
reports entirely.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

RECENT_ACTION_WINDOW_S = 15 * 60
_SECRETISH = re.compile(r"^[A-Za-z0-9_\-+/=.]{24,}$")
_KEYVAL = re.compile(r"^(-{0,2}[\w.\-]+)=(.+)$")
_SENSITIVE_FLAGS = re.compile(r"(pass|secret|token|key|auth|cred|cookie)", re.IGNORECASE)


def report_mode() -> str:
    """``llm`` (default), ``local`` or ``off``."""
    mode = os.getenv("SENTINEL_INCIDENT_REPORTS", "llm").strip().lower()
    return mode if mode in ("llm", "local", "off") else "llm"


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


def redact_cmdline(cmdline: list[str] | None, max_args: int = 6) -> str:
    """Executable basename plus a few args, with anything secret-looking masked."""
    if not cmdline:
        return ""
    parts = [os.path.basename(cmdline[0])]
    mask_next = False
    for arg in cmdline[1:max_args + 1]:
        if mask_next:
            parts.append("***")
            mask_next = False
            continue
        if m := _KEYVAL.match(arg):
            flag = m.group(1)
            parts.append(f"{flag}=***" if _SENSITIVE_FLAGS.search(flag) or _SECRETISH.match(m.group(2))
                         else arg)
            continue
        if arg.startswith("-") and _SENSITIVE_FLAGS.search(arg):
            parts.append(arg)
            mask_next = True
            continue
        if _SECRETISH.match(arg):
            parts.append("***")
        elif "/" in arg:
            parts.append(os.path.basename(arg.rstrip("/")) or arg)
        else:
            parts.append(arg[:40])
    if len(cmdline) > max_args + 1:
        parts.append(f"(+{len(cmdline) - max_args - 1} more args)")
    return " ".join(parts)


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def describe_process(pid: int, open_ports: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Facts about one process, safe to show or send (no environment, redacted args)."""
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            age_s = max(0.0, time.time() - p.create_time())
            info: dict[str, Any] = {
                "pid": pid,
                "name": p.name(),
                "command": redact_cmdline(_safe(p.cmdline, [])),
                "running_for": _age(age_s),
                "running_for_seconds": round(age_s),
                "cpu_percent": round(p.cpu_percent(interval=None), 1),
                "memory_mb": round(p.memory_info().rss / 1_048_576, 1),
                "threads": _safe(p.num_threads, None),
                "status": _safe(p.status, None),
            }
            parent = _safe(p.parent, None)
            info["parent"] = parent.name() if parent else None
            info["child_processes"] = len(_safe(lambda: p.children(recursive=True), []))
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    info["listening_ports"] = sorted(
        {c["port"] for c in open_ports or [] if c.get("pid") == pid and c.get("port")}
    )
    return info


def _safe(fn, default):
    try:
        return fn()
    except (psutil.Error, OSError):
        return default


@dataclass
class IncidentContext:
    category: str
    alert: str
    detected_at: float
    system: dict[str, Any]
    culprit: dict[str, Any] | None = None
    other_top_processes: list[dict[str, Any]] = field(default_factory=list)
    recent_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_json(self) -> str:
        data = asdict(self)
        data.pop("detected_at")
        return json.dumps(data, indent=1, default=str)


def gather_context(anomaly: Any, metrics: Any, recent_actions: list[dict[str, Any]] | None = None
                   ) -> IncidentContext:
    """Collect everything relevant to one anomaly. Never raises for a vanished process."""
    now = time.time()
    system = {
        "cpu_percent": metrics.cpu_percent,
        "memory_percent": metrics.memory_percent,
        "memory_used_gb": metrics.memory_used_gb,
        "memory_total_gb": metrics.memory_total_gb,
        "disk_percent": metrics.disk_percent,
        "disk_free_gb": metrics.disk_free_gb,
        "gpu_percent": metrics.gpu_percent,
        "cpu_cores": metrics.cpu_cores,
        "uptime_hours": metrics.uptime_hours,
    }
    culprit = None
    pid = getattr(anomaly, "offending_pid", None)
    if pid:
        culprit = describe_process(pid, metrics.open_ports)
        if culprit and getattr(anomaly, "offending_metric", None):
            culprit["share"] = anomaly.offending_metric

    others = []
    for p in (metrics.top_processes or [])[:6]:
        if p.get("pid") == pid:
            continue
        others.append({
            "pid": p.get("pid"),
            "name": p.get("name"),
            "cpu_percent": round(p.get("cpu_percent") or 0, 1),
            "memory_percent": round(p.get("memory_percent") or 0, 1),
        })

    recent = []
    for a in recent_actions or []:
        try:
            ts = time.mktime(time.strptime(a["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError, TypeError):
            continue
        if now - ts <= RECENT_ACTION_WINDOW_S and a.get("tier") != "read_only":
            recent.append({"action": a.get("action"), "minutes_ago": round((now - ts) / 60, 1),
                           "status": a.get("status")})

    return IncidentContext(
        category=getattr(anomaly, "category", "general"),
        alert=str(anomaly),
        detected_at=now,
        system=system,
        culprit=culprit,
        other_top_processes=others[:5],
        recent_actions=recent[:5],
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


class IncidentReport(BaseModel):
    title: str = Field(max_length=120)
    what_happened: str
    likely_cause: str
    recommendation: str
    source: str = "local"  # "llm" or "local"
    category: str = "general"
    created_at: float = Field(default_factory=time.time)

    def to_markdown(self) -> str:
        stamp = time.strftime("%H:%M", time.localtime(self.created_at))
        origin = "AI analysis" if self.source == "llm" else "local analysis"
        return (
            f"🧾 **Incident report — {self.title}** ({stamp}, {origin})\n"
            f"- **What happened:** {self.what_happened}\n"
            f"- **Likely cause:** {self.likely_cause}\n"
            f"- **Recommendation:** {self.recommendation}"
        )


def rule_friendly_name(name: str) -> str:
    """'Google Chrome Helper (Renderer)' → 'google chrome' (what a rule can match on)."""
    words = re.sub(r"\(.*?\)|[^\w.\- ]", " ", name or "").lower().split()
    words = [w for w in words if w not in {"helper", "renderer", "gpu", "plugin", "app"}]
    return " ".join(words[:2]) or (name or "it").lower()


def _suggested_threshold(culprit: dict[str, Any], category: str) -> int:
    """A round threshold a little below what the culprit is using now."""
    share = str(culprit.get("share") or "")
    m = re.search(r"(\d+(?:\.\d+)?)", share)
    current = float(m.group(1)) if m else (culprit.get("cpu_percent") or 80.0)
    return max(10, int(current * 0.8) // 10 * 10)


def local_report(ctx: IncidentContext) -> IncidentReport:
    """Deterministic root-cause heuristics (no LLM)."""
    c, s = ctx.culprit, ctx.system
    cat = ctx.category
    causes: list[str] = []
    rec: str

    if cat == "disk":
        what = f"Disk usage reached {s['disk_percent']:.0f}% with {s['disk_free_gb']} GB free."
        causes.append("Storage is nearly full; developer caches, downloads and build "
                      "artifacts are the usual culprits.")
        rec = "Type 'free up disk space' to find reclaimable caches before anything else."
    elif c:
        share = c.get("share") or (f"{c['cpu_percent']}% CPU" if cat == "cpu" else f"{c['memory_mb']} MB")
        what = f"{ctx.alert}. Top consumer is {c['name']} (PID {c['pid']}, {share})."
        if c["running_for_seconds"] < 300:
            causes.append(f"{c['name']} started only {c['running_for']} ago, so the spike "
                          "likely comes from something you just launched.")
        elif c["running_for_seconds"] > 86_400 and cat == "memory":
            causes.append(f"{c['name']} has been running for {c['running_for']}; long-lived "
                          "processes that keep growing often indicate a memory leak.")
        if c.get("child_processes", 0) >= 10:
            causes.append(f"It has {c['child_processes']} child processes — a build, test run or "
                          "browser with many tabs/extensions.")
        if c.get("listening_ports"):
            causes.append(f"It is serving on port(s) {', '.join(map(str, c['listening_ports']))}, "
                          "so the load may come from incoming requests.")
        if not causes:
            causes.append(f"{c['name']} is doing sustained work"
                          + (f" (running for {c['running_for']})." if c.get("running_for") else "."))
        metric_word = "cpu" if cat == "cpu" else "memory"
        rec = (f"If you don't need it right now, type 'kill process {c['pid']}'. To handle it "
               f"automatically next time, add a rule such as 'if {rule_friendly_name(c['name'])} uses over "
               f"{_suggested_threshold(c, cat)}% {metric_word} for 2 minutes, pause it and notify me'.")
    else:
        what = f"{ctx.alert}."
        causes.append("No single process stands out; the load is spread across several.")
        rec = "Type 'show top 5' to see what's busy."

    if ctx.recent_actions:
        last = ctx.recent_actions[0]
        causes.append(f"Note: '{last['action']}' was run {last['minutes_ago']:g} min ago.")

    title = {
        "cpu": "High CPU",
        "memory": "Memory pressure",
        "disk": "Disk almost full",
        "gpu": "High GPU",
    }.get(cat, "Resource alert")
    if c and cat != "disk":
        title += f" from {c['name']}"
    return IncidentReport(title=title, what_happened=what, likely_cause=" ".join(causes),
                          recommendation=rec, source="local", category=cat)


LLM_PROMPT = """You are Sentinel, a careful OS assistant. A resource alert just fired.
Using ONLY the facts in the JSON below, write a short incident report.

Return ONLY JSON: {{"title": "<max 8 words>", "what_happened": "<1 sentence>",
"likely_cause": "<1-2 sentences, say 'unclear' if the data doesn't show it>",
"recommendation": "<1 sentence; never claim you already did anything>"}}

For the recommendation, only suggest commands Sentinel supports, quoted exactly:
- 'kill process <pid>' (asks for confirmation first)
- 'show top 5', 'free up disk space', 'list open ports'
- an automation rule, e.g. 'if <process name> uses over <N>% cpu for 2 minutes, pause it and notify me'

Facts:
{facts}

Observations already derived from these facts (use them for "likely_cause" unless the facts
contradict them):
{observations}"""


def build_report(ctx: IncidentContext, llm: Any | None = None) -> IncidentReport:
    """LLM-written report when possible, local analysis otherwise. Never raises."""
    fallback = local_report(ctx)
    if llm is None or report_mode() != "llm":
        return fallback
    try:
        prompt = LLM_PROMPT.format(facts=ctx.to_prompt_json(), observations=fallback.likely_cause)
        response = llm.complete([{"role": "user", "content": prompt}], use_tools=False)
        m = re.search(r"\{.*\}", response.final_text or "", re.DOTALL)
        data = json.loads(m.group()) if m else None
        if not isinstance(data, dict):
            return fallback
        fields = {k: str(data.get(k, "")).strip() or getattr(fallback, k)
                  for k in ("title", "what_happened", "likely_cause", "recommendation")}
        fields["title"] = fields["title"][:120]
        if fields["likely_cause"].strip(" .").lower() in ("unclear", "unknown", "not clear"):
            fields["likely_cause"] = fallback.likely_cause  # our heuristics found something
        if any(w in fields["recommendation"].lower() for w in ("i paused", "i killed", "i have ")):
            fields["recommendation"] = fallback.recommendation  # never claim actions we didn't take
        return IncidentReport(**fields, source="llm", category=ctx.category)
    except (RuntimeError, ValueError, ValidationError) as e:
        logger.info("Incident report falling back to local analysis: %s", e)
        return fallback
