# Model and hardware notes

Empirical observations from running BTA1 on specific models and specific
machines. **None of this is part of the project's design** — it is what we
measured, on hardware we happened to have, with models that happened to be
current. Treat every number as a data point with a date on it, not a
specification. [`docs/`](../docs/) holds the design; this holds the weather.

The short version: a 4-billion-parameter model on a laptop CPU is
*marginal-but-workable* for this task. It drives the loop, it usually reaches a
correct diagnosis, and it will sometimes need a retry. If results disappoint,
the model and the hardware are the first things to change — and the last things
to blame before you have read the transcript (see below).

---

## Do not blame the model first

Three consecutive runs burned the full iteration budget on the *simplest*
example and produced nothing. It looked exactly like a weak model running out
of depth. It was not: the two gdb tools took the program's input through
different channels, so the agent's correct answer could never be accepted. The
diagnosis had been right on the first attempt of all three runs.

**A failure that looks like model variance is a hypothesis, not a diagnosis.**
Read `<output-dir>/failed-transcript.json` before reaching for a bigger model.

---

## Other models tried

- **Qwen2.5-Coder-7B-Instruct** — emitted two *different* off-protocol
  tool-call shapes on two identical requests. Not recommended here.
- **Granite-3.1-2B** — made zero tool calls, writing a tutorial about the task
  instead of performing it. Below the capability floor for driving a loop.

---

## Measured: Qwen3-4B-Instruct-2507 Q4_K_M on an i7-8565U

A real data point, so you know what to expect rather than guessing.

Setup: `llama-server --jinja -c 16384 -t 8`, Q4_K_M (2.5 GB), 4-core mobile CPU,
agent running in a container reaching the host over the Docker bridge.

| | |
|---|---|
| `make probe-tools` | **passed first try** — well-formed `tool_calls` |
| Typical run | 4–5 iterations, 3–4 tool calls |
| Wall clock | **2–4 minutes** per run |
| Tokens | ~8–14k in, ~150–900 out |
| Success rate | **3 of 6 runs** before prompt fixes; **2 of 2** after |
| Multi-bug run (`examples/multi`, 3 bugs) | 12 iterations, 11 tool calls, **11 min**, all 3 bugs found and verified |

What the failures taught us, all now fixed in the code:

- Answering with explanatory prose *before* the script, which ended up in the
  `.gdb` file (→ the fenced-block contract)
- Emitting bare `run` with no arguments, so the program exited normally and
  reproduced nothing (→ the "script must be self-contained" prompt section)
- Opening every run with a doomed `list_files` on the project-root path
  (→ `initial_task()` no longer names it; saved ~40s per run)

**A 4B model is marginal but workable here.** It reliably drives the four-tool
loop and produces a correct script more often than not, but it needs an explicit
prompt and it will still occasionally produce a script that errors mid-way.
Expect better from a larger model on a GPU; expect worse below ~4B.

The variance is the real lesson: six runs with identical inputs gave materially
different results. That is why a single successful run is not verification, and
why the eval harness is the honest answer to "does this work?".

---

## Expectations on modest hardware

A 4B Q4 model on a mid-range laptop CPU generates roughly **5–15 tokens/sec**.
The agent loop makes five or more model calls with growing context, so budget
several minutes per run, and expect a 4B model to need more than one attempt to
drive four tools to a working script.

That is fine for *proving the integration works*. It is not what students should
use for iteration — for a class, a shared GPU server running vLLM is the right
target, and a local CPU server is a development stand-in.

---
