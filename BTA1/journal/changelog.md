# Changelog

How this project got to its current shape, newest first. **Nothing here is
needed to understand or use BTA1** — that is what [`docs/`](../docs/) is for.
This is the record of what changed and why, kept because the *mistakes* are
teaching material in their own right: several entries below are wrong turns,
and the reasoning that corrected them is the point.

---

## Design and implementation changes

- **2026-10-30** — **OPEN: an unexplained save/refuse loop on
  `examples/multi`.** Recorded while the evidence is fresh, because it is not
  diagnosed and the next person to look at it should not start from zero.

  **What was observed.** A run on `examples/multi` spent 46 minutes and 7 tool
  calls and produced no scripts:

  ```
  1 list_files
  2 read_source bugs.c
  3 run_gdb  copy AAAA…      <- bug 1
  4 run_gdb  scale 0          <- bug 2
  5 run_gdb  name 2           <- bug 3
  6 save_reproduction "stack buffer overflow in copy_label"
  7 save_reproduction "stack buffer overflow in copy_label"   <- identical retry
  ```

  All three bugs were deduced correctly. The agent then got stuck re-saving the
  first one. **The refusal reason was never captured**, so the cause is
  unknown.

  **Why it could not be seen** — three compounding gaps, the first two now
  fixed:

  1. `_refused()` returned silently. The sibling agent's equivalent logged a
     warning and BTA1's never did, so a refusal existed only inside the model's
     context. **Fixed:** `_refused()` now logs the reason, with a test
     asserting the reason itself appears in the line and not merely the fact of
     a refusal.
  2. `_summarise()` caps tool-call arguments at 160 characters, so a
     `save_reproduction` call logs its title and part of its description and
     never the script. **Documented** as a deliberate trade-off rather than
     changed: raising it buries every other line.
  3. The transcript dump only fires on `IterationLimit`. The run was killed by
     a wall-clock `timeout` wrapper, which produces no dump — so the one
     artifact designed to explain a loop was never written. **Not a code
     defect:** the 1800s ceiling was chosen when launching the batch, and it
     guaranteed the run could never reach the state that explains itself.

  **What is known.** The same bug saved successfully in an earlier batch run on
  the same example, so the refusal is not inherent to the bug — it is something
  about that particular script. Plausible candidates, none verified: a
  `continue`/`exit` sequence that errors under `--batch`, or a breakpoint that
  bound but never fired. **Do not trust this list**; read the refusal.

  **How to pick it up.** Run `examples/multi` with no wall-clock wrapper, or a
  ceiling comfortably above 24 iterations at roughly 4 minutes per tool call
  (so >= 2 hours on CPU). Then either it completes, or `IterationLimit` fires
  and writes `output/multi/failed-transcript.json` with every tool result the
  agent saw. The refusal reasons will also be in the log as they happen, which
  was not true when this was observed.

  **Status of the example.** The best complete evidence for `multi` remains the
  first batch run: 2 of 3 scripts saved, both independently verified
  (`SIGSEGV`, `SIGFPE`), killed by the same timeout with the third bug already
  correctly deduced. `multi` therefore has **no fully completed run on
  record**, while `overflow` (1/1) and `parser` (3/3) do.

  *Lesson, and it is the second time this session:* an agent failure you cannot
  read is a tooling defect, not a model defect. Both times the fix was
  observability, and both times the cost of not having it was measured in
  40-minute runs.

