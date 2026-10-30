"""The MCP server: the only component with any privilege.

The agent reasons; this process acts. Every filesystem read and every subprocess
lives here, behind the sandbox in sandbox.py. That boundary is the point of the
architecture: it means the whole tool surface is testable with no LLM, drivable
by hand through MCP Inspector, and reusable by any MCP client (Claude Desktop,
Claude Code, Cursor, your own agent).

Each tool's DOCSTRING BECOMES ITS MCP DESCRIPTION, which becomes the model's only
knowledge of the tool. Write them as documentation for a capable stranger: what
it does, when to use it, what it returns, and what it will refuse.

TRANSPORT NOTE -- stdio reserves stdout for JSON-RPC. A stray print() here
corrupts the protocol and the client dies with a confusing parse error. All
logging goes to stderr. This is the single most common way to break an MCP
server.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import config
from ..config import Config, SandboxConfig
from . import runners
from .sandbox import SandboxError, cap_output, check_gdb_commands, safe_path

# stderr, never stdout. See the module docstring.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(levelname)s [mcp_server] %(message)s",
)
log = logging.getLogger(__name__)

# MCPServer is the decorator-based server API of the official SDK. (In mcp 1.x
# this class was called FastMCP; the decorator pattern is unchanged.) A tool is
# a plain function with type hints: the JSON schema the model sees is generated
# from the signature, and its description from the docstring.
mcp = MCPServer("bta1-tools")

# Set once at startup by main(). The agent never sees or sets the project root:
# it is the anchor every path is validated against, so letting the model
# influence it would defeat the whole control.
_ROOT: Path
_SANDBOX: SandboxConfig

# Where accepted reproduction scripts are written. Set by configure(); never
# influenced by the agent.
_OUTPUT_DIR: Path

# Reproductions accepted this run, in the order they were saved.
#
# Used only to NUMBER the scripts (bug-01, bug-02, ...). It is not how the host
# learns what was found: the host is a different process, so it cannot read
# this list at all -- it globs the output directory and parses each script's
# header. An accessor returning this list used to exist, documented as "read by
# the host to build its report", which was never true.
#
# WHY SAVING IS A TOOL -- a deliberate reversal. The original design said the
# .gdb script must NOT be a tool, because "the deliverable is the loop's return
# value". That held while there was exactly one artifact. With N bugs it breaks
# down for two concrete reasons:
#
#   1. N scripts in one final message risks hitting max_tokens, and a truncated
#      answer costs the whole run (see loop.TruncatedResponse).
#   2. Saving per bug lets us VERIFY each script at save time -- run it, require
#      that it actually fails and does not error. Real runs produced scripts
#      that errored partway through, or ran the program with no arguments so
#      nothing crashed. Both looked like success to the model. A tool that
#      refuses them turns that into feedback the agent can act on.
#
# The principle survives in a better form: artifacts the agent accumulates
# incrementally belong behind a tool; a single final answer does not.
_SAVED: list[dict[str, Any]] = []

# Hard cap on accepted reproductions; -1 means no cap. Set by configure() from
# agent.max_bugs. Enforced here rather than in the agent loop because this
# process owns the output directory: a cap the model could argue its way past
# would not be a cap.
_MAX_BUGS: int = -1

# Directory names that are never worth showing the agent.
_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "out"}


@mcp.tool()
def list_files(
    subdir: Annotated[
        str,
        Field(
            description="Directory relative to the project root. Use '.' for the top."
        ),
    ] = ".",
) -> list[str]:
    """List the source files in the project, relative to the project root.

    Start here. You cannot read a file until you know it exists, and guessing
    filenames wastes turns. Returns a sorted list of paths; build artifacts,
    dotfiles and compiled binaries are excluded.
    """
    base = safe_path(_ROOT, subdir)
    if not base.is_dir():
        raise SandboxError(f"not a directory: {subdir!r}")

    out: list[str] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        # Filter on the path RELATIVE to the root, never the absolute one: the
        # project may sit under a directory that is itself hidden or named like
        # a build dir (e.g. the test suite's .pytest-tmp), and judging absolute
        # parts would then hide every file in the project.
        rel = p.relative_to(_ROOT)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.suffix == "" and os.access(p, os.X_OK):
            continue  # compiled binaries are noise
        out.append(str(rel))
    return out


@mcp.tool()
def read_source(
    path: Annotated[str, Field(description="File path relative to the project root.")],
    start_line: Annotated[
        int, Field(description="First line to return (1-based).", ge=1)
    ] = 1,
    end_line: Annotated[
        int | None,
        Field(description="Last line to return, inclusive. Omit for the whole span."),
    ] = None,
) -> str:
    """Read a range of lines from a source file, with line numbers.

    Line numbers are included so you can cite exact locations when setting
    breakpoints -- `break file.c:27` needs to be right.

    Read selectively: the span is capped (see the error message for the limit),
    and dumping a whole file crowds out your own reasoning. Narrow in on the
    function you suspect.
    """
    resolved = safe_path(_ROOT, path)
    if not resolved.is_file():
        raise SandboxError(f"not a file: {path!r}")

    try:
        lines = resolved.read_text(errors="replace").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SandboxError(f"cannot read {path!r} as text: {exc}") from exc

    last = len(lines) if end_line is None else end_line
    if last < start_line:
        raise SandboxError(f"end_line ({last}) is before start_line ({start_line})")
    if last - start_line + 1 > _SANDBOX.max_read_lines:
        raise SandboxError(
            f"requested {last - start_line + 1} lines but the limit is "
            f"{_SANDBOX.max_read_lines}. Narrow the range."
        )

    chunk = lines[start_line - 1 : last]
    if not chunk:
        raise SandboxError(
            f"{path!r} has {len(lines)} lines; {start_line} is past the end"
        )

    width = len(str(start_line + len(chunk) - 1))
    body = "\n".join(
        f"{start_line + i:>{width}}| {text}" for i, text in enumerate(chunk)
    )
    return cap_output(body, _SANDBOX.max_output_bytes)


@mcp.tool()
def build(
    target: Annotated[
        str, Field(description="Make target. Empty string builds the default target.")
    ] = "",
) -> dict[str, Any]:
    """Run `make` in the project root to compile the program with debug symbols.

    Build before debugging: gdb needs an up-to-date binary with symbols. The
    project's Makefile already uses the right flags (-g -O0), so you normally
    just call this with no target.

    Returns exit_code, stdout and stderr. **exit_code 0 means the build
    succeeded**; anything else is a compile failure and stderr tells you why.
    """
    result = runners.run_make(target, cwd=_ROOT, cfg=_SANDBOX)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }


@mcp.tool()
def run_gdb(
    program: Annotated[
        str,
        Field(description="Compiled binary to debug, relative to the project root."),
    ],
    args: Annotated[
        list[str], Field(description="Command-line arguments to pass to the program.")
    ],
    gdb_commands: Annotated[
        list[str],
        Field(
            description=(
                "The gdb script, ONE COMMAND PER LIST ITEM (not a single "
                "newline-joined string)."
            )
        ),
    ],
) -> dict[str, Any]:
    """Run gdb in batch mode on the program and return what it printed.

    This is how you test a hypothesis. Set a breakpoint where you think the bug
    is, inspect the state there, continue, and read the result. Then revise and
    call again.

    Returns:
      signal    - the inferior's fatal signal, e.g. "SIGSEGV", or null if it did
                  not crash. **THIS, not exit_code, tells you whether the bug
                  reproduced.** gdb exits 0 even when the program segfaults,
                  because it is reporting on its own success, not the program's.
      exit_code - gdb's own status. Non-zero means *gdb* failed: a bad command,
                  a missing binary, a resource limit.
      stdout,
      stderr    - gdb's output, truncated if very long.
      rejected  - set if your script violated policy. Nothing ran; fix and retry.

    Refused commands: `shell`, `python`, `pipe`, `compile`, `source`, `file`,
    `define`, `eval` and their abbreviations -- they can execute arbitrary code
    or escape the project directory. Write commands out in full; aggressive
    abbreviations may be rejected by the prefix check.
    """
    binary = safe_path(_ROOT, program)

    try:
        check_gdb_commands(gdb_commands)
    except SandboxError as exc:
        # A rejection is returned, not raised, so the agent gets a readable
        # reason and a chance to revise rather than an opaque tool error.
        log.warning("rejected gdb script: %s", exc)
        return {
            "rejected": str(exc),
            "signal": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
        }

    # The script goes in the system temp dir, not the project: it is our
    # artifact, not the agent's, and it must not appear in list_files().
    with tempfile.NamedTemporaryFile(
        "w", suffix=".gdb", prefix="agent-", delete=False
    ) as handle:
        handle.write("\n".join(gdb_commands) + "\n")
        script = Path(handle.name)

    try:
        result, crash_signal = runners.run_gdb(
            binary, args, script, cwd=_ROOT, cfg=_SANDBOX
        )
    finally:
        script.unlink(missing_ok=True)

    return {
        "rejected": None,
        "signal": crash_signal,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }


def _refused(reason: str) -> dict[str, Any]:
    """The refusal reply, shaped once, and ALWAYS LOGGED.

    Five sites returned this four-key dict literally. The shape is part of the
    tool's documented contract, so having it written out five times was five
    chances to drift -- and the keys the model reads are the last thing that
    should differ between one refusal and another.

    THE LOG LINE IS NOT DECORATION. Refusals are the most informative events
    this server produces: they are the only record of the agent being told no,
    and the reason is usually the whole diagnosis. Without it a refusal is
    visible only inside the model's context, which means a save/refuse loop
    looks identical to a slow run from the outside.
    """
    log.warning("save_reproduction refused: %s", reason.replace("\n", " ")[:300])
    return {"saved": False, "refused": reason, "signal": None, "path": None}


def _head(output: str, limit: int = 400) -> str:
    """The FRONT of gdb's output, where the first error is.

    This used to be `output[-400:]`, which cut off the head and handed the
    model a message starting mid-word ("gging using libthread_db..."). gdb
    reports the first failure early and then cascades, so the tail is the
    least informative part of it.
    """
    text = output.strip()
    return text if len(text) <= limit else text[:limit] + " [...]"


@mcp.tool()
def save_reproduction(
    title: Annotated[
        str,
        Field(
            description=(
                "Short name for this bug, e.g. 'stack buffer overflow in store_label'."
            )
        ),
    ],
    description: Annotated[
        str,
        Field(
            description=(
                "What the bug is, which source line causes it, and how the "
                "failure manifests. A few sentences."
            )
        ),
    ],
    program: Annotated[
        str,
        Field(
            description=(
                "Compiled binary this script debugs, relative to the project root."
            )
        ),
    ],
    args: Annotated[
        list[str],
        Field(
            description=(
                "Command-line arguments that trigger this bug -- the SAME list "
                "you passed to run_gdb. A `set args` line is written into the "
                "script for you, so do not add one yourself."
            )
        ),
    ],
    gdb_commands: Annotated[
        list[str],
        Field(description="The gdb script, ONE COMMAND PER LIST ITEM."),
    ],
) -> dict[str, Any]:
    """Save a verified reproduction script for ONE bug. Call this once per bug.

    This is how you deliver your findings. A project may contain several
    independent bugs; report each one with its own call to this tool, then move
    on and keep looking. Do not bundle two bugs into one script.

    The script is **verified before it is accepted**: it runs under gdb, and the
    save is REFUSED if the script errors (a bad gdb command) or if the program
    does not actually fail. A script that does not reproduce anything is not a
    reproduction.

    Returns:
      saved     - true if the script was accepted and written to disk.
      path      - where it was written.
      signal    - the fatal signal observed, e.g. "SIGSEGV" or "SIGFPE".
      refused   - why it was rejected, if it was. Fix the script and call again.

    There is a configured limit on how many reproductions one run may save. If
    you reach it the refusal says so explicitly; stop looking for bugs at that
    point and reply with your summary.

    The script is made self-contained for you: pass the triggering `args` and a
    `set args` line is written in, so whoever runs the file reproduces the bug
    with the script alone. Do not rely on absolute addresses -- they change on
    every run.
    """
    # The cap is checked FIRST, before path validation and before gdb runs: once
    # it is reached the answer cannot change, so there is no point spending a
    # subprocess to arrive at the same refusal. The message tells the agent to
    # stop rather than to fix something, which is the opposite of every other
    # refusal here -- a retry would be the wrong response.
    if _MAX_BUGS != -1 and len(_SAVED) >= _MAX_BUGS:
        return _refused(
            f"the limit of {_MAX_BUGS} saved reproductions has been reached "
            f"(agent.max_bugs). Do NOT retry this call and do not look for "
            "more bugs: reply with your summary of what you have already "
            "saved. If this project really has more bugs than that, the limit "
            "in config.yaml is what needs raising."
        )

    binary = safe_path(_ROOT, program)

    try:
        check_gdb_commands(gdb_commands)
    except SandboxError as exc:
        return _refused(str(exc))

    # THE INPUT MUST LIVE IN THE SCRIPT, and the server puts it there.
    #
    # run_gdb takes `args` as a parameter and injects them out of band, so a
    # probe crashes without the script mentioning them. The agent then saved
    # those same commands as the artifact -- and the input silently vanished.
    # The program ran bare, printed its usage, exited 1, and every later command
    # failed with "The program is not being run." Eleven identical refusals in
    # one run: the agent was punished for repeating what had just worked.
    #
    # Telling it to write `set args` itself (the prompt did) loses to a tool
    # interface that demonstrates the opposite. So both tools now take the same
    # (program, args, commands) triple and the server emits the line.
    #
    # NO QUOTING IS POSSIBLE HERE, which is a direct consequence of our own
    # `set startup-with-shell off`. gdb normally hands `set args` to a shell,
    # which does the quoting; with the shell gone gdb splits on whitespace
    # itself and takes quotes literally. Measured, all three forms:
    #
    #     set args 'two words'   -> argv[1] == "'two"
    #     set args "two words"   -> argv[1] == "\""
    #     set args two\ words    -> argv[1] == "two\\"
    #
    # run_gdb is unaffected because it builds a real argv (`--args program
    # ...`). A saved script has no such channel, so an argument containing
    # whitespace cannot be expressed in the artifact AT ALL -- and writing one
    # anyway would hand over a .gdb file that silently feeds the program
    # different input than the one that was verified. Refuse instead.
    unrepresentable = [
        a for a in args if any(ch.isspace() for ch in a) or '"' in a or "'" in a
    ]
    if unrepresentable:
        return _refused(
            "these arguments cannot be written into a self-contained gdb "
            f"script: {unrepresentable!r}. gdb's `set args` splits on "
            "whitespace and cannot quote, because the program is launched "
            "without a shell. Reproduce the bug with arguments that "
            "contain no spaces or quotes, or trigger it through a file the "
            "script can reference instead."
        )

    script_lines = list(gdb_commands)
    if args:
        script_lines.insert(0, "set args " + " ".join(args))

    # Verify before accepting. Observed on real runs: small models produce
    # scripts that error partway through, or that run the program with no
    # arguments so nothing fails. Both look like success to the model.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".gdb", prefix="verify-", delete=False
    ) as handle:
        handle.write("\n".join(script_lines) + "\n")
        probe = Path(handle.name)
    try:
        result, crash_signal = runners.run_gdb(
            binary, [], probe, cwd=_ROOT, cfg=_SANDBOX
        )
    finally:
        probe.unlink(missing_ok=True)

    combined = result.stdout + result.stderr

    # ORDER MATTERS. The no-input case was diagnosed by the generic "your script
    # errors" branch, because running with no arguments produces BOTH symptoms:
    # the program exits early, and every later command then fails with "The
    # program is not being run.". The specific message below -- the one that
    # names the actual cause -- never ran. Check for the cause before the
    # consequence.
    if "The program is not being run" in combined or "exited with code" in combined:
        return _refused(
            "the program exited on its own instead of crashing, so nothing "
            "was reproduced. The usual cause is missing input: pass the "
            "triggering arguments in the `args` parameter (the same list "
            "you gave run_gdb). "
            f"gdb said: {_head(combined)}"
        )
    if "Error in sourced command file" in combined:
        return _refused(
            "the script errors while running under gdb, so it is not a "
            "usable artifact. Fix the failing command and call again. "
            f"gdb said: {_head(combined)}"
        )
    # The acceptance test is "a fatal signal arrived", and deliberately does not
    # ask WHICH one: the planted bugs produce SIGSEGV, SIGFPE and SIGABRT, and
    # enumerating the acceptable signals would reject the next interesting bug
    # for no reason.
    #
    # AUDIT NOTE: the cost of that breadth is that a signal WE caused is
    # accepted too. A sandbox CPU limit expiring before gdb_timeout_s yields
    # "Program terminated with signal SIGKILL" (measured -- see
    # runners.parse_crash_signal), which reaches here as a non-None signal and
    # is written into the artifact header as the observed failure. Nothing
    # downstream distinguishes it.
    if crash_signal is None:
        return _refused(
            "the program did not fail when this script ran, so it does not "
            "reproduce a bug. Are these the arguments that trigger it? "
            "Fix the script and call again."
        )

    index = len(_SAVED) + 1
    slug = _slugify(title) or "bug"
    path = _OUTPUT_DIR / f"bug-{index:02d}-{slug}.gdb"

    header = [
        f"# Bug {index}: {title}",
        "#",
        *[f"# {line}" for line in description.strip().splitlines()],
        "#",
        f"# Observed failure: {crash_signal}",
        # Normalise the program path: the agent may pass "parser" or
        # "./parser", and blindly prefixing "./" produced "././parser" in a
        # real artifact. The header is the first thing a reader copies, so it
        # has to be a command that works.
        f"# Reproduce with: gdb --batch -nx -x {path.name} ./{program.lstrip('./')}",
        "",
    ]
    path.write_text("\n".join(header + script_lines) + "\n")

    _SAVED.append(
        {
            "index": index,
            "title": title,
            "description": description,
            "signal": crash_signal,
            "path": str(path),
            "program": program,
        }
    )
    log.info("saved reproduction %d (%s) -> %s", index, crash_signal, path)

    return {
        "saved": True,
        "path": str(path),
        "signal": crash_signal,
        "refused": None,
        "bugs_saved_so_far": index,
    }


def _slugify(text: str) -> str:
    """Filesystem-safe slug. The model chooses the title; we choose the path."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:48].strip("-")


