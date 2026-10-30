"""Tests for config loading, validation, and environment interpolation.

Config is the one module every other module depends on, and a mistake here
surfaces as a confusing failure somewhere else entirely. It is also the file
students will edit first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bta1 import config
from bta1.config import AgentConfig, SandboxConfig

REPO = Path(__file__).resolve().parent.parent

MINIMAL = """
llm:
  provider: local
  providers:
    local:
      base_url: {base_url}
      api_key: {api_key}
      model: m
agent:
  project_root: examples/overflow
  output_dir: out
sandbox: {{}}
tracing: {{}}
"""


def _write(tmp_path: Path, **kw) -> Path:
    kw.setdefault("base_url", "http://127.0.0.1:1/v1")
    kw.setdefault("api_key", "k")
    path = tmp_path / "c.yaml"
    path.write_text(MINIMAL.format(**kw))
    return path


# ---------------------------------------------------------------------------
# The shipped config must always be valid
# ---------------------------------------------------------------------------


def test_shipped_config_loads() -> None:
    cfg = config.load(REPO / "config.yaml")
    assert cfg.llm.provider in cfg.llm.providers
    assert cfg.llm.selected().model


def test_shipped_config_output_cap_fits_a_modest_context() -> None:
    """max_output_bytes and the server's context length move together.

    At 65536 a single gdb result was ~16k tokens and would overflow a 16k
    window on its own. Pinned so nobody raises it without thinking.
    """
    cfg = config.load(REPO / "config.yaml")
    assert cfg.sandbox.max_output_bytes <= 16000


def test_shipped_config_cpu_limit_exceeds_gdb_timeout() -> None:
    """Otherwise the kernel SIGKILLs before our timeout fires, and the result is
    a confusing crash instead of a clean timeout."""
    cfg = config.load(REPO / "config.yaml")
    assert cfg.sandbox.rlimit_cpu_s >= cfg.sandbox.gdb_timeout_s


def test_shipped_config_address_space_above_measured_gdb_floor() -> None:
    """gdb itself aborts below ~512 MiB; the cap applies to the debugger too."""
    cfg = config.load(REPO / "config.yaml")
    assert cfg.sandbox.rlimit_address_space_bytes >= 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# Validation: a typo must fail at startup, naming the field
# ---------------------------------------------------------------------------


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path)
    path.write_text(path.read_text().replace("max_read_lines", "max_read_linez"))
    path.write_text(path.read_text() + "\nunexpected_section: 1\n")
    with pytest.raises(ValidationError, match="unexpected_section"):
        config.load(path)


def test_missing_file_raises_clearly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="config file not found"):
        config.load(tmp_path / "nope.yaml")


def test_provider_without_a_block_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path)
    path.write_text(path.read_text().replace("provider: local", "provider: ghost"))
    cfg = config.load(path)
    with pytest.raises(ValueError, match="no block under llm.providers"):
        cfg.llm.selected()


def test_defaults_fill_in_omitted_sections(tmp_path: Path) -> None:
    cfg = config.load(_write(tmp_path))
    assert cfg.sandbox.gdb_timeout_s > 0
    assert cfg.agent.max_iterations >= 1
    assert cfg.tracing.enabled is True


# ---------------------------------------------------------------------------
# ${VAR} and ${VAR:-fallback}
# ---------------------------------------------------------------------------


def test_env_var_is_substituted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_KEY", "s3cret")
    cfg = config.load(_write(tmp_path, api_key="${MY_KEY}"))
    assert cfg.llm.selected().api_key == "s3cret"


def test_missing_env_var_without_fallback_becomes_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Empty, not an error: providers.build() is where a missing key is caught,
    so only the provider you actually selected has to have one."""
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    cfg = config.load(_write(tmp_path, api_key="${ABSENT_KEY}"))
    assert cfg.llm.selected().api_key == ""


def test_fallback_is_used_when_the_var_is_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = config.load(
        _write(tmp_path, base_url="${LLM_BASE_URL:-http://172.17.0.1:8000/v1}")
    )
    assert cfg.llm.selected().base_url == "http://172.17.0.1:8000/v1"


def test_env_var_wins_over_the_fallback(tmp_path: Path, monkeypatch) -> None:
    """This is how run_from_scratch.sh points the agent at its own server
    without editing the committed config."""
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    cfg = config.load(
        _write(tmp_path, base_url="${LLM_BASE_URL:-http://172.17.0.1:8000/v1}")
    )
    assert cfg.llm.selected().base_url == "http://127.0.0.1:8000/v1"


def test_empty_env_var_falls_back(tmp_path: Path, monkeypatch) -> None:
    """An exported-but-blank variable should behave as unset, matching shell
    `${VAR:-default}` semantics -- otherwise a stray `export LLM_BASE_URL=`
    silently produces an unusable empty URL."""
    monkeypatch.setenv("LLM_BASE_URL", "")
    cfg = config.load(
        _write(tmp_path, base_url="${LLM_BASE_URL:-http://172.17.0.1:8000/v1}")
    )
    assert cfg.llm.selected().base_url == "http://172.17.0.1:8000/v1"


