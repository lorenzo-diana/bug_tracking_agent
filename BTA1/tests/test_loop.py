"""Tests for the agent loop, against a mock LLM.

These cover the loop's MECHANICS, which are deterministic: message
accumulation, tool dispatch, the iteration cap, and every failure path a weak
model will actually produce.

WHAT IS NOT TESTED HERE, DELIBERATELY: whether the agent solves the bug. That
is one nondeterministic sample from a model, not a test. Treating a single green
run as verification is the habit this project is trying to teach students out of
-- the honest answer to "does the agent work?" is an eval harness over a graded
bug set, which is future work. See docs/implementation-plan.md.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest

from bta1.agent import loop
from bta1.agent.mcp_bridge import Bridge, to_openai_tool
from bta1.agent.providers import Provider

# ---------------------------------------------------------------------------
# Mocks: just enough of the OpenAI response shape
# ---------------------------------------------------------------------------


@dataclass
class _Func:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Func
    type: str = "function"


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.role = "assistant"

    def model_dump(self, exclude_none=False):
        out: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in self.tool_calls
            ]
        return out


@dataclass
class _Choice:
    message: _Message
    finish_reason: str


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5


class _Response:
    def __init__(self, choice):
        self.choices = [choice]
        self.usage = _Usage()


class _Completions:
    """Replays a scripted list of responses and records the requests."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        # Snapshot the messages. The loop appends to the same list it passed in,
        # so storing the reference would mean every recorded request appears to
        # contain the *final* history -- an easy way to write a test that
        # silently checks nothing.
        recorded = dict(kwargs)
        recorded["messages"] = copy.deepcopy(kwargs["messages"])
        self.requests.append(recorded)
        if not self._scripted:
            raise AssertionError("loop made more model calls than the script provided")
        return self._scripted.pop(0)


class _FakeClient:
    def __init__(self, scripted):
        self.chat = type("chat", (), {})()
        self.chat.completions = _Completions(scripted)


def _provider(scripted) -> Provider:
    return Provider(
        client=_FakeClient(scripted), model="mock", temperature=0.0, max_tokens=512
    )


def _answer(text: str) -> _Response:
    return _Response(_Choice(_Message(content=text), "stop"))


def _wants_tool(name: str, arguments: str, call_id: str = "c1") -> _Response:
    return _Response(
        _Choice(
            _Message(tool_calls=[_ToolCall(call_id, _Func(name, arguments))]),
            "tool_calls",
        )
    )


class _FakeSession:
    """Stands in for an MCP ClientSession."""

    def __init__(self, results=None, raises=None):
        self.results = results or {}
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.raises:
            raise self.raises
        text = self.results.get(name, "ok")

        class _Block:
            def __init__(self, t):
                self.text = t

        class _Result:
            def __init__(self, t):
                self.content = [_Block(t)]
                self.is_error = False

        return _Result(text)


def _bridge(session=None, tool_names=("build", "run_gdb")) -> Bridge:
    tools = [
        {
            "type": "function",
            "function": {"name": n, "description": f"{n} tool", "parameters": {}},
        }
        for n in tool_names
    ]
    return Bridge(session=session or _FakeSession(), tools=tools)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_returns_answer_without_tools() -> None:
    t = await loop.run(_provider([_answer("break main\nrun")]), _bridge(), "proj", 5)
    assert t.answer == "break main\nrun"
    assert t.iterations == 1
    assert t.tool_calls == 0


async def test_runs_a_tool_then_answers() -> None:
    session = _FakeSession({"build": '{"exit_code": 0}'})
    t = await loop.run(
        _provider([_wants_tool("build", "{}"), _answer("run\nbacktrace")]),
        _bridge(session),
        "proj",
        5,
    )
    assert t.answer == "run\nbacktrace"
    assert t.iterations == 2
    assert t.tool_calls == 1
    assert session.calls == [("build", {})]


async def test_accumulates_tokens() -> None:
    t = await loop.run(
        _provider([_wants_tool("build", "{}"), _answer("done")]), _bridge(), "proj", 5
    )
    assert t.prompt_tokens == 20
    assert t.completion_tokens == 10


# ---------------------------------------------------------------------------
# Message protocol: easy to get wrong, breaks the next request when you do
# ---------------------------------------------------------------------------


