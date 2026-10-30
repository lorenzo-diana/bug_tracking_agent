"""Load and validate config.yaml.

Why a pydantic model instead of the dict that `yaml.safe_load` hands back: a
typo in config.yaml should fail at startup with a message naming the bad field,
not surface as a KeyError twelve tool calls into an agent run. Validating at the
boundary is the same principle the MCP server applies to tool arguments.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ${VAR} and ${VAR:-fallback}. The fallback form lets config.yaml ship a
# sensible default that a script or a student can override from the environment
# without editing the file -- see run_from_scratch.sh, which starts its own
# llama-server on localhost and overrides LLM_BASE_URL accordingly.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Strict(BaseModel):
    """Reject unknown keys, so a misspelled option is an error, not a no-op."""

    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_Strict):
    base_url: str | None = None
    api_key: str = ""
    model: str


class LLMConfig(_Strict):
    provider: str
    temperature: float = 0.2
    max_tokens: int = 2048
    providers: dict[str, ProviderConfig]

    def selected(self) -> ProviderConfig:
        try:
            return self.providers[self.provider]
        except KeyError:
            known = ", ".join(sorted(self.providers))
            raise ValueError(
                f"llm.provider is {self.provider!r}, which has no block under "
                f"llm.providers (known: {known})"
            ) from None


class AgentConfig(_Strict):
    project_root: Path
    # A DIRECTORY, not a file: a project can contain several bugs and the agent
    # writes one .gdb script per bug it reproduces.
    output_dir: Path
    max_iterations: int = Field(default=24, ge=1)
    # Hard cap on accepted reproductions, enforced in the MCP server by
    # save_reproduction (see _MAX_BUGS there) rather than in the loop: the
    # server owns the artifacts, so it is the only place the limit cannot be
    # talked out of.
    #
    # -1 means NO CAP. Zero is rejected, because a configuration that cannot
    # accept any reproduction is never what someone meant -- it would make the
    # agent run to its iteration limit being refused every time.
    max_bugs: int = 10

    @field_validator("max_bugs")
    @classmethod
    def _cap_must_be_positive_or_unlimited(cls, value: int) -> int:
        if value == -1 or value >= 1:
            return value
        raise ValueError(f"agent.max_bugs must be >= 1, or -1 for no cap (got {value})")


class SandboxConfig(_Strict):
    """Limits applied to every make and gdb subprocess, in runners.py.

    `rlimit_cpu_s` must stay >= `gdb_timeout_s`, and the validator below
    enforces it. Each field is otherwise validated independently, so nothing
    compared them and an inverted pair was accepted silently.

    It matters more than an ordinary misconfiguration because of what the
    failure looks like. If the CPU limit expires first the kernel kills the
    inferior, and gdb prints "Program terminated with signal SIGKILL" --
    measured, with a `for(;;);` program under `prlimit --cpu=1`. That reaches
    runners.parse_crash_signal as an ordinary fatal signal, and
    save_reproduction accepts any non-None signal, so the limit would be
    recorded in an artifact as the bug: a false positive in exactly the thing
    this project exists to detect.

    The timeout path does not have this problem, for a structural reason:
    `gdb_timeout_s` is enforced by killing the whole session (runners.run_guarded
    -> kill_session), which takes gdb down with the inferior. gdb is the thing
    that prints the "Program terminated with signal ..." line, so a timeout
    cannot manufacture one. Any signal still parsed after a timeout came from
    output gdb had already produced -- i.e. a real crash, followed by a later
    command that hung.

    Note the units differ, which is why the comparison is a safe heuristic
    rather than an exact rule: prlimit --cpu counts CPU seconds, the timeouts
    are wall-clock. CPU time can only be <= wall time for a single-threaded
    inferior, so cpu >= timeout guarantees the timeout fires first.

    `build_timeout_s` is deliberately NOT part of the invariant, even though
    make runs under the same limits. The shipped config has build_timeout_s 60
    > rlimit_cpu_s 35 on purpose: a build spends most of its wall time waiting
    on subprocesses rather than burning CPU in one process. And a build killed
    by the limit surfaces as a compile failure, which is misleading but cannot
    be mistaken for a reproduced bug.
    """

    build_timeout_s: int = Field(default=60, ge=1)
    gdb_timeout_s: int = Field(default=30, ge=1)
    max_output_bytes: int = Field(default=65536, ge=1024)
    max_read_lines: int = Field(default=400, ge=1)
    rlimit_cpu_s: int = Field(default=35, ge=1)
    rlimit_address_space_bytes: int = Field(default=1 << 30, ge=1 << 29)
    rlimit_processes: int = Field(default=256, ge=1)
    rlimit_file_size_bytes: int = Field(default=10 << 20, ge=1024)

    @model_validator(mode="after")
    def _cpu_limit_must_outlast_the_gdb_timeout(self) -> SandboxConfig:
        if self.rlimit_cpu_s < self.gdb_timeout_s:
            raise ValueError(
                f"sandbox.rlimit_cpu_s ({self.rlimit_cpu_s}) is below "
                f"sandbox.gdb_timeout_s ({self.gdb_timeout_s}). The kernel "
                "would SIGKILL the inferior before the timeout fires, and gdb "
                "reports that as 'Program terminated with signal SIGKILL' -- "
                "which this server cannot distinguish from a real crash, so a "
                "resource limit would be saved as a reproduced bug. Raise "
                "rlimit_cpu_s to at least gdb_timeout_s."
            )
        return self


class TracingConfig(_Strict):
    enabled: bool = True
    endpoint: str = "http://127.0.0.1:6006/v1/traces"
    service_name: str = "bta1"


class Config(_Strict):
    llm: LLMConfig
    agent: AgentConfig
    sandbox: SandboxConfig
    tracing: TracingConfig


def _substitute(match: re.Match[str]) -> str:
    """Resolve one ${VAR} / ${VAR:-fallback} occurrence.

    Note the `or`, which is load-bearing: a variable that is SET BUT EMPTY takes
    the fallback, exactly as if it were unset. This matches POSIX `${VAR:-word}`,
    which substitutes when the variable is unset *or null* -- so the behaviour
    agrees with the shell syntax the pattern borrows, and `LLM_BASE_URL=` from a
    script that computed nothing useful does not silently configure an empty
    base_url.

    The form WITHOUT a fallback, `${VAR}`, resolves to "" when unset rather than
    raising. That is deliberate and documented on _expand_env: a missing key
    only matters for the provider actually selected, and providers.build() is
    what enforces it.
    """
    name, fallback = match.group(1), match.group(2)
    return os.environ.get(name) or (fallback if fallback is not None else "")


def _expand_env(value: Any) -> Any:
    """Replace ${VAR} and ${VAR:-fallback} with environment values, recursively.

    A missing variable with no fallback becomes an empty string rather than an
    error: local servers have no real secret, and we only care that a key is
    present for the provider actually selected. providers.build() enforces that.
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_substitute, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


DEFAULT_CONFIG_PATH = Path("config.yaml")


def load(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(_expand_env(raw))