# ---------------------------------------------------------------------------
# requirements.txt must stay in step with the environment
# ---------------------------------------------------------------------------


def test_requirements_txt_has_no_self_reference() -> None:
    """`pip freeze` without --exclude-editable emits a local path or an
    `-e .` line, which breaks `pip install -r` on a fresh machine."""
    text = (REPO / "requirements.txt").read_text()
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines, "requirements.txt has no packages"
    assert not any(ln.startswith("-e") or "bta1" in ln or "bta1" in ln for ln in lines)
    assert all("==" in ln for ln in lines), "every dependency should be pinned"


def test_requirements_txt_covers_the_core_dependencies() -> None:
    names = {
        ln.split("==")[0].lower().replace("_", "-")
        for ln in (REPO / "requirements.txt").read_text().splitlines()
        if "==" in ln and not ln.startswith("#")
    }
    for required in ("mcp", "openai", "pydantic", "pyyaml"):
        assert required in names, f"{required} missing from requirements.txt"


# ---------------------------------------------------------------------------
# Tracing must not drown the run in noise
# ---------------------------------------------------------------------------


def test_tracing_skipped_when_no_collector_is_listening() -> None:
    """Graceful degradation was not enough.

    With nothing on the endpoint, the OTLP exporter retried every batch and
    logged a WARNING each time -- hundreds of lines that buried a demo run's
    real output. A student reads that as breakage. So we probe the port once
    and skip instrumenting entirely: one honest line instead of a wall.
    """
    from bta1.agent import tracing
    from bta1.config import TracingConfig

    cfg = TracingConfig(enabled=True, endpoint="http://127.0.0.1:59999/v1/traces")
    assert tracing.setup(cfg) is None


def test_endpoint_reachability_probe() -> None:
    from bta1.agent.tracing import _endpoint_reachable

    assert _endpoint_reachable("http://127.0.0.1:59999/v1/traces") is False
    # A port we know is open: the probe must not report everything dead.
    import socket

    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
        assert _endpoint_reachable(f"http://127.0.0.1:{port}/v1/traces") is True


def test_tracing_disabled_by_config_returns_none() -> None:
    from bta1.agent import tracing
    from bta1.config import TracingConfig

    assert tracing.setup(TracingConfig(enabled=False)) is None


# ---------------------------------------------------------------------------
# Cross-field validation
# ---------------------------------------------------------------------------


def test_cpu_limit_below_the_gdb_timeout_is_rejected() -> None:
    """Guards a false positive in the one thing this project detects.

    If rlimit_cpu_s expires before gdb_timeout_s, the kernel SIGKILLs the
    inferior and gdb prints "Program terminated with signal SIGKILL" --
    measured, with a `for(;;);` program under `prlimit --cpu=1`. That reaches
    runners.parse_crash_signal as an ordinary fatal signal, and
    save_reproduction accepts any non-None signal, so a resource limit would be
    written into an artifact as the reproduced bug.

    Each field was validated independently before, so nothing compared them.
    """
    with pytest.raises(ValidationError, match="rlimit_cpu_s"):
        SandboxConfig(gdb_timeout_s=30, rlimit_cpu_s=29)


def test_cpu_limit_equal_to_the_timeout_is_allowed() -> None:
    """The boundary is inclusive: prlimit --cpu counts CPU seconds and the
    timeout is wall-clock, and CPU time cannot exceed wall time for a
    single-threaded inferior, so equal values still let the timeout win."""
    assert SandboxConfig(gdb_timeout_s=30, rlimit_cpu_s=30).rlimit_cpu_s == 30


def test_build_timeout_is_deliberately_outside_the_invariant() -> None:
    """The shipped config has build_timeout_s 60 > rlimit_cpu_s 35 on purpose:
    a build spends most of its wall time waiting on subprocesses, and a build
    killed by the limit surfaces as a compile failure, which cannot be mistaken
    for a reproduced bug."""
    assert SandboxConfig(build_timeout_s=60, gdb_timeout_s=30, rlimit_cpu_s=35)


@pytest.mark.parametrize("value", [-1, 1, 10, 1000])
def test_max_bugs_accepts_a_positive_cap_or_minus_one(value: int) -> None:
    """-1 is the documented way to say "no cap"."""
    assert (
        AgentConfig(
            project_root=Path("."), output_dir=Path("out"), max_bugs=value
        ).max_bugs
        == value
    )


@pytest.mark.parametrize("value", [0, -2, -100])
def test_max_bugs_rejects_zero_and_other_negatives(value: int) -> None:
    """Zero would make every save refuse, so the agent would burn its whole
    iteration budget being told no. Only -1 means unlimited."""
    with pytest.raises(ValidationError, match="max_bugs"):
        AgentConfig(project_root=Path("."), output_dir=Path("out"), max_bugs=value)