def configure(
    root: Path,
    sandbox: SandboxConfig,
    output_dir: Path | None = None,
    max_bugs: int = -1,
) -> None:
    """Wire up module state. Separate from main() so tests can call it directly.

    `max_bugs` defaults to -1 (no cap) so a test that does not care about the
    limit is not silently capped at some number it never asked for.
    """
    global _ROOT, _SANDBOX, _OUTPUT_DIR, _MAX_BUGS
    _ROOT = root.resolve()
    _SANDBOX = sandbox
    _MAX_BUGS = max_bugs
    if not _ROOT.is_dir():
        raise RuntimeError(f"project root is not a directory: {_ROOT}")

    # Deliberately OUTSIDE the sandboxed project root: these are our artifacts,
    # not the target's, and they must not show up in list_files(). The agent
    # never supplies this path -- it is set here and only ever joined with a
    # slug we generate, so there is nothing to escape with.
    _OUTPUT_DIR = (output_dir or Path("out")).resolve()
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _SAVED.clear()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP server exposing C/C++ debugging tools"
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Folder to debug. Overrides agent.project_root from the config file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where reproduction scripts are written. Overrides agent.output_dir.",
    )
    args = parser.parse_args()

    cfg: Config = config.load(args.config)

    runners.require_tools()
    configure(
        args.project_root or cfg.agent.project_root,
        cfg.sandbox,
        args.output_dir or cfg.agent.output_dir,
        cfg.agent.max_bugs,
    )

    log.info("serving tools for project root %s", _ROOT)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
