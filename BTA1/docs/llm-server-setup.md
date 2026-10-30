# LLM server setup

The agent needs an **OpenAI-compatible** endpoint. Which server you run is a
deployment decision, not an architectural one: `providers.py` is the only module
that knows, and switching is one line in `config.yaml`.

This document covers the three servers with blocks in `config.yaml`, the flags
that actually decide whether tool calling works, and how to choose a Qwen3
variant.

**Before anything else, know this:** the single highest-risk unknown in the whole
project is whether your server returns `tool_calls` in the OpenAI response
format. Every section below ends at the same place —

```sh
make probe-tools
```

Twenty seconds, and it tells you whether the ground is solid. A misconfigured
tool-call parser makes a *correct* agent look broken, and without the probe you
can lose a day to it.

---

## Choosing a server

| | llama.cpp | vLLM | Ollama |
|---|---|---|---|
| Hardware | **CPU (AVX2 is enough)** | **GPU (CUDA)** | CPU or GPU |
| Install | prebuilt binary | `pip install vllm` | one shell script |
| Qwen3-4B footprint | ~2.5 GB (Q4_K_M) | ~8 GB (bf16) | ~2.5 GB (Q4) |
| Pin the exact model variant? | **yes** (`-hf repo:quant`) | yes (HF path) | only what the library ships |
| Constrained decoding | GBNF grammars | xgrammar | limited |
| Concurrency | modest | **excellent** | modest |
| Use it when | you have no GPU | teaching a class | you want zero friction |

**Decision rule:** GPU available → vLLM. No GPU → llama.cpp. Need it working in
five minutes and willing to accept the thinking-mode caveat → Ollama.

---

## Pick the Qwen3 variant deliberately

This matters more than any flag, and it is the most common way to lose an
afternoon.

| Model | Thinking | Verdict |
|---|---|---|
| **`Qwen3-4B-Instruct-2507`** | none | **Use this.** Nothing to collide with tool-call parsing. |
| `Qwen3-4B` (original hybrid) | **on by default** | Workable, but the thinking block interferes with tool-call parsing. Needs a reasoning parser or thinking disabled. |
| `Qwen3-4B-Thinking-2507` | always | Avoid for this project. |

Reasoning models emit their chain of thought before the answer. If the server
does not know to strip that, the text arrives mixed into the tool-call region
and the parser produces either nothing or garbage. Choosing a non-thinking
variant sidesteps the entire class of problem.

---

## llama.cpp (CPU)

Purpose-built for CPU inference, needs only AVX2, and lets you name the exact
GGUF you want.

Grab a prebuilt binary from <https://github.com/ggml-org/llama.cpp/releases>
(`llama-b*-bin-ubuntu-x64.zip`) — no build required.

```sh
./llama-server \
    -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
    --host 0.0.0.0 \
    --port 8000 \
    --jinja \
    -c 16384 \
    -t 8
```

| Flag | Why it matters |
|---|---|
| `--jinja` | **Required for tool calling.** It applies the model's chat template, which is what emits and parses `tool_calls`. Without it you get no tool calls at all — and no error explaining why. |
| `--host 0.0.0.0` | Binds all interfaces. The default is loopback-only, which is unreachable from a container or another machine no matter what address you point at it. |
| `-hf repo:QUANT` | Downloads the GGUF from HuggingFace. This is where you **pin the non-thinking Instruct variant**. |
| `-c 16384` | Context window. Must exceed your largest tool result — see *Context budget* below. |
| `-t 8` | Threads. Match your core/thread count. |

Quantization: `Q4_K_M` is the usual quality/size sweet spot (~2.5 GB for a 4B
model). `Q5_K_M` or `Q6_K` if you have RAM to spare and want slightly better
instruction-following; tool-call reliability does degrade at very low bit widths.

If the GGUF repo name 404s, search HuggingFace for "Qwen3-4B-Instruct-2507 GGUF"
— community repackagers rename things.

Then set `llm.provider: llamacpp` in `config.yaml` and run `make probe-tools`.

---

## vLLM (GPU)

What you would deploy for a class: built for many concurrent requests, with
grammar-constrained decoding that makes tool-call JSON valid by construction.

```sh
python3 -m venv .venv-vllm && .venv-vllm/bin/pip install vllm

.venv-vllm/bin/vllm serve Qwen/Qwen3-4B-Instruct-2507 \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name qwen3-4b \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --max-model-len 32768
```

| Flag | Why it matters |
|---|---|
| `--enable-auto-tool-choice` | **Required.** Without it vLLM never parses tool calls and the model's attempt arrives as prose. |
| `--tool-call-parser hermes` | Qwen3 emits tool calls in `<tool_call>…</tool_call>` tags — the Hermes convention. The wrong parser means no `tool_calls` field. |
| `--host 0.0.0.0` | As above. |
| `--served-model-name` | Optional. Gives `config.yaml` a short alias instead of the full HF path. |
| `--max-model-len` | Context. Lower it to shrink KV cache if VRAM is tight. |

