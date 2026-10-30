# Journal

**This folder is history, not documentation.** Nothing here is required to
understand, run or modify BTA1.

The split is deliberate:

| Folder | Holds | Answers |
|---|---|---|
| [`../docs/`](../docs/) | the project's durable knowledge — architecture, goals, limitations by design | "what is this and how does it work?" |
| `journal/` (here) | how it got that way, and what we measured along the way | "why is it like that, and what happened?" |

A reader learning BTA1 should never need this folder. A reader asking *why* a
decision was made, or wondering whether a disappointing run is the model or the
code, belongs here.

## Contents

**Open items** are marked `OPEN:` at the top of their changelog entry — grep
for it. Right now there is one: an unexplained save/refuse loop on
`examples/multi`, where the agent deduced all three bugs and then could not get
the first one accepted. The entry records what was observed, why the reason was
not captured, and exactly how to pick it up.

- [`changelog.md`](changelog.md) — what changed, newest first, with the
  reasoning that drove each change. Several entries are wrong turns that were
  later corrected; those are kept on purpose.
- [`model-and-hardware-notes.md`](model-and-hardware-notes.md) — measurements
  from specific models on specific machines. Data points with dates, not
  specifications.
- [`implementation-plan.md`](implementation-plan.md) — the original build
  order: phases, specifications and the acceptance gate for each. A planning
  artifact, kept because the *verification discipline* it describes (assumption
  probes before a phase, acceptance gates after) is worth more than the plan
  itself.

## What belongs where

When adding to the project, the test is whether a newcomer needs it to *use*
what exists:

- **`docs/`** — a design decision, a constraint that follows from the
  architecture, a limitation that will still be true next year.
- **`journal/`** — a dated event, a measurement, an observation about one
  model or one machine, a mistake and its correction.
