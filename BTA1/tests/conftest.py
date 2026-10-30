"""Shared fixtures.

Note there is no LLM anywhere in this suite. The entire tool surface is a plain
JSON-RPC server, so it is testable for free -- which means students can work on
a train and a model outage never blocks a lab. Protect that property.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bta1.config import SandboxConfig
from bta1.mcp_server import server

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "overflow"
MULTI = REPO / "examples" / "multi"


@pytest.fixture
def sandbox_cfg() -> SandboxConfig:
    """Tight limits so timeout/cap tests finish quickly."""
    return SandboxConfig(
        build_timeout_s=30,
        gdb_timeout_s=10,
        max_output_bytes=8192,
        max_read_lines=50,
        rlimit_cpu_s=15,
        rlimit_address_space_bytes=1 << 30,
        rlimit_processes=256,
        rlimit_file_size_bytes=1 << 20,
    )


@pytest.fixture
def project(tmp_path: Path, sandbox_cfg: SandboxConfig) -> Path:
    """A disposable copy of the overflow example, wired into the server.

    tmp_path lives under .pytest-tmp inside the repo (see pyproject.toml),
    NOT under /tmp -- which is mounted noexec in some environments, and this
    fixture compiles and *executes* a binary.
    """
    root = tmp_path / "proj"
    shutil.copytree(EXAMPLE, root)
    # Output dir sits OUTSIDE the sandboxed project root, exactly as in
    # production: saved scripts are our artifacts, not the target's.
    server.configure(root, sandbox_cfg, tmp_path / "out")
    return root


@pytest.fixture
def built_project(project: Path) -> Path:
    """The example, already compiled."""
    r = subprocess.run(["make"], cwd=project, capture_output=True, text=True)
    assert r.returncode == 0, f"fixture build failed: {r.stderr}"
    return project


@pytest.fixture
def multi_project(tmp_path: Path, sandbox_cfg: SandboxConfig) -> Path:
    """The three-bug example, compiled. For multi-bug behaviour."""
    root = tmp_path / "multi"
    shutil.copytree(MULTI, root)
    server.configure(root, sandbox_cfg, tmp_path / "out")
    r = subprocess.run(["make"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, f"fixture build failed: {r.stderr}"
    return root


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Where save_reproduction writes; matches what `project` configured."""
    return tmp_path / "out"
