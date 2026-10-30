"""Subprocess launching. Policy lives in sandbox.py; this module only runs things.

DO NOT USE preexec_fn
---------------------
`preexec_fn` is the obvious API for setting resource limits and process groups,
and it is the wrong one. It runs arbitrary *Python* between fork() and exec().

The fork is the trigger; multiple threads are the precondition. POSIX fork() in
a multi-threaded process gives the child only the forking thread, while still
inheriting the *state* of every lock. A lock held by another thread at fork time
stays locked in the child with no thread alive to release it -- so any Python
operation in the child that touches an interpreter lock (an allocation, a lazy
import, a logging call) deadlocks it before exec() ever runs. No exception, no
traceback: a hung process, nondeterministic on fork timing.

This is not hypothetical here. MCPServer (FastMCP in mcp 1.x) is async, so a
blocking tool function
runs on a worker thread, which means the parent IS multi-threaded at fork time.

So we run no Python in the child:

    need                        mechanism
    --------------------------  ------------------------------------------------
    a session to kill           Popen(start_new_session=True) -- setsid() in C
    resource limits             prlimit(1) wrapper -- sets limits, then exec()s

Both push the work into C or into a separate binary.

The safety claim here is *structural* -- no Python runs between fork and exec --
and that is what carries it. Empirically, 200 concurrent launches from a
4-thread parent completed with zero hangs, but note the limit of that evidence:
the deadlock is nondeterministic, so absence of a hang in N trials is not proof.

PLATFORM: Linux only. prlimit(1) is util-linux and /proc is Linux. gdb is the
subject matter, so this is not a meaningful restriction.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import SandboxConfig
from .sandbox import cap_output, kill_session

# Minimal environment. The agent must not be able to influence a build through
# inherited variables (CFLAGS, LD_PRELOAD, MAKEFLAGS...), and a scrubbed env
# also makes runs reproducible across machines.
_SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "TERM": "dumb",
    "HOME": "/nonexistent",
}

# "Program received signal SIGSEGV, Segmentation fault."
# "Program terminated with signal SIGABRT, Aborted."
_SIGNAL_RE = re.compile(r"Program (?:received|terminated with) signal (SIG[A-Z0-9]+)")


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _prlimit_prefix(cfg: SandboxConfig) -> list[str]:
    return [
        "prlimit",
        f"--cpu={cfg.rlimit_cpu_s}",
        f"--as={cfg.rlimit_address_space_bytes}",
        f"--nproc={cfg.rlimit_processes}",
        f"--fsize={cfg.rlimit_file_size_bytes}",
        "--",
    ]


def require_tools() -> None:
    """Fail loudly at startup if the platform tools are missing."""
    for tool in ("prlimit", "gdb", "make"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"required tool not found on PATH: {tool!r}. "
                "This project targets Linux; prlimit comes from util-linux."
            )


def run_guarded(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    cfg: SandboxConfig,
) -> RunResult:
    """Run `argv` under prlimit in its own session, with a wall-clock timeout.

    On timeout the WHOLE SESSION is killed, not just the direct child -- see
    sandbox.session_pids() for why that distinction is load-bearing.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is built from validated inputs
        _prlimit_prefix(cfg) + argv,
        cwd=str(cwd),
        env=_SAFE_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        # setsid() in C. Makes this child a session leader, so its session id
        # equals its pid and everything it spawns inherits that session.
        start_new_session=True,
    )

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        # proc.pid == the session id, because start_new_session=True.
        kill_session(proc.pid)
        out, err = proc.communicate()  # collect whatever was produced
        err = (err or "") + f"\n[timed out after {timeout_s}s; session killed]"

    return RunResult(
        exit_code=proc.returncode,
        stdout=cap_output(out or "", cfg.max_output_bytes),
        stderr=cap_output(err or "", cfg.max_output_bytes),
        timed_out=timed_out,
    )


def run_make(target: str, *, cwd: Path, cfg: SandboxConfig) -> RunResult:
    """Invoke make. Never a shell -- argv only, so there is nothing to inject into."""
    argv = ["make"]
    if target:
        argv.append(target)
    return run_guarded(argv, cwd=cwd, timeout_s=cfg.build_timeout_s, cfg=cfg)


def run_gdb(
    program: Path,
    program_args: list[str],
    script_path: Path,
    *,
    cwd: Path,
    cfg: SandboxConfig,
) -> tuple[RunResult, str | None]:
    """Invoke gdb in batch mode on `script_path`. Returns (result, crash_signal).

    `-nx` skips the user's ~/.gdbinit. Without it, behaviour depends on whoever's
    machine this runs on, which defeats the entire point of a reproduction
    script.
    """
    argv = [
        "gdb",
        "--batch",
        "-nx",
        # SECURITY, and it is load-bearing. gdb starts the inferior through
        # $SHELL by default, and it passes the `run`/`set args` arguments
        # through that shell. The command denylist only ever inspected command
        # *names*, so this was a complete bypass of it:
        #
        #     run $(touch /tmp/pwned)      -> executed
        #     run `touch /tmp/pwned`       -> executed
        #     set args ok > /etc/anything  -> wrote outside the project root
        #
        # All four layers that existed at the time were defeated by one line of
        # generated gdb (this one is the fifth, added because of it),
        # reachable from a prompt injection. Turning off startup-with-shell
        # closes every variant (verified), and plain command-line arguments
        # still work -- the shell is only needed for redirection, globbing and
        # expansion, none of which a reproduction script should rely on.
        #
        # Applied HERE, at the launch level, rather than prepended to the script
        # text: every caller gets it, and no caller can forget it. It is sent
        # before -x so the script cannot run first.
        #
        # `set startup-with-shell` is itself on the denylist, so a generated
        # script cannot turn it back on.
        "-ex",
        "set startup-with-shell off",
        "-x",
        str(script_path),
        "--args",
        str(program),
        *program_args,
    ]
    result = run_guarded(argv, cwd=cwd, timeout_s=cfg.gdb_timeout_s, cfg=cfg)
    return result, parse_crash_signal(result.stdout + result.stderr)


def parse_crash_signal(output: str) -> str | None:
    """Extract the inferior's fatal signal from gdb's output, e.g. "SIGSEGV".

    WHY THIS EXISTS. Verified: `gdb --batch` exits 0 on a run where the target
    took SIGSEGV. gdb reports on its *own* success, not the inferior's. So the
    crash has to be read out of the output text, and exit_code must never be
    used to decide whether the bug reproduced.

    Left unhandled, the agent sees exit_code 0, concludes its reproduction
    attempt failed when it actually succeeded, and wanders off down a dead end.

    AUDIT NOTE -- this returns "a fatal signal happened", NOT "the program has a
    bug". The two differ when WE killed the inferior. Measured, with a
    `for(;;);` program under `prlimit --cpu=1`:

        gdb prints: "Program terminated with signal SIGKILL, Killed."
        this function returns: 'SIGKILL'

    The pattern below matches "terminated with" as well as "received", so that
    SIGKILL is indistinguishable here from a genuine crash. save_reproduction
    accepts any non-None signal (see its `crash_signal is None` gate), so a
    sandbox resource limit can be written into an artifact as
    "# Observed failure: SIGKILL".

    This is reachable through misconfiguration rather than through anything the
    agent does: it needs sandbox.rlimit_cpu_s to expire before
    sandbox.gdb_timeout_s, a relationship config.yaml documents but nothing
    validates. See the AUDIT NOTE in config.SandboxConfig.
    """
    match = _SIGNAL_RE.search(output)
    return match.group(1) if match else None
