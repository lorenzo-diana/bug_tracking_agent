"""ASSUMPTION PROBE -- run this before trusting the agent. Not a test.

    make probe-tools

It asks the configured model to call one trivial tool and prints exactly what
came back. Nothing else.

WHY THIS EXISTS. The highest-risk unknown in this project is whether your local
model actually emits `tool_calls` in the OpenAI response format. If the server's
tool-call parser is misconfigured, or a reasoning model's thinking output
collides with tool-call parsing, the model returns tool calls as *prose* -- and
the agent loop, which is correct, will appear broken. Discovering that after
writing the loop costs a day. Discovering it here costs twenty seconds.

For vLLM this usually means the server needs:
    --enable-auto-tool-choice --tool-call-parser <parser-for-your-model>
and, for reasoning models, either a matching --reasoning-parser or thinking
disabled.

PROBES VS GATES: a probe checks that the ground is solid before you build on it,
and is thrown away. A gate checks that what you built is correct, and is kept
(see tests/). This file is a probe that we keep only because students need to
re-run it on their own server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .. import config
from . import providers

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_build_status",
        "description": "Return whether the project currently compiles.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Make target to check.",
                }
            },
            "required": ["target"],
        },
    },
}


async def _probe(config_path: Path, provider_name: str | None) -> int:
    cfg = config.load(config_path)
    if provider_name:
        cfg.llm.provider = provider_name
    provider = providers.build(cfg.llm)

    print(f"provider : {cfg.llm.provider}")
    print(f"model    : {provider.model}")
    print(f"base_url : {provider.client.base_url}")
    print("-" * 60)

    try:
        response = await provider.client.chat.completions.create(
            model=provider.model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Check whether the project compiles. Use the "
                        "get_build_status tool with target 'all'."
                    ),
                }
            ],
            tools=[PROBE_TOOL],
            temperature=0,
            max_tokens=256,
            parallel_tool_calls=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not reach the model: {exc}")
        print("\nIs the server running, and is base_url in config.yaml correct?")
        return 1

    choice = response.choices[0]
    message = choice.message
    print(f"finish_reason : {choice.finish_reason}")
    print(f"content       : {(message.content or '')[:300]!r}")
    print(f"tool_calls    : {message.tool_calls}")
    print("-" * 60)

    if not message.tool_calls:
        print("FAIL: the model did not return tool_calls.")
        if message.content and "get_build_status" in message.content:
            print(
                "\nIt mentioned the tool in PROSE instead. That is the classic\n"
                "symptom of a server that is not parsing tool calls:\n"
                "  vLLM: --enable-auto-tool-choice --tool-call-parser <parser>\n"
                "  Reasoning models: add --reasoning-parser, or disable thinking."
            )
        else:
            print("\nThe model may not support tool calling. Try another model.")
        return 1

    call = message.tool_calls[0]
    print(f"name      : {call.function.name}")
    print(f"arguments : {call.function.arguments!r}")

    try:
        parsed = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        print(
            "\nPARTIAL: tool_calls arrived but the arguments are not valid "
            f"JSON ({exc})."
        )
        print("The loop tolerates this by handing the error back, but it wastes")
        print("turns. Enable guided/constrained decoding on the server.")
        return 1

    print(f"parsed    : {parsed}")
    if call.function.name != "get_build_status":
        print(f"\nPARTIAL: called {call.function.name!r} instead. Tool calling works,")
        print("but the model is not following the schema closely.")
        return 1

    print("\nPASS: the model emits well-formed tool calls. Safe to run the agent.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--provider", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_probe(args.config, args.provider)))


if __name__ == "__main__":
    main()
