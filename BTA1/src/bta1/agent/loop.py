"""The agent loop.

THIS IS THE LESSON. An agent is a `while` loop around one API call:

    1. send the conversation + the tool schemas to the model
    2. if the model asked for a tool, run it and append the result
    3. otherwise it answered -- return
    4. repeat

That is all. Everything else in this repo is plumbing around these twenty lines.

WHAT A FRAMEWORK WOULD HIDE. If this were LangChain's AgentExecutor, LangGraph,
or the OpenAI Agents SDK, these pieces would be inside the library instead of in
front of you:

  * the loop itself, and its termination condition
  * accumulating the message list (including appending the assistant's own
    tool-call message before the results -- omit it and the API rejects the
    next request)
  * dispatching a tool call by name and encoding the result back as a message
  * converting tool schemas between formats (see mcp_bridge.to_openai_tool)
  * retry and error-recovery policy
  * the iteration cap

None of that is hard. It is just invisible when a framework owns it, and
invisible machinery is a poor thing to teach. Once you have written it once,
reach for a framework if you want -- you will know what it is doing.

WHY ASYNC: the MCP client API is async, so the loop is too. Nothing about the
control flow changes; mentally delete the `await`s and the shape is identical.
"""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from .mcp_bridge import Bridge
from .prompts import SYSTEM_PROMPT, initial_task
from .providers import Provider

log = logging.getLogger(__name__)


class IterationLimit(RuntimeError):
    """The agent used its whole budget without producing a final answer.

    Carries the transcript. Without it the only report was the iteration count,
    and diagnosing a loop meant guessing at what the agent had been doing --
    which cost three runs and two wrong hypotheses before anyone looked.
    """

    def __init__(self, message: str, transcript: Transcript | None = None) -> None:
        super().__init__(message)
        self.transcript = transcript


class TruncatedResponse(RuntimeError):
    """The model ran out of output tokens mid-answer."""


@dataclass
class Transcript:
    """What happened, for reporting and for tracing."""

    answer: str = ""
    iterations: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rejected_scripts: int = 0
    refused_saves: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


async def run(
    provider: Provider,
    bridge: Bridge,
    project_root: str,
    max_iterations: int,
    tracer: Any | None = None,
    evidence: str = "",
) -> Transcript:
    """Drive the agent until it answers, or the iteration budget runs out."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_task(project_root, evidence)},
    ]
    t = Transcript(messages=messages)

    for iteration in range(1, max_iterations + 1):
        t.iterations = iteration

        response = await provider.client.chat.completions.create(
            model=provider.model,
            messages=messages,
            tools=bridge.tools,
            temperature=provider.temperature,
            max_tokens=provider.max_tokens,
            # Small local models produce malformed *parallel* tool calls far
            # more often than sequential ones, and a strictly sequential loop is
            # easier to read and to trace. One call per turn.
            parallel_tool_calls=False,
        )

        if usage := response.usage:
            t.prompt_tokens += usage.prompt_tokens or 0
            t.completion_tokens += usage.completion_tokens or 0

        choice = response.choices[0]
        message = choice.message

        # Append the assistant's message BEFORE running any tools. The API
        # requires every tool result to follow the tool_call that asked for it;
        # skip this and the next request is rejected.
        messages.append(message.model_dump(exclude_none=True))

        if choice.finish_reason == "length":
            # Distinct from a finished answer. A half-written gdb script is not
            # a deliverable, and silently accepting one would hand the user a
            # broken artifact that looks complete.
            raise TruncatedResponse(
                f"model hit the {provider.max_tokens}-token output limit on "
                f"iteration {iteration}; raise llm.max_tokens in config.yaml"
            )

        if not message.tool_calls:
            t.answer = (message.content or "").strip()
            log.info(
                "done in %d iteration(s), %d tool call(s)", iteration, t.tool_calls
            )
            return t

        for call in message.tool_calls:
            t.tool_calls += 1
            result = await _dispatch(bridge, call, t, tracer)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )

    raise IterationLimit(
        f"no final answer after {max_iterations} iterations "
        f"({t.tool_calls} tool calls). Raise agent.max_iterations, or look at "
        "the trace: the agent is probably looping on a failing hypothesis.",
        t,
    )


def _summarise(arguments: str | None, limit: int = 160) -> str:
    """One readable line of a tool call's arguments, for the log.

    A gdb script argument can be hundreds of characters of newlines; printed
    raw it buries every other log line.

    The cap is a real trade-off, not a formatting nicety: at 160 characters a
    save_reproduction call shows its title and the start of its description and
    then runs out, so THE SCRIPT ITSELF NEVER APPEARS. Diagnosing a
    save/refuse loop from the log alone is impossible as a result -- the
    refusal reason (now logged by the server) and the failed-transcript dump
    are what cover that gap. Raise this if you are debugging script content and
    do not mind the noise.
    """
    text = " ".join((arguments or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


async def _dispatch(
    bridge: Bridge, call: Any, t: Transcript, tracer: Any | None
) -> str:
    """Run one tool call, converting every failure into text the model can read.

    Nothing in here raises. A model that gets a readable error can correct
    itself on the next turn; a model that gets an exception just ends the run.
    Small local models in particular emit malformed JSON arguments often enough
    that treating it as fatal would make the agent unusable.
    """
    name = call.function.name
    log.info("tool call: %s %s", name, _summarise(call.function.arguments))

    try:
        arguments = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        log.warning("malformed tool arguments from model: %s", exc)
        return (
            f"ERROR: your arguments for {name} were not valid JSON ({exc}). "
            "Send the arguments again as a single valid JSON object."
        )

    if name not in bridge.tool_names:
        available = ", ".join(sorted(bridge.tool_names))
        return f"ERROR: no tool named {name!r}. Available tools: {available}."

    span = (
        tracer.start_as_current_span(f"tool.{name}")
        if tracer is not None
        else nullcontext()
    )
    with span as active:
        if active is not None and hasattr(active, "set_attribute"):
            active.set_attribute("tool.name", name)
            active.set_attribute("tool.arguments", json.dumps(arguments)[:4096])

        try:
            result = await bridge.call(name, arguments)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            log.warning("tool %s raised: %s", name, exc)
            result = f"ERROR calling {name}: {exc}"

        # AUDIT NOTE: these two counters are derived by SUBSTRING-MATCHING the
        # other process's output, which couples the run summary to the MCP
        # server's exact wording and JSON formatting. Both strings were verified
        # against the real server as of this writing -- sandbox.py still emits
        # "gdb command rejected on line N", and a refused save still serialises
        # as `"saved": false` -- but nothing makes that true tomorrow.
        #
        # The coupling exists because mcp_bridge.call flattens every tool result
        # to text on purpose (the model's context is text), so by the time the
        # loop sees a refusal there is no structured field left to read. The
        # guard is a test that feeds this function the REAL server payload
        # rather than a mock string, so a wording change fails loudly instead of
        # silently zeroing a reported number.
        #
        # Only the summary line is affected: nothing branches on these.
        if "gdb command rejected" in result:
            t.rejected_scripts += 1
        if '"saved": false' in result.lower():
            t.refused_saves += 1

        if active is not None and hasattr(active, "set_attribute"):
            active.set_attribute("tool.result_bytes", len(result))

    return result
