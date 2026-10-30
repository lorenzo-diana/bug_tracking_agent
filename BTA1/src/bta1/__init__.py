"""A teaching reference implementation of an AI debugging agent.

Two halves, deliberately separated:
  mcp_server/ -- owns every privileged action (files, make, gdb)
  agent/      -- owns the reasoning loop and nothing else

See docs/architecture.md.
"""

__version__ = "0.1.0"
