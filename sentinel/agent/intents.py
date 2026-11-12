"""Local intent routing: recognise common requests without calling an LLM.

The table below is ordered: the first matching intent wins, which is how
"kill highest cpu" beats the generic "kill <name>" and "top 5 memory" beats
"top 5". Matchers are pure functions of the request text (plus a little
conversation context), so they're cheap to test exhaustively.

Two kinds of routing use this table:

* **fast path** (``fast_path=True``): destructive requests handled before any
  LLM call, so the confirmation appears instantly;
* **offline fallback**: every intent, used when all LLM providers fail.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoutingContext:
    text: str  # lower-cased, stripped request
    recent_agent_replies: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_input(cls, user_input: str, recent_agent_replies: Sequence[str] = ()) -> RoutingContext:
        return cls(user_input.lower().strip(), tuple(recent_agent_replies))


# --------------------------------------------------------------------------- #
# Keyword tables
# --------------------------------------------------------------------------- #

FAST_GREETINGS = frozenset({"hi", "hii", "hello", "hey", "who are you", "help"})

_GREETING_RE = re.compile(
    r"^(?:h+[i|e|y]+|hello+|howdy|yo+|sup|greetings)(?:\s+there)?(?:[!?. ]|$)"
)
_GREETING_PHRASES = (
    "hi", "hii", "hiii", "hello", "hey", "heyy", "yo", "sup", "good morning",
    "good evening", "good afternoon", "how are you", "greetings", "hi there",
    "hello there", "howdy",
)
_IDENTITY_PHRASES = ("who are you", "what is your name", "what are you called", "what are you")

HEALTH_KEYWORDS = (
    "update on my pc", "pc status", "system status", "computer status", "status of my pc",
    "health check", "system update", "give me an update", "how is my pc", "how's my pc",
    "overview of my pc", "system health", "health", "is my system health okay",
    "is my pc ok", "is my pc okay", "system overview", "how is the system", "pc health",
)
KILL_HIGHEST_CPU_KEYWORDS = ("kill highest cpu", "kill high cpu", "kill cpu hog", "kill top cpu")
KILL_HIGHEST_MEMORY_KEYWORDS = (
    "kill highest memory", "kill high memory", "kill memory hog", "kill top memory",
    "kill highest ram", "kill high ram",
)
CLOSE_PORT_KEYWORDS = (
    "close port", "kill port", "stop port", "free port", "shut port", "terminate port",
)
TOP_CPU_QUESTION_KEYWORDS = (
    "which process", "what process", "top cpu process", "highest cpu process",
    "taking more energy", "using the most energy", "using the most cpu",
    "cpu hog", "most cpu", "process taking the most",
    "highest energy", "using highest energy", "most energy",
)
BATTERY_KEYWORDS = (
    "battery", "charge", "charging", "thermal", "overheating", "temperature", "fan",
    "throttle", "throttling",
)
DISK_HOG_KEYWORDS = (
    "free up disk", "clean disk", "clean cache", "clean developer", "disk hog", "disk hogs",
    "reclaim disk", "reclaim space", "clean bloat", "find large folders",
)
GPU_KEYWORDS = ("gpu", "graphics", "vram", "video card", "graphic card")
BANDWIDTH_KEYWORDS = (
    "bandwidth", "network speed", "download speed", "upload speed", "internet speed",
    "net speed", "throughput", "data transferred", "bytes received", "network bandwidth",
)

_TOP_N_RE = re.compile(r"top\s+(\d+)")
_PID_ONLY_RE = re.compile(
    r"^(?:(?:kill|terminate|stop|close|end)(?:\s+all)?\s+)?pid\s*[:=]?\s*\d+", re.IGNORECASE
)
_KILL_VERB_RE = re.compile(
    r"^(?:kill|terminate|stop\s+process|close\s+process|end\s+process)\s+.+", re.IGNORECASE
)
_NUMBER_RE = re.compile(r"\b(\d+)\b")


def _any(text: str, keywords: Sequence[str]) -> bool:
    return any(kw in text for kw in keywords)


# --------------------------------------------------------------------------- #
# Matchers
# --------------------------------------------------------------------------- #


def is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text)) or any(
        text == g or text.startswith(g + " ") for g in _GREETING_PHRASES
    )


def is_identity_question(text: str) -> bool:
    return _any(text, _IDENTITY_PHRASES)


def is_close_port(text: str) -> bool:
    if _any(text, CLOSE_PORT_KEYWORDS):
        return True
    return "port" in text and any(v in text for v in ("close", "kill", "stop", "free"))


def is_kill_process(ctx: RoutingContext) -> bool:
    """A request to kill a specific process by name or PID."""
    text = ctx.text
    if _PID_ONLY_RE.search(text):
        return True
    # A bare number right after the agent asked which PID to kill.
    if re.fullmatch(r"\d+", text):
        for prev in reversed(ctx.recent_agent_replies):
            prev = prev.lower()
            if any(k in prev for k in ("please specify pid", "pid", "multiple processes match", "kill")):
                return True
    if _KILL_VERB_RE.search(text):
        # "kill highest cpu" etc. have their own intents.
        return not any(k in text for k in ("highest", "high cpu", "high mem", "top cpu", "top mem"))
    return False


def top_n(text: str) -> int | None:
    m = _TOP_N_RE.search(text)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Intent table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Intent:
    name: str
    matches: Callable[[RoutingContext], bool]
    fast_path: bool = False


INTENTS: tuple[Intent, ...] = (
    Intent("greeting", lambda c: is_greeting(c.text) or is_identity_question(c.text)),
    Intent("system_health", lambda c: _any(c.text, HEALTH_KEYWORDS)),
    Intent("kill_highest_cpu", lambda c: _any(c.text, KILL_HIGHEST_CPU_KEYWORDS), fast_path=True),
    Intent("kill_highest_memory", lambda c: _any(c.text, KILL_HIGHEST_MEMORY_KEYWORDS), fast_path=True),
    Intent("close_port", lambda c: is_close_port(c.text), fast_path=True),
    Intent("kill_process", is_kill_process, fast_path=True),
    Intent("top_cpu_question", lambda c: _any(c.text, TOP_CPU_QUESTION_KEYWORDS)),
    Intent(
        "top_n_processes",
        lambda c: top_n(c.text) is not None and "memory" not in c.text and "ram" not in c.text,
    ),
    Intent(
        "cpu_usage",
        lambda c: _any(c.text, ("cpu", "processor"))
        and not _any(c.text, ("process", "top", "high")),
    ),
    Intent("battery_thermal", lambda c: _any(c.text, BATTERY_KEYWORDS)),
    Intent("memory", lambda c: _any(c.text, ("memory", "ram", "mem"))),
    # Before disk_hogs: its reply tells the user to type "clean developer caches all",
    # which also contains the disk_hogs keyword "clean developer".
    Intent(
        "clean_caches",
        lambda c: c.text.startswith("clean developer caches") or c.text.startswith("purge caches"),
    ),
    Intent("disk_hogs", lambda c: _any(c.text, DISK_HOG_KEYWORDS)),
    Intent("disk_usage", lambda c: _any(c.text, ("disk", "storage", "space"))),
    Intent("gpu", lambda c: _any(c.text, GPU_KEYWORDS)),
    Intent(
        "list_processes",
        lambda c: _any(c.text, ("process", "running")) and _any(c.text, ("list", "show")),
    ),
    Intent("network_bandwidth", lambda c: _any(c.text, BANDWIDTH_KEYWORDS)),
    Intent("inspect_process", lambda c: "inspect" in c.text and bool(_NUMBER_RE.search(c.text))),
    Intent("open_ports", lambda c: _any(c.text, ("port", "network", "connection"))),
)

INTENT_NAMES = tuple(i.name for i in INTENTS)


def matching(ctx: RoutingContext, *, fast_path_only: bool = False) -> Iterator[str]:
    """Names of all matching intents, in priority order.

    Callers try each in turn; a handler that returns ``None`` (e.g. a tool
    failed) lets the next intent have a go.
    """
    for intent in INTENTS:
        if fast_path_only and not intent.fast_path:
            continue
        if intent.matches(ctx):
            yield intent.name


def route(ctx: RoutingContext, *, fast_path_only: bool = False) -> str | None:
    """Name of the first matching intent, or ``None`` if the LLM should handle it."""
    return next(matching(ctx, fast_path_only=fast_path_only), None)