- **2026-10-30** — **Config knobs that lied, and one that could corrupt a
  result.** Two findings from an audit pass over `src/`, both now fixed:

  * `agent.max_bugs` was validated, stored, documented in `config.yaml` as
    stopping "a confused agent filling the disk" — and **never read by any
    code**. The declaration was its only hit outside the YAML. It is now
    enforced, with `-1` meaning no cap and `0` rejected at startup (a config
    that can never accept a reproduction would make the agent spend its whole
    iteration budget being refused). Enforcement lives in the **MCP server**
    rather than the loop: that process owns the output directory, so it is the
    only place the cap cannot be talked past. The refusal tells the agent to
    stop and summarise — the one refusal here where retrying is wrong — and it
    is checked before path validation and before gdb runs, since once the cap
    is reached the answer cannot change.
  * `sandbox.rlimit_cpu_s >= sandbox.gdb_timeout_s` was documented in
    `config.yaml` and **checked nowhere**; pydantic validated each field
    independently. Below that threshold the kernel SIGKILLs the debuggee before
    our timeout fires, gdb prints `Program terminated with signal SIGKILL`, and
    `parse_crash_signal` matches `terminated with` as well as `received` — so
    `save_reproduction`, which accepts any non-`None` signal, would write
    `# Observed failure: SIGKILL` into an artifact. A cross-field validator now
    refuses to load such a config, naming both fields and the consequence.

  *Lesson for the course:* a documented invariant with no validator is a
  comment, and the second one was a false positive waiting to happen in the one
  thing this project exists to detect. Note also which direction the sandbox
  failed in: the usual worry is a control costing performance, while this one
  could have cost correctness.

- **2026-10-30** — Adding that validator **broke two existing tests**, which is
  the useful part. `test_rlimit_cpu_is_enforced` and
  `test_rlimit_address_space_is_enforced` hand-built a `SandboxConfig` with a
  tiny `rlimit_cpu_s` against the default 30-second timeout — exactly the
  inverted pair now forbidden — so they failed at construction, before any
  assertion. Neither needed an assertion changed: the CPU test *wants* the
  kernel limit to win, so `gdb_timeout_s=1` alongside `rlimit_cpu_s=1` states
  its intent more precisely than relying on the old default. Only test code
  violated the rule; no production path could, since `run_gdb` and `run_make`
  take both values from the same config object. Had getting green required
  weakening an assertion, the validator would have been the thing to question.

- **2026-10-30** — **Cleanup pass over `src/`** (−13 lines net, gate: 146 tests
  + ruff + `reference.gdb` still reproducing SIGSEGV):
  * Removed `saved_reproductions()`, dead and documented with a false premise —
    "read by the host to build its report", when the host is a *different
    process* and globs the output directory instead. The `_SAVED` comment
    repeated the same claim.
  * Replaced a hand-rolled `_nullcontext` class with `contextlib.nullcontext`.
  * Collapsed five literal refusal dicts into one `_refused()` helper. That
    four-key shape is the contract the model reads, so five copies were five
    chances to drift.
  * Single-sourced the gdb-detection predicate, which `_extract_script`
    inlined alongside `_looks_like_gdb`.
  * Corrected comments: `sandbox.py` declared five layers but numbered four
    inline (the markers skipped `startup-with-shell off`); `runners.py`
    attributed async behaviour to **FastMCP**, renamed `MCPServer` in mcp 2.x;
    the layout tree claimed four tools where there are five.

- **2026-10-30** — **Commenting pass over `src/`.** The code was already at
  17–51% comment density, so this added almost no volume — the work was
  verifying claims and naming hazards. Four `AUDIT NOTE:` tags where there were
  none. Three claims I nearly wrote and measurement refuted, recorded because
  the near-misses are the lesson:
  * that this `${VAR:-fallback}` handling "differs from POSIX" — it does not;
    POSIX substitutes when the variable is unset *or null*, exactly as the `or`
    does;
  * that the timeout path "reports no signal" — output captured *before* a
    timeout can legitimately contain a real crash line;
  * that a CPU-limit kill is per-*tree* — measured per-process: under
    `prlimit --cpu=1` a child burned the full second and was SIGKILLed while
    its parent then spent its own 0.5s and survived. The first version of that
    experiment had the parent burn 2s against a 1s limit, so both hypotheses
    predicted its death and it proved nothing.
  Also verified rather than assumed: `SpanLimits().max_attributes == 128`, the
  `make probe-tools` target exists, and every config field named in the source
  is present in `config.yaml`.

- **2026-10-30** — Removed a compatibility `getattr(result, "isError", False)`
  from the MCP bridge. It was defended in a comment as surviving a version
  bump, and that rationale does not hold: the only bump it could serve is a
  move *down* to mcp 1.x, where `tool.input_schema` does not exist either, so
  the code would already have failed at the first tool listing. A dead shim in
  a project meant to be read teaches cargo-culting.

