"""LLM providers behind one interface, plus an ordered fallback chain.

Each provider turns its SDK's response (streamed or not) into the same
:class:`LLMResponse`, so the agent loop never has to know which model answered.

    chain = build_default_chain()
    reply = chain.complete(messages, use_tools=True, on_token=print)
    for call in reply.tool_calls: ...
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], None]
Message = dict[str, Any]


# --------------------------------------------------------------------------- #
# Normalized response types
# --------------------------------------------------------------------------- #


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = "{}"


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None
    provider: str = ""
    streamed: bool = False  # True if text was already delivered via on_token

    @property
    def final_text(self) -> str:
        """Text to show the user; falls back to the reasoning trace if empty."""
        return self.text or self.reasoning or ""


class AllProvidersFailed(RuntimeError):
    """Raised when every provider in the chain is unconfigured or errors."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("All LLMs failed. " + " | ".join(self.errors))


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Best-effort parse of a tool call's arguments into a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if raw in (None, "", "null"):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_real_key(value: str | None) -> bool:
    """False for empty values and the placeholders shipped in .env.example."""
    if not value:
        return False
    v = value.strip()
    return bool(v) and not v.upper().startswith("PASTE_") and not v.lower().startswith("your_")


# --------------------------------------------------------------------------- #
# Message format helpers
# --------------------------------------------------------------------------- #


def to_openai_messages(messages: Iterable[Any]) -> list[Message]:
    """Coerce a history into OpenAI chat messages (bare strings become user turns)."""
    out: list[Message] = []
    for msg in messages:
        if isinstance(msg, str):
            out.append({"role": "user", "content": msg})
        elif isinstance(msg, dict):
            out.append(msg)
    return out


_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "model": "Model",
}


def to_transcript(messages: Iterable[Any]) -> str:
    """Flatten a chat history into a single transcript prompt (used for Gemini)."""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            lines.append(str(msg))
            continue
        role = msg.get("role", "user")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                lines.append(
                    f"Assistant requested tool {fn.get('name', 'unknown')}"
                    f"({fn.get('arguments', '{}')})"
                )
            if content is None:
                continue
        if content is None:
            continue
        lines.append(f"{_ROLE_LABELS.get(role, role.title())}: {content}")
    return "\n\n".join(lines)


def assemble_openai_stream(chunks: Iterable[Any], on_token: TokenCallback) -> LLMResponse:
    """Collect an OpenAI-style streamed completion into one response.

    Text is forwarded to ``on_token`` as it arrives, until the model starts
    emitting a tool call (tool-call turns are not shown to the user).
    """
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, str]] = {}

    for chunk in chunks:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None):
            reasoning.append(delta.reasoning_content)
        for tc in getattr(delta, "tool_calls", None) or []:
            idx = getattr(tc, "index", 0) or 0
            slot = calls.setdefault(idx, {"id": f"call_{idx}", "name": "", "arguments": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                slot["name"] += getattr(fn, "name", None) or ""
                slot["arguments"] += getattr(fn, "arguments", None) or ""
        text = getattr(delta, "content", None)
        if text:
            content.append(text)
            if not calls:
                on_token(text)

    return LLMResponse(
        text="".join(content),
        tool_calls=[
            ToolCall(
                id=c["id"],
                name=c["name"],
                arguments=parse_tool_arguments(c["arguments"]),
                raw_arguments=c["arguments"] or "{}",
            )
            for _, c in sorted(calls.items())
        ],
        reasoning="".join(reasoning) or None,
        streamed=True,
    )


def normalize_openai_message(message: Any) -> LLMResponse:
    """Convert a non-streamed OpenAI-style ``choices[0].message``."""
    tool_calls = []
    for i, tc in enumerate(getattr(message, "tool_calls", None) or []):
        raw = getattr(tc.function, "arguments", None) or "{}"
        tool_calls.append(
            ToolCall(
                id=getattr(tc, "id", None) or f"call_{i}",
                name=tc.function.name,
                arguments=parse_tool_arguments(raw),
                raw_arguments=raw,
            )
        )
    return LLMResponse(
        text=getattr(message, "content", None) or "",
        tool_calls=tool_calls,
        reasoning=getattr(message, "reasoning_content", None),
    )


def normalize_gemini_response(response: Any) -> LLMResponse:
    """Convert a google-genai ``GenerateContentResponse``."""
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    candidates = getattr(response, "candidates", None) or []
    parts = getattr(getattr(candidates[0], "content", None), "parts", None) if candidates else None
    for part in parts or []:
        fc = getattr(part, "function_call", None)
        if fc:
            args = parse_tool_arguments(getattr(fc, "args", None))
            tool_calls.append(
                ToolCall(
                    id=f"call_{len(tool_calls)}",
                    name=fc.name,
                    arguments=args,
                    raw_arguments=json.dumps(args),
                )
            )
        elif getattr(part, "text", None):
            texts.append(part.text)
    return LLMResponse(text="".join(texts), tool_calls=tool_calls)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class LLMProvider(ABC):
    """One model endpoint. Subclasses implement :meth:`complete`."""

    key: str = ""
    display_name: str = ""
    setup_hint: str = ""
    runtime_hint: str = ""

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @property
    def available(self) -> bool:
        return self.configured

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "available": self.available,
            "hint": self.runtime_hint if self.configured else self.setup_hint,
        }

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Any],
        *,
        use_tools: bool = True,
        on_token: TokenCallback | None = None,
    ) -> LLMResponse: ...


