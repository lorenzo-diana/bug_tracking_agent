# Bugs Tracking Agent (BTA)

An AI agent that helps track bugs in C/C++ code by developing a gdb script to
reproduce each bug it finds.

## [`BTA1/`](BTA1/) — you have the source code

Give it a folder of C/C++ **source** plus a Makefile. It reads the code, forms a
hypothesis about each bug, drives gdb to confirm it, and writes one **verified
`.gdb` script per bug** — the artifact you attach to a bug report so someone
else can reproduce the failure on demand.

A teaching reference implementation: a lesson in how to *build* an AI agent,
written to be read.

→ [`BTA1/README.md`](BTA1/README.md)

## Getting started

```sh
cd BTA1
make setup
make help
```

It needs an OpenAI-compatible LLM endpoint. See
[`BTA1/docs/llm-server-setup.md`](BTA1/docs/llm-server-setup.md).

## Where things are written down

The project separates the two kinds of writing, and the split is worth knowing
before you go looking:

| Folder | Holds | Answers |
|---|---|---|
| `docs/` | architecture, goals, limitations by design | "what is this and how does it work?" |
| `journal/` | changelog, build record, measurements from particular models and machines | "why is it like that, and what happened?" |

You never need `journal/` to use the agent. It is where open questions live,
though — anything unresolved is tagged `OPEN:` in a changelog entry.

## AI generated project

This project has been written mainly by AI.