- **2026-10-30** — **Fixed the bug that made the agent loop: the two gdb tools
  took the program's input through different channels.** `run_gdb(program,
  args, gdb_commands)` injects arguments out of band, so a probe crashes
  without the script mentioning them. `save_reproduction` had no `args`
  parameter, so the agent saved the commands that had just worked and the input
  silently vanished — the program ran bare, printed its usage, exited 1, and
  every later command failed with "The program is not being run." Three
  consecutive runs burned all 24 iterations on the *simplest* example with
  eleven byte-identical refusals; the agent's diagnosis was correct from the
  first attempt and could not be accepted. `save_reproduction` now takes the
  same `(program, args, gdb_commands)` triple and the server writes the
  `set args` line into the artifact itself. The run that had produced nothing
  finished in **5 iterations, 4 tool calls, no retries**.
  *Lesson for the course:* the prompt did say "the script must `set args`
  itself". One line of instruction loses to a tool interface that demonstrates
  the opposite. When a dev-loop tool and the deliverable disagree about how
  input arrives, the agent learns the habit that does not transfer — so make
  the two interfaces identical rather than documenting the difference.
- **2026-10-30** — Refusal messages: **check for the cause before the
  consequence.** Running with no input produces two symptoms at once (the
  program exits early, then every command fails with "The program is not being
  run"), and the generic "your script errors" branch was tested first, so the
  specific message naming the missing input never ran. The agent read "fix the
  failing command", resent the same call, and looped. Also replaced
  `combined[-400:]` with `_head()`: gdb reports its first failure early and
  then cascades, so the tail was the least informative part — the refusal
  literally began mid-word (`gging using libthread_db`).
- **2026-10-30** — **`set args` cannot quote, and the artifact now refuses what
  it cannot carry.** A consequence of our own `set startup-with-shell off`: with
  no shell, gdb splits `set args` on whitespace itself and takes quotes
  literally. Measured — `set args 'two words'` arrives as `argv[1] == "'two"`,
  the double-quoted form as `"\""`, the backslash form as `"two\\"`. `run_gdb`
  is immune because it builds a real argv (`--args program …`); a saved script
  has no such channel. So a bug can be *probed* with such input and still not be
  expressible as a standalone artifact, and `save_reproduction` now refuses it
  rather than writing a script that feeds the program different input than the
  one it verified. This also rules out the tempting alternative fix — dropping
  `args` from `run_gdb` so the model writes `set args` itself: there is no
  quoting to delegate, only a limitation to respect.
- **2026-10-30** — **Made the loop observable, which is what ended the guessing.**
  It logged one line per *run* and nothing per turn, so a 24-iteration loop was
  indistinguishable from a hang and every diagnosis had to be inferred from
  source. Three runs and two wrong hypotheses went that way. Now: one INFO line
  per tool call (arguments collapsed and truncated), `IterationLimit` carries
  the `Transcript`, and a failure writes `output/failed-transcript.json` with a
  log line pointing at it. The first run *with* this answered the question
  immediately. Written on failure only; a success already produces the scripts.
- **2026-10-30** — Removed an `@plt` paragraph from the system prompt. It was
  imported from a binary-only agent and does not belong here: this agent
  compiles the target itself with debug symbols, and the transcript of a real
  run shows it anchoring on a source line in the project's own function
  (`break buggy.c:27`), never on a library-call PLT entry. The guidance
  addressed a failure mode this agent does not have while nudging it toward
  symbol-poor technique it has no reason to use. A test now forbids `@plt` and
  the libc primer names from reappearing — the same test that keeps `strcpy`
  out, since an earlier prompt used it as a syntax example and the model
  fabricated a strcpy diagnosis for a program that had none.
- **2026-10-30** — The pre-build output now reaches the agent. It was gathered,
  exit-code checked, logged and thrown away: `loop.run` had no `evidence`
  parameter, so `initial_task` ignored its argument entirely and the agent spent
  its first turn calling `build` again to learn what the pre-run already knew.

- **2026-10-29** — Fixed a crash on an *expected* outcome. `_run` caught
  `loop.IterationLimit` and `loop.TruncatedResponse` directly, but the loop runs
  inside an anyio task group (the MCP stdio client) and a task group re-raises
  whatever escapes it wrapped in an `ExceptionGroup`. The `except` clause
  therefore never matched: hitting the iteration cap — an ordinary result worth
  one log line — killed the process with a wall of anyio internals instead.
  `_unwrap_loop_failure` now searches the group recursively (duck-typing
  `.exceptions`, so no `BaseExceptionGroup` import is needed) and re-raises
  anything that is not ours, so genuine defects still surface. Two regression
  tests cover the nesting and the re-raise.
  *Lesson for the course:* a `try/except` around `async with` is not catching
  what the indentation suggests. Structured concurrency changes the exception's
  **type** on the way out, and tests that raise the exception directly — as ours
  did — will pass while production crashes.

- **2026-10-28** — Renamed the package and all identifiers `bta` → `bta1`
  (distribution, console scripts `bta1`/`bta1-mcp`, MCP server `bta1-tools`,
  logger, and `tracing.service_name`), so nothing is ambiguously named.
- **2026-10-28** — **Made the two projects independent.** The repository README
  was BTA1's full manual while `BTA1/README.md` was a stub pointing outward, so
  BTA1 was not self-contained. The manual now lives in `BTA1/README.md`; the
  root README is a short neutral index. All cross-references to the sibling
  project were removed from documentation, source comments and tests — each
  project is readable on its own, and only the repository root mentions both.
- **2026-10-28** — Documented how bug discovery actually works, because it was
  never written down: **deduction, not search.** There is no fuzzing and no
  input generation; the agent reads the parameter contract out of the source
  and derives the triggering value from the constraint each bug violates. Added
  the structural limits that follow (bounded by what it reads; no coverage
  notion; wrong hypotheses are caught but missed bugs are not), and a section
  separating **deduction from verification** — on `examples/parser` the agent
  named all three bugs and could only verify one.
- **2026-10-28** — Rewrote the output documentation around a **real generated
  artifact**: what the file contains, how to run it, what it can show (the
  `$1`/`$2` pair showing a NULL *arriving*), and what it cannot (unstable
  addresses, unverified prose, bugs never examined).
- **2026-10-28** — New example `examples/parser`: three bugs that are **not
  visible on the line they live on** — an unchecked `fopen` whose trigger is
  not a value in the source, a use-after-free that dies inside glibc several
  frames from the defect, and a null dereference behind a magic string. An
  off-by-one write was tried and dropped: it did not crash, since overwriting
  one byte of a `.rodata` pointer still yields a mapped address. That is worth
  knowing — an off-by-one is frequently not fatal, and `save_reproduction`
  rightly requires a real signal.
- **2026-10-28** — **Tracing no longer drowns a run in noise.** Two defects:
  the OTLP exporter retried every batch against a dead endpoint and logged a
  WARNING each time (hundreds of lines burying the real output), and the
  default 128-attribute span cap was exceeded by the OpenAI instrumentation,
  emitting "Attributes dict is full" per span *and silently truncating the
  data*. Now the collector's port is probed once before instrumenting — one
  honest line if absent — and `SpanLimits(max_attributes=512)` raises the cap.
- **2026-10-28** — Fixed a generated header reading `././parser`: the program
  path was blindly prefixed with `./`. The header is the first thing a reader
  copies, so it has to be a command that works.

- **2026-10-22** — **Security fix: closed a complete sandbox bypass.** gdb starts
  the inferior through `$SHELL` and passes `run` / `set args` arguments through
  it, while the denylist only ever inspected command *names*. So
  `run $(touch /tmp/pwned)` executed, backticks executed, and
  `set args ok > /path` wrote outside the project root — defeating all four
  layers with one line of generated gdb, reachable from a prompt injection in
  the target's source. `runners.run_gdb` now passes
  `-ex 'set startup-with-shell off'` **before** the script (applied at the
  launch level so no caller can forget it), and `set startup-with-shell` is
  denied so a script cannot turn it back on. Verified: closes redirection,
  `$(...)` and backticks; plain `set args` still works, because the shell is
  only needed for redirection, globbing and expansion. 9 regression tests.

- **2026-10-22** — **Renamed to Bugs Tracking Agent (BTA)**: Python package
  `debug_agent` → `bta1`, distribution and console script `bta1`, MCP server
  `bta1-tools`, OTel service `bta1`. The repository directory is unchanged.
- **2026-10-22** — **Multi-bug reporting.** A project may contain several
  independent bugs, and BTA now finds and reports each one separately. Added a
  fifth tool, `save_reproduction`, which the agent calls once per bug; it
  **verifies before accepting** (runs the script, refuses it if gdb errors or
  the program does not actually fail) and writes `out/bug-NN-<slug>.gdb` with a
  header naming the bug and its signal. `agent.output` became
  `agent.output_dir`; `--output` became `--output-dir`; `max_iterations` 12 → 24
  and `max_bugs` added. Added `examples/multi` — three independent bugs
  (overflow/SIGSEGV, divide-by-zero/SIGFPE, null-deref/SIGSEGV), each needing a
  different trigger input, with three hand-written reference scripts.
  **Verified on a real run:** Qwen3-4B found all three in 12 iterations and 11
  tool calls (11 min on CPU), with 1 refused save that the agent then corrected
  — the verification gate working as intended. All three generated scripts
  reproduce their bug with zero script errors.
  Note the loop itself did not change: "find them all" lives in the prompt and
  in `save_reproduction` returning rather than terminating.
- **2026-10-21** — Packaging and ergonomics. The agent now **always builds the
  project before reasoning**: gdb is useless without an up-to-date binary, so
  compiling is a precondition of the task rather than a decision the agent
  should make — and it saves a turn (~40s on a local model) while turning a
  compile error into a compile error instead of confusing gdb output. Added
  `requirements.txt` (pinned, `--exclude-editable`; `pyproject.toml` stays
  authoritative) and `run_from_scratch.sh`, a one-command demo that provisions
  the venv, downloads prebuilt llama.cpp, starts the server, runs the agent, and
  **verifies the generated script actually crashes** — exiting non-zero if not,
  so it works in CI. Config gained `${VAR:-fallback}` interpolation so the
  committed `base_url` default can be the Docker bridge gateway
  (`172.17.0.1:8000`) while the script overrides it to localhost for its own
  server. README gained an Overview (purpose, input, output, how to use the
  output, how to run, what to configure) and a complete document map.
- **2026-10-21** — Added `docs/llm-server-setup.md` and provider blocks for
  llama.cpp and Ollama alongside vLLM. Default provider is now **llama.cpp**:
  the available hardware (Intel i7-8565U, AVX2 only, no GPU) rules out vLLM
  entirely — its PyPI wheel is CUDA-only and its x86 CPU backend requires
  AVX-512, and without `avx512_bf16` the forced float32 path puts Qwen3-4B at
  ~16 GB of weights against ~11 GB of RAM. Model pinned to the non-thinking
  `Qwen3-4B-Instruct-2507`. `sandbox.max_output_bytes` lowered 65536 → 8000: at
  the old value a single `run_gdb` result was ~16k tokens and would overflow a
  modest context window on its own.
- **2026-10-21** — Implemented, phases 0–5. Deviations from the plan, all
  recorded here: the layout is one installable package `src/bta1/`
  (subpackages `mcp_server/`, `agent/`) rather than loose top-level modules, to
  avoid a generic `config` module in the global namespace; `mcp` 2.x renamed
  `FastMCP` to `MCPServer` and `Tool.inputSchema` to `Tool.input_schema`, so the
  dependency is pinned `>=2,<3`; `arize-phoenix` moved to an optional `[trace]`
  extra because only the trace *viewer* needs it; the loop is `async` because
  the MCP client API is; a `list_files` tool was added as the fourth tool for
  orientation. Phase 3 verified end-to-end against a mock OpenAI server, and the
  generated script reproduces the crash.
- **2026-10-21** — Verified the containment design experimentally; six findings
  changed it. Kill scope corrected from process group to **session** (`killpg`
  leaks forked grandchildren). `run_gdb` must parse output for the crash signal
  — `gdb --batch` exits 0 on a segfaulting inferior. `rlimit_address_space`
  default raised to 1 GB (gdb aborts below 512 MB). **Corrected the earlier
  ASLR claim**: gdb cannot disable ASLR in a container, so addresses are not
  reproducible — signal and source line are. `RLIMIT_NPROC` is per-UID.
  Platform fixed as Linux-only.
- **2026-10-21** — Target source reclassified from trusted to **semi-trusted**:
  documented prompt injection via a crafted source comment, and that layering is
  what downgrades it from code execution to an annoyance. Dropped `preexec_fn`
  in favour of `start_new_session=True` + a `prlimit` wrapper (fork-in-a-
  multi-threaded-process hazard). Project now targets Linux/WSL.
- **2026-10-21** — All tunables moved into `config.yaml` (pydantic-validated,
  `pyyaml`); CLI flags demoted to overrides. Sandboxing expanded to four
  documented layers with `resource.setrlimit` added, plus an explicit threat
  model. Recorded the repo file conventions.
- **2026-10-21** — Reverted the MCP dev-tooling decision: use the official MCP
  Inspector via `npx`, not a hand-written harness. Reimplementing a maintained
  official tool teaches the wrong habit, and `pytest` already covers
  programmatic tool exercise. Dropped `tools/mcp_cli.py` from the layout.
- **2026-10-21** — Initial architecture. Settled: Python, hand-written loop,
  OpenAI-compatible client with provider switch, 4 MCP tools over stdio,
  Phoenix tracing, sandboxing in the server, reference-implementation course
  shape. Deferred: eval harness, vLLM/Qwen3 tuning, staged labs.

---

## Build phases and early runs

- **2026-10-30** — **A mis-attribution corrected, and the defect behind it
  fixed.** The six-run scorecard above counted one failure as the model failing
  to reason about `set args`. It was not: `run_gdb` took the program's input as
  a parameter and injected it out of band, while `save_reproduction` had no such
  parameter and required the input to be inside the script. The agent saved the
  commands that had just worked, the input vanished, the program ran bare, and
  the save was refused. Three later runs reproduced it exactly — 24 iterations,
  eleven byte-identical refusals, a correct diagnosis every time.

  Both tools now take the same `(program, args, gdb_commands)` triple and the
  server writes the `set args` line into the artifact. The run that had produced
  nothing finished in 5 iterations with 4 tool calls.

  Two things worth carrying into the teaching material:

  * **Interface symmetry beats instruction.** The prompt did say the script must
    set its own input. That lost to a tool interface demonstrating the opposite.
  * **Attributing a failure to "model variance" is a hypothesis, not a
    diagnosis.** This looked exactly like a weak model and was not. The loop had
    no per-turn logging, which is what made the wrong explanation comfortable
    for three runs; it now logs every tool call and dumps the transcript on
    failure.

- **2026-10-22** — Renamed the project to **Bugs Tracking Agent (BTA)** and
  added **multi-bug reporting** (fifth tool `save_reproduction`, verified at
  save time; `examples/multi` with three independent bugs). Test count 119 →
  127. Two notes for the plan's own record:
  * The **loop needed no change.** Supporting N artifacts was entirely a prompt
    change plus a tool that returns instead of terminating. A good illustration
    of why the loop was kept minimal.
  * `save_reproduction` **reverses** the plan's original "the .gdb script is not
    a tool" decision. The reasoning behind that decision was sound for one
    artifact and wrong for N — recorded rather than quietly dropped, because the
    *why* is the teachable part. See architecture.md.
- **2026-10-21** — **First real-model runs** (llama.cpp, Qwen3-4B-Instruct-2507
  Q4_K_M, CPU). Probe passed on the first attempt. Six end-to-end runs surfaced
  three defects that no amount of mock testing would have found, all now fixed
  and covered by tests:

  1. **`SandboxError` was swallowed.** It subclassed plain `Exception`, which
     the MCP SDK treats as a *crash* — the model received only
     "Error executing tool list_files" while the useful text stayed
     server-side. Now subclasses the SDK's `ToolError` ("a failure you
     anticipated"), so refusals arrive as actionable feedback.
  2. **Prose leaked into the deliverable.** One run answered with four
     paragraphs of explanation followed by the script; the prose became line 1
     of the `.gdb` file and gdb died on it. The prompt now *requires* a fenced
     block and `_extract_script()` extracts it — aligning the contract with
     what the model already wants to do beats instructing it not to.
  3. **A wasted turn in every single run.** `initial_task()` named the project
     folder, so the model opened by calling
     `list_files(subdir="examples/overflow")` — which fails, because the tools
     are already rooted there. A prompt/interface mismatch: the prompt spoke in
     absolute paths, the tools in relative ones. Invisible in the tool tests,
     obvious in the first real log. Fixing it cut runtime from 3m30s to 2m04s.

  Also added a "the script must be self-contained" prompt section after a run
  emitted bare `run` with no arguments, so the program exited normally and
  reproduced nothing — plus the instruction to verify `signal` is non-null
  before finishing.

  **Honest scorecard across six runs:** two failures from the defects above,
  one failure recorded at the time as a *reasoning* failure (missing
  `set args`) — **re-attributed on 2026-10-30 to a tool-interface defect, not
  the model**; see the Changelog entry below — and three clean successes. After the
  fixes, runs 5 and 6 both produced error-free scripts that reproduce the
  crash. A 4B model is marginal-but-workable for this task; the variance is
  exactly why a single green run is not verification and why the eval harness
  matters.
- **2026-10-21** — All phases implemented and their gates green: 98 tests,
  ruff clean, `reference.gdb` reproduces the crash, the MCP server verified over
  real stdio JSON-RPC, and the agent verified end-to-end against a mock
  OpenAI-compatible server (5 iterations, 4 tool calls, generated script
  reproduces the SIGSEGV). Tracing verified three ways: spans emitted with the
  expected attributes, graceful degradation with no collector, and disabled by
  config. Two bugs found during implementation, both now covered by tests:
  `list_files` filtered on *absolute* path parts (so a project under a dotted
  directory listed nothing), and the loop-test mock recorded a live reference to
  the mutating message list rather than a snapshot.
- **2026-10-21** — Added the Verification discipline section: assumption probes
  (before a phase, throwaway) vs. acceptance gates (after a phase, committed);
  per-phase table; the caveat that a single agent run is a sample and not a
  test; and a required tool-calling probe before phase 3.
- **2026-10-21** — Containment design verified experimentally (8 checks + a
  decisive forking-inferior test). Changes: kill by **session** not process
  group; `run_gdb` gains a parsed `signal` field because `gdb --batch` exits 0
  on a segfaulting inferior; `rlimit_address_space_bytes` default 1 GB with a
  measured 512 MB floor; `rlimit_processes` raised (per-UID semantics);
  reference `.gdb` must not assert absolute addresses (ASLR cannot be disabled
  in a container); pytest needs a repo-local `basetemp` because `/tmp` is
  `noexec`. Platform fixed as Linux-only.
- **2026-10-21** — Documented prompt injection via the target source (source
  downgraded from trusted to semi-trusted) and why layering is what makes it
  survivable. Replaced `preexec_fn` with `start_new_session=True` + a `prlimit`
  wrapper, with the fork/threads hazard written up; records Linux/WSL as a
  project requirement. Annotated `gdb_timeout_s` as the knob to raise for
  complex targets; added `rlimit_file_size_bytes`.
- **2026-10-21** — Added `config.yaml` as the single home for all tunables
  (pydantic-validated, `pyyaml`); added `resource.setrlimit` as a fourth sandbox
  layer; added an explicit threat model and the layer/limitation table; recorded
  the repo file conventions (`LICENSE` untouched, "AI generated project" stays
  last in `README.md`).
- **2026-10-21** — Initial plan: 6 phases, specs for the 4 tools, the sandbox
  policy, the system prompt outline, and the loop's failure handling.