class OpenAICompatibleProvider(LLMProvider):
    """Any endpoint exposing ``client.chat.completions.create`` (NVIDIA NIM, Groq, ...)."""

    def __init__(
        self,
        *,
        key: str,
        display_name: str,
        client: Any | None,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        stream_timeout: float | None = None,
        extra_params: dict[str, Any] | None = None,
        setup_hint: str = "",
        runtime_hint: str = "",
    ):
        self.key = key
        self.display_name = display_name
        self.client = client
        self.model = model
        self.tools = tools or []
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self.extra_params = extra_params or {}
        self.setup_hint = setup_hint
        self.runtime_hint = runtime_hint

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _params(self, messages: Sequence[Any], use_tools: bool, stream: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            **self.extra_params,
        }
        if use_tools and self.tools:
            params["tools"] = self.tools
            params["tool_choice"] = "auto"
        timeout = self.stream_timeout if stream else self.timeout
        if timeout is not None:
            params["timeout"] = timeout
        if stream:
            params["stream"] = True
        return params

    def complete(self, messages, *, use_tools=True, on_token=None) -> LLMResponse:
        create = self.client.chat.completions.create
        if on_token:
            return assemble_openai_stream(create(**self._params(messages, use_tools, True)), on_token)
        response = create(**self._params(messages, use_tools, False))
        return normalize_openai_message(response.choices[0].message)


