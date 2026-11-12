"""LLM provider layer: normalization, streaming assembly and fallback order."""

from types import SimpleNamespace as NS

import pytest

from sentinel.llm.providers import (
    GEMINI_MIN_DEADLINE_S,
    AllProvidersFailed,
    FallbackChain,
    GeminiProvider,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    assemble_openai_stream,
    is_real_key,
    normalize_gemini_response,
    normalize_openai_message,
    parse_tool_arguments,
    to_transcript,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeProvider(LLMProvider):
    def __init__(self, key, *, configured=True, available=True, reply=None, error=None):
        self.key = self.display_name = key
        self._configured, self._available = configured, available
        self.reply, self.error = reply, error
        self.calls = 0

    @property
    def configured(self):
        return self._configured

    @property
    def available(self):
        return self._available

    def complete(self, messages, *, use_tools=True, on_token=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply or LLMResponse(text=f"from {self.key}")


def chunk(content=None, tool_calls=None, reasoning=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls, reasoning_content=reasoning))])


def tc_delta(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


# --------------------------------------------------------------------------- #
# Fallback chain
# --------------------------------------------------------------------------- #


def test_first_healthy_provider_answers():
    a, b = FakeProvider("a"), FakeProvider("b")
    reply = FallbackChain([a, b]).complete([])
    assert reply.text == "from a" and reply.provider == "a"
    assert b.calls == 0


def test_skips_unconfigured_unavailable_and_failing_providers():
    down = FakeProvider("down", available=False)
    missing = FakeProvider("missing", configured=False)
    broken = FakeProvider("broken", error=TimeoutError("timed out"))
    ok = FakeProvider("ok")
    reply = FallbackChain([missing, down, broken, ok]).complete([])
    assert reply.provider == "ok"
    assert (down.calls, missing.calls, broken.calls) == (0, 0, 1)


def test_all_failing_raises_with_every_reason():
    chain = FallbackChain([
        FakeProvider("a", configured=False),
        FakeProvider("b", error=RuntimeError("429 rate limited")),
    ])
    with pytest.raises(AllProvidersFailed) as exc:
        chain.complete([])
    assert exc.value.errors == ["a not configured", "b runtime error: 429 rate limited"]
    assert isinstance(exc.value, RuntimeError)  # callers catching RuntimeError still work


def test_status_reports_each_provider():
    chain = FallbackChain([FakeProvider("a"), FakeProvider("b", configured=False)])
    status = chain.status()
    assert status["a"]["configured"] and status["a"]["available"]
    assert not status["b"]["configured"]


@pytest.mark.parametrize(
    ("value", "real"),
    [
        ("sk-abc123", True),
        ("", False),
        (None, False),
        ("PASTE_YOUR_NVIDIA_KEY_HERE", False),
        ("your_gemini_api_key_here", False),  # the .env.example placeholder
    ],
)
def test_placeholder_keys_are_not_configured(value, real):
    assert is_real_key(value) is real


# --------------------------------------------------------------------------- #
# OpenAI-compatible normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [('{"pid": 42}', {"pid": 42}), ("", {}), ("null", {}), (None, {}), ("not json", {}),
     ("[1, 2]", {}), ({"n": 3}, {"n": 3})],
)
def test_parse_tool_arguments(raw, parsed):
    assert parse_tool_arguments(raw) == parsed


def test_stream_assembles_text_and_forwards_tokens():
    tokens = []
    reply = assemble_openai_stream([chunk("Hel"), chunk("lo"), NS(choices=[])], tokens.append)
    assert reply.text == "Hello" and tokens == ["Hel", "lo"]
    assert reply.streamed and not reply.tool_calls


