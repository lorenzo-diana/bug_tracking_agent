"""Containment for everything the agent can make this machine do.

THREAT MODEL
------------
Adversary:    the LLM. It may emit a gdb script containing `shell`, `python`, or
              a path escape -- through confusion, through prompt injection from a
              crafted comment in the target source, or through plain generation
              noise. It is NOT a determined human attacker probing for a bypass.
Trusted:      the operator and config.yaml.
Semi-trusted: the target C/C++ source. It is the student's own code here, but
              read_source() returns it verbatim into the model's context, which
              makes a crafted comment an injection vector. This is the trust
              assumption most likely to break in practice.
Blast radius: commands run with this process's privileges, in the operator's own
              development environment -- where they already run gdb by hand.
Inverts when: run as a *service* over untrusted, user-submitted source. Then the
              adversary is a human choosing the input, and process-level controls
              are nowhere near sufficient; you need OS-level isolation
              (container, seccomp, a dedicated unprivileged user).

The generalizable lesson: sandboxing strength is a function of the threat model,
not a fixed property you either have or lack.

FIVE LAYERS, AND WHAT EACH DOES NOT STOP
----------------------------------------
| Layer                       | Stops                          | Does NOT stop        |
|-----------------------------|--------------------------------|----------------------|
| Path confinement            | reads/writes outside the root   | anything not path-   |
|                             |                                 | based                |
| gdb command denylist        | the obvious escapes (shell,     | creative aliasing;   |
|                             | python, pipe, compile, ...)     | commands we did not  |
|                             |                                 | anticipate           |
| startup-with-shell off      | `run $(cmd)`, backticks and     | nothing further, but |
| (applied at launch, see     | `run > /path` reaching a shell  | it must STAY off --  |
|  runners.run_gdb)           | -- this was a COMPLETE bypass   | hence `set startup-  |
|                             | of the denylist until fixed     | with-shell` is denied|
| Resource limits (prlimit)   | fork bombs, memory exhaustion,  | one destructive      |
|                             | runaway CPU, huge writes --     | command that         |
|                             | EVEN AFTER a denylist bypass    | completes quickly    |
| Timeout + kill by session   | hangs; gdb, the inferior, and   | an inferior that     |
|                             | grandchildren it forked         | calls setsid() to    |
|                             |                                 | escape the session   |

The third layer is a lesson in itself: four documented controls were all
bypassable by `run $(touch /tmp/pwned)`, because every one of them inspected
command *names* and none inspected `run`'s *arguments*. A control you have not
tried to break is a control you do not know the strength of.

The honest "does not stop" column matters more pedagogically than any single
control. Note also what the layering buys: a successful prompt injection
degrades from *arbitrary code execution* to *a wasted run and a bad script*.
Defense in depth is not an abstraction here -- it is the mechanism that turns a
critical vulnerability into an annoyance.

No Docker, no root, no privileged operations: the design works identically
inside or outside a container.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError


class SandboxError(ToolError):
    """A tool argument violated the sandbox policy.

    Subclasses the MCP SDK's `ToolError` deliberately, and it matters more than
    it looks. The SDK treats a plain `Exception` from a tool as a *crash*: it
    logs the detail server-side and sends the model only
    "Error executing tool <name>". `ToolError` means "a failure you anticipated",
    and the SDK passes your message through to the model instead.

    That distinction is the difference between the agent reading
    "no such path in project: 'src'" -- and fixing itself on the next turn --
    and reading "Error executing tool list_files", which tells it nothing. We
    found this the expensive way: a real run burned turns because a guessed
    directory name produced an opaque error.

    Everything that reaches the model should be actionable. A sandbox refusal is
    not a bug, it is feedback.
    """


# --------------------------------------------------------------------------
# Layer 1: path confinement
# --------------------------------------------------------------------------


def safe_path(root: Path, candidate: str, *, must_exist: bool = True) -> Path:
    """Resolve `candidate` inside `root`, or raise SandboxError.

    ORDER MATTERS: resolve first, compare second. Comparing the string before
    resolving it is the classic symlink-escape bug -- "src/link" can look
    harmless and point at /etc. .resolve() follows symlinks, so the comparison
    happens on the real destination.
    """
    if not candidate or candidate.strip() == "":
        raise SandboxError("path must not be empty")

    if Path(candidate).is_absolute():
        # Absolute paths are rejected outright rather than silently reinterpreted:
        # the agent should think in project-relative terms, and a refusal teaches
        # it that faster than a surprise.
        raise SandboxError(
            f"absolute paths are not allowed: {candidate!r} "
            "(use a path relative to the project root)"
        )

    root = root.resolve()
    resolved = (root / candidate).resolve()

    if resolved != root and root not in resolved.parents:
        raise SandboxError(
            f"path escapes the project root: {candidate!r} resolves outside {root}"
        )

    if must_exist and not resolved.exists():
        raise SandboxError(f"no such path in project: {candidate!r}")

    return resolved


# --------------------------------------------------------------------------
# Layer 2: the gdb command denylist
# --------------------------------------------------------------------------

# Commands that can execute code, write arbitrary files, or retarget gdb outside
# the sandbox. A denylist is the WEAKER form of this control -- see the module
# docstring. It is backed by resource limits and the session kill precisely
# because it cannot be complete.
DENIED_COMMANDS = frozenset(
    {
        # arbitrary command / code execution
        "shell",
        "python",
        "python-interactive",
        "guile",
        "guile-repl",
        "compile",  # gdb's `compile code` runs code in the inferior
        "pipe",
        # loads unvalidated commands from elsewhere
        "source",
        "jit-reader-load",
        # retargets gdb outside the sandbox
        "file",
        "exec-file",
        "symbol-file",
        "add-symbol-file",
        "core-file",
        # arbitrary file writes
        "dump",
        "append",
        # can smuggle a denied command into a body we never inspect
        "define",
        "document",
        "eval",  # string-constructs a command, defeating line matching
    }
)

# Explicit aliases and punctuation forms that prefix-matching would miss.
DENIED_TOKENS = frozenset({"!", "|", "pi", "py", "sh"})

# `set` is legitimate and necessary (`set args ...`), so it is filtered on its
# subcommand rather than banned.
#
# `startup-with-shell` is here because runners.run_gdb turns it OFF at launch to
# stop `run $(cmd)` and `run > /path` escaping through $SHELL. A generated script
# that turned it back on would reopen a full bypass of this entire denylist, so
# the command is refused outright.
DENIED_SET_SUBCOMMANDS = frozenset({"logging", "trace-commands", "startup-with-shell"})


def check_gdb_commands(lines: list[str]) -> None:
    """Raise SandboxError if any line is denied.

    gdb accepts unique prefixes of command names, so a bare exact-match denylist
    would be trivially bypassed by writing `shel` or `pyth`. We therefore reject
    any token of 2+ characters that is a prefix of a denied command.

    This is deliberately conservative and will reject some legitimate
    abbreviations (`do` for `down`, because it prefixes `document`). That is an
    acceptable trade: the tool description tells the agent to write commands out
    in full, and a false rejection costs one retry while a false acceptance
    costs a shell.
    """
    for lineno, raw in enumerate(lines, start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        tokens = line.split()
        head = tokens[0].lower()

        if head in DENIED_TOKENS:
            raise SandboxError(
                f"gdb command rejected on line {lineno}: {raw.strip()!r} "
                f"-- {head!r} can execute arbitrary commands"
            )

        # Punctuation escapes can be glued to their argument: `!ls`, `|cat`.
        if head[0] in "!|":
            raise SandboxError(
                f"gdb command rejected on line {lineno}: {raw.strip()!r} "
                "-- shell escape"
            )

        if len(head) >= 2:
            for denied in DENIED_COMMANDS:
                if denied.startswith(head):
                    raise SandboxError(
                        f"gdb command rejected on line {lineno}: {raw.strip()!r} "
                        f"-- matches denied command {denied!r}. "
                        "Write commands out in full and avoid shell/python escapes."
                    )

        if head == "set" and len(tokens) >= 2:
            sub = tokens[1].lower()
            for denied in DENIED_SET_SUBCOMMANDS:
                if denied.startswith(sub) and len(sub) >= 2:
                    raise SandboxError(
                        f"gdb command rejected on line {lineno}: {raw.strip()!r} "
                        f"-- `set {denied}` can write arbitrary files"
                    )


# There is deliberately no "Layer 3" or "Layer 4" section in this file.
# `startup-with-shell off` and prlimit are both LAUNCH concerns -- they have to
# be applied when the process is started, not when its script is inspected --
# so they live in runners.py, inside run_gdb and _prlimit_prefix. The numbering
# follows the five-layer table in this module's docstring.
#
# --------------------------------------------------------------------------
# Layer 5: the kill scope
# --------------------------------------------------------------------------


def session_pids(sid: int) -> list[int]:
    """Every pid in session `sid`, read straight from /proc.

    WHY SESSIONS AND NOT PROCESS GROUPS. Verified experimentally: gdb puts the
    inferior in its OWN process group, so killpg(gdb_pgid) never reaches it. A
    ptraced inferior usually dies anyway because gdb sets PTRACE_O_EXITKILL --
    but that covers only the traced process. An inferior that calls fork()
    leaves a grandchild that is neither ptraced nor in gdb's group, and killpg
    leaks it. Measured layout:

        session 1058
        |- 1058 gdb     pgid 1058   <- killpg(1058) hits only this
        |- 1068 forker  pgid 1068   <- inferior: different group
        `- 1071 forker  pgid 1068   <- grandchild it forked: LEAKED

    Popen(start_new_session=True) makes our child a session leader, so
    sid == child.pid, and session membership is inherited by everything it
    spawns. Killing the session is the correct scope.

    /proc/<pid>/stat field 6 is the session id. The comm field (2) can itself
    contain spaces and parentheses, so we split after the LAST ')'.
    """
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue  # the process exited while we were looking at it
        close = stat.rfind(")")
        if close == -1:
            continue
        fields = stat[close + 2 :].split()
        if len(fields) < 4:
            continue
        try:
            if int(fields[3]) == sid:
                found.append(int(entry))
        except ValueError:
            continue
    return found


def kill_session(sid: int, sig: int = signal.SIGKILL) -> int:
    """SIGKILL every process in the session. Returns how many were signalled."""
    killed = 0
    for pid in session_pids(sid):
        if pid == os.getpid():
            continue  # never signal ourselves
        try:
            os.kill(pid, sig)
            killed += 1
        except ProcessLookupError:
            pass
    return killed


# --------------------------------------------------------------------------
# Output capping -- not a security control, a context-window control.
# --------------------------------------------------------------------------


def cap_output(text: str, limit: int) -> str:
    """Truncate to `limit` bytes with an explicit marker.

    The marker matters: silently handing back a partial picture lets the agent
    reason confidently from truncated evidence. Telling it information was
    withheld lets it narrow its next query instead.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    dropped = len(encoded) - limit
    kept = encoded[:limit].decode("utf-8", errors="ignore")
    return f"{kept}\n...[truncated {dropped} bytes]"
