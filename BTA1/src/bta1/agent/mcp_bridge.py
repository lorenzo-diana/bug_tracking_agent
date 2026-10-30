"""Connects the agent to the MCP server, and translates between the two worlds.

THE ADAPTER BELOW IS THE POINT OF THIS FILE. An MCP tool definition and an
OpenAI function definition are the same idea in different envelopes, and
`to_openai_tool()` is the whole translation -- about ten lines:

    MCP                        OpenAI
    -------------------------  ---------------------------------
    tool.name               -> function.name
    tool.description        -> function.description   (the docstring)
    tool.input_schema       -> function.parameters    (JSON Schema)

That near-1:1 mapping is why MCP composes with any model that supports tool
calling, and why we did not need a framework to glue them together. Read this
function before anything else in the agent.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def to_openai_tool(tool: Any) -> dict[str, Any]:
    """Convert one MCP tool definition into an OpenAI tool definition.

    `tool.input_schema` is the mcp 2.x attribute name; it was `inputSchema` in
    mcp 1.x. Verified against the installed SDK, whose `Tool` model fields are
    name / title / description / input_schema / ... -- and pyproject pins
    `mcp>=2,<3`, so the snake_case spelling is the only one that can occur here.
    Relaxing that pin downward would break this line silently, with an
    AttributeError at the first tool listing.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


@dataclass
class Bridge:
    """A live MCP session plus the tool schemas to hand the model."""

    session: ClientSession
    tools: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_names(self) -> set[str]:
        return {t["function"]["name"] for t in self.tools}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool and flatten the result to text for the model.

        The model's context is text, so structured results get JSON-encoded.
        Tool *errors* are returned as text too, not raised: an error the model
        can read is an error the model can recover from, and "no such file,
        here is what exists" is a far more useful turn than a crashed run.
        """
        result = await self.session.call_tool(name, arguments)

        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                # Best effort for a non-text block (image, embedded resource).
                # `default=str` means a pydantic model serialises to its repr
                # rather than to its fields -- ugly but never raising, which is
                # the right trade in a path whose only job is to put *something*
                # in front of the model. No BTA1 tool returns a non-text block,
                # so this branch does not fire in practice.
                parts.append(json.dumps(block, default=str))
        payload = "\n".join(parts) if parts else "(no output)"

        # mcp marks tool failures ON THE RESULT rather than raising, so a failed
        # tool call looks like a successful JSON-RPC round trip. Miss this and
        # every SandboxError refusal reads to the model as a normal answer.
        #
        # `is_error` is the mcp 2.x field name (`isError` in 1.x), and pyproject
        # pins `mcp>=2,<3`. getattr with a default rather than attribute access
        # so a result type without the field degrades to "not an error" instead
        # of raising inside the error path.
        if getattr(result, "is_error", False):
            return f"ERROR from tool {name}: {payload}"
        return payload


@asynccontextmanager
async def connect(project_root: Path, config_path: Path, output_dir: Path):
    """Launch the MCP server as a subprocess and yield a ready Bridge.

    The server is a *separate process*, spoken to over stdio. That is the trust
    boundary: this side holds no privilege, and everything dangerous happens
    behind a JSON-RPC call that validates its arguments.

    We launch it with the same interpreter that is running us, so an activated
    virtualenv is inherited without the caller thinking about it.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "bta1.mcp_server.server",
            "--config",
            str(config_path),
            "--project-root",
            str(project_root),
            "--output-dir",
            str(output_dir),
        ],
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        yield Bridge(
            session=session,
            tools=[to_openai_tool(t) for t in listed.tools],
        )
