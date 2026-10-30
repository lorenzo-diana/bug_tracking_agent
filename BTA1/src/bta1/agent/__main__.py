"""Entry point: wire everything together and write the .gdb script to disk.

Note what is NOT here: no reasoning, no tool logic, no policy. This module loads
config, builds the pieces, runs the loop, and saves the artifact. Keeping the
entry point boring is what lets loop.py stay readable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from .. import config
from . import loop, mcp_bridge, providers, tracing

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("bta1")


# A fenced block, optionally tagged (```gdb). DOTALL so it spans lines.
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# Tokens that make a line look like gdb rather than English. Used only to warn,
# never to filter -- silently dropping lines from the deliverable would be worse
# than handing over a file the user can see is wrong.
_GDB_HINTS = (
    "break",
    "run",
    "continue",
    "backtrace",
    "bt",
    "print",
    "info",
    "set ",
    "x/",
    "next",
    "step",
    "frame",
    "finish",
    "echo",
    "until",
    "delete",
)


def _looks_like_gdb(text: str) -> bool:
    """Does the first non-empty line look like gdb rather than English?

    One definition, two callers: the salvage path in _run decides whether to
    write an unverified script at all, and _extract_script decides whether to
    warn. The predicate was written out twice and could have drifted.
    """
    first = next((ln.strip().lower() for ln in text.splitlines() if ln.strip()), "")
    return first.startswith("#") or any(first.startswith(h) for h in _GDB_HINTS)


def _extract_script(text: str) -> str:
    """Pull the gdb script out of the model's final message.

    WHY A FENCE IS THE CONTRACT. An earlier version of the prompt asked for the
    bare script with "no markdown fences", and this function just stripped any
    fences that appeared anyway. Then a real run produced four paragraphs of
    explanation followed by the script -- the prose became line 1 of the .gdb
    file and gdb died immediately.

    Models are strongly inclined to wrap code in fences. Fighting that habit is
    harder than using it, so the prompt now REQUIRES a fenced block and we
    extract it. Aligning the contract with what the model already wants to do is
    more reliable than instructing it not to.

    The general lesson: tell the model what you want, then handle it not
    complying -- and prefer a format it will produce by accident.
    """
    stripped = text.strip()

    matches = _FENCE_RE.findall(stripped)
    if matches:
        # Last block: any preamble the model wrote is before it, and if it
        # showed an example mid-answer the final one is the deliverable.
        return matches[-1].strip()

    # No fence. Fall back to the whole message, but say so -- a .gdb file full
    # of prose should not arrive silently.
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and not _looks_like_gdb(stripped):
        log.warning(
            "the model did not use a fenced block and its answer does not "
            "look like a gdb script (first line: %r). Writing it out "
            "anyway -- inspect the file before trusting it.",
            lines[0][:80],
        )
    return stripped


async def _run(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)

    project_root = args.project_root or cfg.agent.project_root
    output_dir = (args.output_dir or cfg.agent.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.provider:
        cfg.llm.provider = args.provider

    tracer = tracing.setup(cfg.tracing)
    provider = providers.build(cfg.llm)
    log.info("provider=%s model=%s", cfg.llm.provider, provider.model)

    try:
        async with mcp_bridge.connect(project_root, args.config, output_dir) as bridge:
            log.info("tools: %s", ", ".join(sorted(bridge.tool_names)))

            # Always build before reasoning. gdb is useless without an
            # up-to-date binary carrying debug symbols, so compiling is a
            # precondition of the task rather than a decision the agent should
            # have to make. Doing it here also saves a turn -- on a local model
            # that is ~40 seconds -- and means a compile error surfaces as a
            # compile error instead of as confusing gdb output later.
            #
            # The `build` tool stays available: the agent may still want to
            # rebuild, and the pre-build goes through the same sandboxed path.
            log.info("building the project before handing it to the agent...")
            build_output = await bridge.call("build", {})
            if '"exit_code": 0' not in build_output:
                log.error(
                    "the project does not compile, so there is nothing to debug "
                    "under gdb:\n%s",
                    build_output,
                )
                return 1

            transcript = await loop.run(
                provider=provider,
                bridge=bridge,
                project_root=str(project_root),
                max_iterations=cfg.agent.max_iterations,
                tracer=tracer,
                evidence=build_output,
            )
    except BaseException as exc:  # noqa: BLE001 - re-raised unless it is ours
        # The loop runs INSIDE an anyio task group (the MCP stdio client), and a
        # task group wraps whatever escapes it in an ExceptionGroup. So
        # `except loop.IterationLimit` never matched: hitting the iteration cap
        # -- an ordinary, expected outcome -- came back wrapped and crashed the
        # process with a traceback instead of reporting cleanly.
        #
        # Unwrap the group rather than trusting the exception type.
        recovered = _unwrap_loop_failure(exc)
        if recovered is None:
            raise
        log.error("%s", recovered)
        _dump_transcript(getattr(recovered, "transcript", None), output_dir)
        return 1
    finally:
        tracing.shutdown()

    scripts = sorted(output_dir.glob("bug-*.gdb"))

    # Fallback for a model that ignores save_reproduction and just answers with
    # a script. Same principle as the fenced-block contract: tell the model what
    # you want, then handle it not complying -- a weak model that found a real
    # bug should not lose the result on a protocol technicality.
    if not scripts:
        salvaged = _extract_script(transcript.answer)
        if salvaged and _looks_like_gdb(salvaged):
            fallback = output_dir / "bug-01-unnamed.gdb"
            fallback.write_text(
                "# Salvaged from the agent's final message: it did not call\n"
                "# save_reproduction, so this script was NOT verified.\n"
                "# Run it yourself before trusting it.\n\n" + salvaged + "\n"
            )
            log.warning(
                "the agent never called save_reproduction; salvaged a script "
                "from its final message into %s (UNVERIFIED)",
                fallback,
            )
            scripts = [fallback]

    if not scripts:
        log.error(
            "no reproduction scripts were produced. The agent's final message was:\n%s",
            transcript.answer[:2000] or "(empty)",
        )
        return 1

    print(f"\n{'=' * 62}")
    print(f"BUGS REPRODUCED: {len(scripts)}")
    print("=" * 62)
    for path in scripts:
        title, signal = _script_header(path)
        print(f"\n  {path.name}")
        if title:
            print(f"    {title}")
        if signal:
            print(f"    fails with: {signal}")
        print(
            f"    verify: cd {project_root} && "
            f"gdb --batch -nx -x {path.resolve()} ./<program>"
        )

    print(
        f"\n{'-' * 62}"
        f"\niterations    : {transcript.iterations}"
        f"\ntool calls    : {transcript.tool_calls}"
        f"\nrefused saves : {transcript.refused_saves}"
        f"\nrejected gdb  : {transcript.rejected_scripts}"
        f"\ntokens in/out : {transcript.prompt_tokens}/{transcript.completion_tokens}"
        f"\noutput dir    : {output_dir}"
    )
    if transcript.answer:
        print(f"\nAgent summary:\n{transcript.answer}")
    return 0


def _dump_transcript(transcript: Any | None, output_dir: Path) -> None:
    """Write the conversation to disk when the agent fails.

    A failed run used to report one number -- "no final answer after 24
    iterations" -- and nothing about WHAT it did for those 24 turns. Diagnosing
    a loop then means re-running with ad-hoc logging, and the evidence from the
    failure you already have is gone. Three runs were spent that way.

    Written on failure only; a success already produces the scripts.
    """
    if transcript is None:
        return
    path = output_dir / "failed-transcript.json"
    try:
        path.write_text(
            json.dumps(getattr(transcript, "messages", []), indent=2, default=str)
        )
    except OSError as exc:  # pragma: no cover - diagnostics must never mask it
        log.warning("could not write the transcript: %s", exc)
        return
    log.error(
        "the full conversation is in %s -- read it before changing anything", path
    )


_LOOP_FAILURES = (loop.IterationLimit, loop.TruncatedResponse)


def _unwrap_loop_failure(exc: BaseException) -> BaseException | None:
    """Find one of our loop failures inside a possibly-nested ExceptionGroup.

    Checks `.exceptions` by duck typing rather than importing
    BaseExceptionGroup, so this works on 3.10 (where anyio supplies the
    backport) as well as 3.11+.
    """
    if isinstance(exc, _LOOP_FAILURES):
        return exc
    for nested in getattr(exc, "exceptions", ()) or ():
        found = _unwrap_loop_failure(nested)
        if found is not None:
            return found
    return None


def _script_header(path: Path) -> tuple[str | None, str | None]:
    """Read back the title and signal that save_reproduction wrote as comments."""
    title = signal = None
    for line in path.read_text().splitlines()[:12]:
        if line.startswith("# Bug ") and ":" in line:
            title = line.split(":", 1)[1].strip()
        elif line.startswith("# Observed failure:"):
            signal = line.split(":", 1)[1].strip()
    return title, signal


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find a bug in a C/C++ project and emit a gdb script that reproduces it"
        )
    )
    # Flags are OVERRIDES ONLY. config.yaml is the source of truth; nothing here
    # exists solely as a flag. See docs/architecture.md.
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for the generated .gdb scripts (one per bug found).",
    )
    parser.add_argument("--provider", default=None, help="override llm.provider")
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
