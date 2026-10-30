# Example: three bugs that are *not* visible on one line

`examples/overflow` and `examples/multi` both plant bugs you can spot by
reading the offending line. These cannot be, and that is the point: BTA1
discovers bugs by **reading and deducing**, never by trying inputs, so the
interesting question is how far that deduction reaches.

| Trigger | Bug | Signal | Why it is hard to deduce |
|---|---|---|---|
| `config /no/such/file` | unchecked `fopen()` → `fgets(NULL)` | `SIGSEGV` | the bug is a **missing line**, and the trigger is a value **not in the source** |
| `release 1` | use-after-free, then double free | `SIGABRT` | **where it dies ≠ where the bug is** — glibc aborts, the fault is the loop order |
| `mode verbose` | `lookup_mode` returns NULL, unchecked | `SIGSEGV` | reachable **only** with the exact string `"verbose"` |

```sh
make
./parser config /no/such/file
./parser release 1
./parser mode verbose

./parser config /etc/hostname   # clean
./parser release 0              # clean
./parser mode quiet             # clean
```

## What each one tests

**`config` — a trigger that is not in the code.** Every other example in this
repo has its triggering input derivable from a constant: `buf[8]` implies ">7
characters", `NAMES[2] == NULL` implies "index 2". Here the agent must realise
the input has to name a file that *does not exist* — a fact about the world,
not about the source. If BTA1 can reproduce this one, its deduction extends
past reading constants.

**`release` — the crash site is not the bug site.** The allocator aborts on the
second `free()`; the actual defect is that the loop frees inside its body and
reads on the next iteration. An agent that only looks where the signal arrives
will describe the wrong thing. Note also the use-after-free *read* usually
passes silently — the memory is still mapped — so the observable failure comes
from the allocator's bookkeeping, not from the illegal access.

**`mode` — a magic-string gate.** `show_mode` only takes the faulting branch
for `"verbose"`. Any other value, including every name actually in the table,
goes down the safe path. This is the case a fuzzer would struggle with and
deduction gets immediately, because the string is right there in the `strcmp`.

## Why there is no off-by-one here

The first draft used an off-by-one write (`i <= 8` into `char key[8]`,
clobbering an adjacent pointer). It was dropped because **it did not crash** —
overwriting one byte of a pointer into `.rodata` still yields a mapped address,
so the program printed garbage and exited 0.

That is a genuine and underappreciated property: *an off-by-one is frequently
not fatal.* It is excellent material for discussion, but it is useless as a
BTA1 example, because `save_reproduction` requires a real fatal signal — and
rightly so. A script that "reproduces" a bug by producing no observable failure
reproduces nothing.

## Discussion questions

**Which of these would a fuzzer find first?** `release 1` falls out of almost
any integer input. `mode verbose` needs one specific string out of the whole
space. `config` needs a path that does not exist, which a fuzzer generates by
accident constantly. Deduction and fuzzing have nearly opposite strengths here.

**For `release`, where should the reproduction script stop?** At the `free()`,
at the second iteration's read, or at the abort? The signal arrives in the
allocator, inside libc, with your code several frames up. What does the student
most need to see?

**Could BTA1 find a bug with no textual tell at all?** No — and this example is
the closest it gets to that boundary. See the Known limitations section of the
[BTA1 README](../../README.md).