If you use the hybrid `Qwen3-4B` instead of the Instruct variant, add
`--reasoning-parser qwen3` so thinking output is separated from the tool-call
region.

**VRAM:** Qwen3-4B in bf16 needs roughly 8 GB for weights plus KV cache. On an
8–12 GB card, reduce `--max-model-len` or serve a quantized build.

Constrained decoding is on by default in recent vLLM (xgrammar), which is a
large reliability win on small models — it forces syntactically valid tool-call
JSON rather than hoping for it. Confirm on your version rather than assuming.

### vLLM on CPU: don't

Recorded because it is the obvious thing to try. Two hard blockers:

1. **The x86 CPU backend requires AVX-512.** There is also no CPU wheel on
   PyPI, so it means building from source (`VLLM_TARGET_DEVICE=cpu`).
2. **Without `avx512_bf16`, you are forced to `float32`** — Qwen3-4B becomes
   ~16 GB of weights before any KV cache.

Check your CPU with `grep -o 'avx512[a-z_]*' /proc/cpuinfo | sort -u`. If that
comes back empty, use llama.cpp.

For completeness, the GPU→CPU flag translation:

| GPU | CPU equivalent |
|---|---|
| `--gpu-memory-utilization` | gone; env `VLLM_CPU_KVCACHE_SPACE=4` (GiB) |
| implicit device | `--device cpu` |
| `--dtype bfloat16` | `--dtype float32` without AVX-512-BF16 |
| CUDA graphs | `--enforce-eager` |
| — | `VLLM_CPU_OMP_THREADS_BIND` for thread pinning |

**The tool-calling flags are backend-independent.**
`--enable-auto-tool-choice` and `--tool-call-parser` are identical on CPU and
GPU.

---

## Ollama (zero-build fallback)

```sh
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:4b
OLLAMA_HOST=0.0.0.0 ollama serve        # serves /v1 on port 11434
```

Easiest possible setup. One caveat worth taking seriously: Ollama's `qwen3:4b`
is the **hybrid thinking** model, and thinking output colliding with tool-call
parsing is exactly the failure mode we have been designing around. Controlling
thinking through the OpenAI-compatible endpoint is awkward. If the probe shows
tool calls arriving as prose, that is the likely cause — switch to llama.cpp,
where the variant is yours to pin.

---

## Reaching the server from elsewhere

If the agent and the server are not on the same host — a container, a VM, a lab
machine — then:

1. The server **must** bind `0.0.0.0`. A loopback bind is unreachable from
   outside no matter which address the client uses.
2. `base_url` must be the server's reachable address, not `127.0.0.1`.
3. The port must be open through any firewall in between.

Check it from the client side before blaming the agent:

```sh
curl http://<host>:<port>/v1/models
```

A model list back means the transport is fine and any remaining problem is
tool-call parsing — which is what the probe is for.

---

## Context budget

A detail that bites in practice. `sandbox.max_output_bytes` caps how much a
single tool result can return, and that text goes straight into the model's
context.

At the original 65536 bytes, one `run_gdb` result was roughly **16k tokens** —
enough to overflow a modest context window by itself. It is now **8000 bytes**
(~2k tokens), which fits comfortably in a 16k window alongside the system
prompt, the tool schemas, and several turns of history.

The rule: `max_output_bytes` and the server's context length move together.
Raise one, raise the other.

A tighter cap is not purely a constraint — truncated results carry an explicit
`...[truncated N bytes]` marker, which tells the agent evidence was withheld and
nudges it toward narrower, more deliberate queries instead of dumping state.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Probe: `could not reach the model` | Server not running, wrong `base_url`, or bound to loopback instead of `0.0.0.0` |
| Probe: `did not return tool_calls`, and the tool name appears in **prose** | Tool-call parsing not enabled: missing `--jinja` (llama.cpp) or `--enable-auto-tool-choice` / wrong `--tool-call-parser` (vLLM) |
| Probe: `did not return tool_calls`, no mention of the tool | Model may not support tool calling, or thinking output is swallowing it — check the variant |
| Probe: arguments are not valid JSON | Weak model. Enable constrained decoding (GBNF / xgrammar), or use a larger model |
| Agent raises `TruncatedResponse` | Raise `llm.max_tokens` |
| Agent raises `IterationLimit` | Read the trace (`make phoenix`). Usually the agent is looping on a failing hypothesis — a prompt problem, not a loop problem |
| Server 400s on the second request | A message-ordering bug: the assistant's `tool_calls` message must precede its `tool` results. Covered by `tests/test_loop.py` |