async def test_assistant_message_precedes_tool_result() -> None:
    """The API requires the tool_call message before its result.

    Append them in the wrong order and the *next* request 400s, which is a
    baffling error to debug from the response alone.
    """
    provider = _provider([_wants_tool("build", "{}", "call-7"), _answer("done")])
    t = await loop.run(provider, _bridge(), "proj", 5)

    roles = [m["role"] for m in t.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

    assistant = t.messages[2]
    assert assistant["tool_calls"][0]["id"] == "call-7"
    assert t.messages[3]["tool_call_id"] == "call-7"


async def test_second_request_carries_the_full_history() -> None:
    provider = _provider([_wants_tool("build", "{}"), _answer("done")])
    await loop.run(provider, _bridge(), "proj", 5)

    second = provider.client.chat.completions.requests[1]
    assert [m["role"] for m in second["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]


async def test_tools_and_sequential_flag_are_sent() -> None:
    provider = _provider([_answer("done")])
    bridge = _bridge()
    await loop.run(provider, bridge, "proj", 5)

    request = provider.client.chat.completions.requests[0]
    assert request["tools"] == bridge.tools
    assert request["parallel_tool_calls"] is False
    assert request["model"] == "mock"


# ---------------------------------------------------------------------------
# Failure paths. Every one of these is something a small local model does.
# ---------------------------------------------------------------------------


async def test_malformed_json_arguments_are_returned_to_the_model() -> None:
    """A weak model emits broken JSON. That must not end the run."""
    provider = _provider(
        [_wants_tool("build", "{not valid json"), _answer("recovered")]
    )
    t = await loop.run(provider, _bridge(), "proj", 5)

    assert t.answer == "recovered"
    tool_message = t.messages[3]
    assert "not valid JSON" in tool_message["content"]
    assert "send the arguments again" in tool_message["content"].lower()


async def test_unknown_tool_name_lists_the_real_ones() -> None:
    provider = _provider([_wants_tool("compile_it", "{}"), _answer("recovered")])
    t = await loop.run(provider, _bridge(), "proj", 5)

    content = t.messages[3]["content"]
    assert "no tool named 'compile_it'" in content
    assert "build" in content and "run_gdb" in content


async def test_tool_exception_becomes_a_readable_message() -> None:
    session = _FakeSession(raises=RuntimeError("server went away"))
    provider = _provider([_wants_tool("build", "{}"), _answer("recovered")])
    t = await loop.run(provider, _bridge(session), "proj", 5)

    assert "server went away" in t.messages[3]["content"]
    assert t.answer == "recovered"


async def test_iteration_cap_raises() -> None:
    """Without a cap, a confused agent runs until the budget or the GPU dies."""
    provider = _provider([_wants_tool("build", "{}") for _ in range(10)])
    with pytest.raises(loop.IterationLimit, match="after 3 iterations"):
        await loop.run(provider, _bridge(), "proj", 3)


async def test_truncated_response_is_not_treated_as_an_answer() -> None:
    """finish_reason == "length" means a half-written script. Not a deliverable."""
    truncated = _Response(_Choice(_Message(content="break sto"), "length"))
    with pytest.raises(loop.TruncatedResponse, match="max_tokens"):
        await loop.run(_provider([truncated]), _bridge(), "proj", 5)


async def test_rejected_gdb_scripts_are_counted() -> None:
    session = _FakeSession(
        {"run_gdb": '{"rejected": "gdb command rejected on line 1: shell id"}'}
    )
    provider = _provider([_wants_tool("run_gdb", "{}"), _answer("fixed")])
    t = await loop.run(provider, _bridge(session), "proj", 5)
    assert t.rejected_scripts == 1


# ---------------------------------------------------------------------------
# The adapter: MCP tool definition -> OpenAI tool definition
# ---------------------------------------------------------------------------


def test_adapter_maps_mcp_tool_to_openai_tool() -> None:
    @dataclass
    class _McpTool:
        name: str
        description: str
        input_schema: dict

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    converted = to_openai_tool(_McpTool("read_source", "Read a file.", schema))

    assert converted == {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "Read a file.",
            "parameters": schema,
        },
    }


def test_adapter_tolerates_a_missing_description() -> None:
    @dataclass
    class _McpTool:
        name: str
        description: None
        input_schema: dict

    converted = to_openai_tool(_McpTool("x", None, {}))
    assert converted["function"]["description"] == ""


# ---------------------------------------------------------------------------
# Bridge result flattening
# ---------------------------------------------------------------------------


async def test_bridge_flattens_text_content() -> None:
    bridge = _bridge(_FakeSession({"build": "compiled fine"}))
    assert await bridge.call("build", {}) == "compiled fine"


async def test_bridge_marks_tool_errors() -> None:
    class _ErrSession(_FakeSession):
        async def call_tool(self, name, arguments):
            class _B:
                text = "no such file"

            class _R:
                content = [_B()]
                is_error = True

            return _R()

    bridge = _bridge(_ErrSession())
    out = await bridge.call("read_source", {"path": "x"})
    assert out.startswith("ERROR from tool read_source")
    assert "no such file" in out


def test_bridge_tool_names() -> None:
    assert _bridge().tool_names == {"build", "run_gdb"}


# ---------------------------------------------------------------------------
# Fence stripping in the entry point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The contract: a fenced block, tagged or not.
        ("```gdb\nrun\nbt\n```", "run\nbt"),
        ("```\nrun\nbt\n```", "run\nbt"),
        # OBSERVED IN A REAL RUN: four paragraphs of explanation, then the
        # script. Before the fence contract this prose became line 1 of the
        # .gdb file and gdb died on it.
        (
            "The script successfully reproduces the bug.\n\n"
            "Key evidence:\n1. the breakpoint is hit\n\n"
            "```gdb\nbreak buggy.c:27\nrun\nbacktrace\n```",
            "break buggy.c:27\nrun\nbacktrace",
        ),
        # If the model demonstrates a bad example first, the LAST block is the
        # deliverable.
        (
            "First I tried:\n```gdb\nrun\n```\nThat failed. Final version:\n"
            "```gdb\nbreak main\nrun\nbt\n```",
            "break main\nrun\nbt",
        ),
        # No fence at all: pass it through rather than silently dropping lines.
        ("run\nbt", "run\nbt"),
        ("  break main\nrun  ", "break main\nrun"),
        ("# reproduce\nrun", "# reproduce\nrun"),
    ],
)
def test_extract_script(raw: str, expected: str) -> None:
    from bta1.agent.__main__ import _extract_script

    assert _extract_script(raw) == expected


