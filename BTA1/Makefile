# Makefile, not CMake. One build system, one place to keep correct.
#
# Everything you need to run this project is a target here. If a command is
# worth typing twice, it belongs in this file.

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
EXAMPLE := examples/overflow

.PHONY: help setup test lint fmt run run-multi inspect phoenix probe-tools example clean

help:
	@echo "setup    - create $(VENV) and install the project (editable) + dev tools"
	@echo "test     - run the test suite (no LLM required)"
	@echo "lint     - ruff check + format check"
	@echo "fmt      - ruff format"
	@echo "example  - build the buggy example program"
	@echo "inspect  - open MCP Inspector against the MCP server (needs Node/npx)"
	@echo "phoenix  - start the local Phoenix trace viewer on :6006"
	@echo "probe-tools - check the configured LLM can actually emit tool calls"
	@echo "run      - run the agent against $(EXAMPLE) (one bug)"
	@echo "run-multi - run the agent against examples/multi (three bugs)"
	@echo "clean    - remove build artifacts and caches"

$(VENV)/bin/activate:
	python3 -m venv $(VENV)

setup: $(VENV)/bin/activate
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(VENV)/bin/pytest -q

lint:
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

fmt:
	$(VENV)/bin/ruff format src tests

example:
	$(MAKE) -C $(EXAMPLE)

# MCP Inspector is the official interactive debugger for MCP servers. It lists
# the tools, shows their schemas, and lets you invoke them by hand -- with no
# agent and no model involved. Start here when a tool misbehaves.
inspect:
	npx @modelcontextprotocol/inspector \
		$(PY) -m bta1.mcp_server.server --project-root $(EXAMPLE)

phoenix:
	@$(PY) -c "import phoenix" 2>/dev/null || \
		{ echo "Phoenix not installed. Run: $(PIP) install -e '.[trace]'"; exit 1; }
	$(VENV)/bin/phoenix serve

# Assumption probe, not a test. Run this BEFORE trusting the agent: it confirms
# the configured model actually returns tool_calls in the OpenAI format. A model
# that emits tool calls as prose will fail here in two seconds instead of
# wasting an afternoon of loop debugging.
probe-tools:
	$(PY) -m bta1.agent.probe_tool_calls

run: example
	$(PY) -m bta1.agent --project-root $(EXAMPLE) --output-dir out

# The three-bug example: proves the agent keeps looking after the first bug.
# Slower -- finding N bugs needs several times the turns of finding one.
run-multi:
	$(MAKE) -C examples/multi
	$(PY) -m bta1.agent --project-root examples/multi --output-dir out

clean:
	$(MAKE) -C $(EXAMPLE) clean
	$(MAKE) -C examples/multi clean
	rm -rf .pytest_cache .pytest-tmp .ruff_cache out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