def test_stream_assembles_tool_calls_split_across_chunks():
    tokens = []
    chunks = [
        chunk(tool_calls=[tc_delta(0, id="call_a", name="get_cpu", arguments='{"x"')]),
        chunk(tool_calls=[tc_delta(0, name="_usage", arguments=': 1}')]),
        chunk(tool_calls=[tc_delta(1, id="call_b", name="list_processes", arguments="")]),
        chunk("thinking out loud"),  # text after a tool call starts is not shown
    ]
    reply = assemble_openai_stream(chunks, tokens.append)
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("call_a", "get_cpu_usage", {"x": 1}),
        ("call_b", "list_processes", {}),
    ]
    assert tokens == []


def test_stream_keeps_reasoning_separately():
    reply = assemble_openai_stream([chunk(reasoning="hmm "), chunk(reasoning="ok")], lambda t: None)
    assert reply.text == "" and reply.final_text == "hmm ok"


def test_normalize_non_streamed_message():
    msg = NS(
        content=None,
        reasoning_content=None,
        tool_calls=[NS(id="c1", function=NS(name="kill_process", arguments='{"pid": 7}'))],
    )
    reply = normalize_openai_message(msg)
    assert reply.tool_calls[0].name == "kill_process"
    assert reply.tool_calls[0].arguments == {"pid": 7}
    assert reply.tool_calls[0].raw_arguments == '{"pid": 7}'


def test_openai_provider_sends_tools_only_when_enabled():
    sent = []

    def create(**params):
        sent.append(params)
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None, reasoning_content=None))])

    client = NS(chat=NS(completions=NS(create=create)))
    p = OpenAICompatibleProvider(key="x", display_name="X", client=client, model="m",
                                 tools=[{"type": "function"}], timeout=8.0)
    p.complete([{"role": "user", "content": "hi"}])
    p.complete([{"role": "user", "content": "hi"}], use_tools=False)
    assert sent[0]["tools"] and sent[0]["tool_choice"] == "auto" and sent[0]["timeout"] == 8.0
    assert "tools" not in sent[1]


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #


def test_normalize_gemini_text_and_function_calls():
    from google.genai import types as gt

    response = gt.GenerateContentResponse(candidates=[gt.Candidate(content=gt.Content(
        role="model",
        parts=[
            gt.Part(text="Checking. "),
            gt.Part(function_call=gt.FunctionCall(name="get_cpu_usage", args={})),
            gt.Part(function_call=gt.FunctionCall(name="get_disk_usage", args={"path": "/"})),
        ],
    ))])
    reply = normalize_gemini_response(response)
    assert reply.text == "Checking. "
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("call_0", "get_cpu_usage", {}),
        ("call_1", "get_disk_usage", {"path": "/"}),
    ]


def test_gemini_disables_sdk_auto_function_calling():
    seen = {}

    def generate_content(model, contents, config):
        seen["config"] = config
        from google.genai import types as gt
        return gt.GenerateContentResponse(candidates=[])

    client = NS(models=NS(generate_content=generate_content))
    GeminiProvider(client, "gemini-x", [{"name": "get_cpu_usage", "description": "",
                                          "parameters": {"type": "object", "properties": {}}}]
                   ).complete([{"role": "user", "content": "cpu?"}])
    assert seen["config"].automatic_function_calling.disable is True
    assert seen["config"].tools


def test_gemini_deadlines_meet_api_minimum():
    # The API rejects deadlines under 10s with 400 INVALID_ARGUMENT.
    seen = []

    def generate_content(model, contents, config):
        seen.append(config.http_options.timeout)
        from google.genai import types as gt
        return gt.GenerateContentResponse(candidates=[])

    GeminiProvider(NS(models=NS(generate_content=generate_content)), "m").complete([])
    assert seen[0] >= GEMINI_MIN_DEADLINE_S * 1000
    with pytest.raises(ValueError):
        GeminiProvider(None, "m", timeout=8.0)


def test_transcript_renders_tool_turns():
    text = to_transcript([
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "get_cpu_usage", "arguments": "{}"}}]},
        {"role": "tool", "content": '{"cpu_percent": 5}'},
    ])
    assert text.splitlines()[0] == "System: be brief"
    assert "Assistant requested tool get_cpu_usage({})" in text
    assert 'Tool: {"cpu_percent": 5}' in text