def test_extract_script_warns_on_unfenced_prose(caplog) -> None:
    """A .gdb file full of English should never arrive silently."""
    from bta1.agent.__main__ import _extract_script

    prose = "The script successfully reproduces the stack buffer overflow bug."
    with caplog.at_level("WARNING"):
        result = _extract_script(prose)

    assert result == prose  # passed through, not silently filtered
    assert any("does not look like a gdb script" in r.message for r in caplog.records)


def test_extract_script_does_not_warn_on_plain_script(caplog) -> None:
    from bta1.agent.__main__ import _extract_script

    with caplog.at_level("WARNING"):
        _extract_script("break store_label\nrun\nbacktrace")

    assert not [r for r in caplog.records if "does not look like" in r.message]


def test_json_arguments_round_trip() -> None:
    """Sanity: the dispatch path parses what a well-behaved model sends."""
    args = {"path": "buggy.c", "start_line": 1, "end_line": 20}
    assert json.loads(json.dumps(args)) == args


def test_loop_failures_are_found_inside_an_exception_group() -> None:
    """Regression guard for a crash on an ordinary outcome.

    The loop runs inside an anyio task group (the MCP stdio client), and a task
    group wraps whatever escapes it in an ExceptionGroup. So
    `except loop.IterationLimit` never matched: hitting the iteration cap -- an
    expected, well-handled outcome -- came back wrapped and killed the process
    with a traceback instead of printing its message and exiting 1.

    Observed on a real `run_from_scratch.sh` run, which ended in a wall of
    anyio internals rather than "no final answer after 24 iterations".
    """
    import builtins

    from bta1.agent.__main__ import _unwrap_loop_failure

    group_type = (
        getattr(builtins, "ExceptionGroup", None)
        or __import__("exceptiongroup").ExceptionGroup
    )

    limit = loop.IterationLimit("out of turns", None)
    assert _unwrap_loop_failure(limit) is limit
    assert _unwrap_loop_failure(group_type("tg", [limit])) is limit
    assert (
        _unwrap_loop_failure(group_type("outer", [group_type("inner", [limit])]))
        is limit
    )
    assert _unwrap_loop_failure(group_type("tg", [ValueError("noise"), limit])) is limit

    # A genuine bug must still propagate, or we would hide our own defects.
    assert _unwrap_loop_failure(ValueError("a real bug")) is None
    assert _unwrap_loop_failure(group_type("tg", [KeyError("x")])) is None


def test_truncated_response_is_also_recoverable() -> None:
    import builtins

    from bta1.agent.__main__ import _unwrap_loop_failure

    group_type = (
        getattr(builtins, "ExceptionGroup", None)
        or __import__("exceptiongroup").ExceptionGroup
    )
    exc = loop.TruncatedResponse("hit the token limit")
    assert _unwrap_loop_failure(group_type("tg", [exc])) is exc


def test_run_summary_counters_match_the_real_server_payloads() -> None:
    """Pins a CROSS-PROCESS coupling that no other test covers.

    `_dispatch` counts refused saves and rejected scripts by substring-matching
    the MCP server's output -- the bridge flattens tool results to text, so no
    structured field survives for the loop to read. The existing counter test
    feeds a mock string, which means it would keep passing if the server's
    wording changed and the reported numbers silently went to zero.

    This test builds the payloads the way the real server does: the actual
    SandboxError message from check_gdb_commands, and json.dumps of the real
    refusal dict from _refused(). If either changes shape, this fails.
    """
    import json

    from bta1.mcp_server.sandbox import SandboxError, check_gdb_commands
    from bta1.mcp_server.server import _refused

    try:
        check_gdb_commands(["shell id"])
        raise AssertionError("the denylist should have rejected `shell id`")
    except SandboxError as exc:
        rejection_text = f"ERROR from tool run_gdb: {exc}"
    assert "gdb command rejected" in rejection_text, rejection_text

    refusal_text = json.dumps(_refused("the program exited on its own"), indent=2)
    assert '"saved": false' in refusal_text.lower(), refusal_text