class NemotronProvider(OpenAICompatibleProvider):
    """NVIDIA Nemotron via the NIM API. Checks reachability once at startup."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if not self.configured:
            return False
        if self._available is None:
            try:
                self.client.models.list()
                self._available = True
                logger.info("NVIDIA Nemotron connected: %s", self.model)
            except Exception as e:  # network, auth, ...
                self._available = False
                logger.warning("NVIDIA Nemotron not available: %s", e)
        return self._available


# The Gemini API returns 400 INVALID_ARGUMENT for any deadline below this.
GEMINI_MIN_DEADLINE_S = 10.0


class GeminiProvider(LLMProvider):
    """Google Gemini via the ``google-genai`` SDK."""

    key = "gemini"
    display_name = "Gemini"
    setup_hint = "Set GEMINI_API_KEY in .env"
    runtime_hint = "Check key validity and Gemini quota/rate limits."

    def __init__(
        self,
        client: Any | None,
        model: str,
        function_declarations: list[dict[str, Any]] | None = None,
        timeout: float = 12.0,
        stream_timeout: float = 15.0,
    ):
        if min(timeout, stream_timeout) < GEMINI_MIN_DEADLINE_S:
            raise ValueError(f"Gemini rejects deadlines under {GEMINI_MIN_DEADLINE_S:.0f}s")
        self.client = client
        self.model = model
        self.function_declarations = function_declarations or []
        self.timeout = timeout
        self.stream_timeout = stream_timeout

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _config(self, use_tools: bool, timeout_s: float) -> Any:
        from google.genai import types as gt

        cfg: dict[str, Any] = {"http_options": gt.HttpOptions(timeout=int(timeout_s * 1000))}
        if use_tools and self.function_declarations:
            cfg["tools"] = [gt.Tool(function_declarations=self.function_declarations)]
            cfg["tool_config"] = gt.ToolConfig(
                function_calling_config=gt.FunctionCallingConfig(mode="AUTO")
            )
            # Sentinel runs tools itself (permissions + audit), never the SDK.
            cfg["automatic_function_calling"] = gt.AutomaticFunctionCallingConfig(disable=True)
        return gt.GenerateContentConfig(**cfg)

    def complete(self, messages, *, use_tools=True, on_token=None) -> LLMResponse:
        prompt = to_transcript(messages)
        tools_on = use_tools and bool(self.function_declarations)
        if on_token and not tools_on:
            parts: list[str] = []
            for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=prompt, config=self._config(False, self.stream_timeout)
            ):
                text = getattr(chunk, "text", None)
                if text:
                    parts.append(text)
                    on_token(text)
            return LLMResponse(text="".join(parts), streamed=True)
        response = self.client.models.generate_content(
            model=self.model, contents=prompt, config=self._config(tools_on, self.timeout)
        )
        return normalize_gemini_response(response)


# --------------------------------------------------------------------------- #
# Fallback chain
# --------------------------------------------------------------------------- #


class FallbackChain:
    """Try providers in order; the first one that answers wins."""

    def __init__(self, providers: Sequence[LLMProvider]):
        self.providers = list(providers)

    def complete(
        self,
        messages: Sequence[Any],
        *,
        use_tools: bool = True,
        on_token: TokenCallback | None = None,
    ) -> LLMResponse:
        errors: list[str] = []
        for provider in self.providers:
            if not provider.configured:
                errors.append(f"{provider.display_name} not configured")
                continue
            if not provider.available:
                errors.append(f"{provider.display_name} unavailable")
                continue
            try:
                response = provider.complete(messages, use_tools=use_tools, on_token=on_token)
            except Exception as e:
                errors.append(f"{provider.display_name} runtime error: {e}")
                logger.warning("%s error: %s; trying next provider", provider.display_name, e)
                continue
            response.provider = provider.key
            logger.debug("%s call successful", provider.display_name)
            return response
        raise AllProvidersFailed(errors)

    def status(self) -> dict[str, dict[str, Any]]:
        """Per-provider ``{configured, available, hint}`` for the UI."""
        return {p.key: p.status() for p in self.providers}


def build_default_chain() -> FallbackChain:
    """Nemotron → Groq → Gemini, configured from environment variables."""
    from sentinel.tools.registry import get_gemini_tool_schemas, get_tool_schemas

    openai_tools = get_tool_schemas()

    nvidia_key = os.getenv("NVIDIA_API_KEY")
    nvidia_client = None
    if is_real_key(nvidia_key):
        from openai import OpenAI

        nvidia_client = OpenAI(
            base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            api_key=nvidia_key,
            timeout=30.0,
            max_retries=1,
        )
    nemotron = NemotronProvider(
        key="nemotron",
        display_name="Nemotron",
        client=nvidia_client,
        model=os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"),
        tools=openai_tools,
        extra_params={"temperature": 0.2},
        setup_hint="Set NVIDIA_API_KEY in .env. Get one from https://build.nvidia.com",
        runtime_hint="Set NVIDIA_API_KEY in .env. Get one from https://build.nvidia.com",
    )

    groq_key = os.getenv("GROQ_API_KEY")
    groq_client = None
    if is_real_key(groq_key):
        from groq import Groq

        groq_client = Groq(api_key=groq_key)
    groq = OpenAICompatibleProvider(
        key="groq",
        display_name="Groq",
        client=groq_client,
        model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
        tools=openai_tools,
        timeout=8.0,
        stream_timeout=12.0,
        setup_hint="Set GROQ_API_KEY in .env",
        runtime_hint="Check key validity and Groq quota/rate limits.",
    )

    gemini_key = os.getenv("GEMINI_API_KEY")
    gemini_client = None
    if is_real_key(gemini_key):
        from google import genai

        gemini_client = genai.Client(api_key=gemini_key)
    gemini = GeminiProvider(
        client=gemini_client,
        model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        function_declarations=get_gemini_tool_schemas(),
    )

    chain = FallbackChain([nemotron, groq, gemini])
    if nemotron.available:
        logger.info("NVIDIA Nemotron initialized as primary LLM")
    else:
        logger.info("NVIDIA Nemotron not available, will use cloud fallbacks")
    return chain
