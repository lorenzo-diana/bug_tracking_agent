# Example: three independent bugs

The multi-bug test case. `examples/overflow` proves the agent can reproduce *a*
bug; this one proves it keeps looking after the first.

## The bugs

Each is reached through a different first argument, so **each needs its own
reproduction script with its own `set args`** — written into the script by
`save_reproduction` from the `args` it was given. That is the whole point: one
bug per script, and a script that does not set the right input reproduces
nothing.

| # | Trigger | Bug | Signal |
|---|---|---|---|
| 1 | `./bugs copy <long string>` | `strcpy()` into `char buf[8]` overruns the saved return address | `SIGSEGV` |
| 2 | `./bugs scale 0` | divisor used with no zero check | `SIGFPE` |
| 3 | `./bugs name 2` | `NAMES[2]` is a `NULL` hole; only the index bound is checked | `SIGSEGV` |

They fail in genuinely different ways — a corrupted return address, an integer
division trap, and a dereference of a null table entry — at three different
places, with two distinct signals.

Every clean path still works, which matters: a bug the program hits on *every*
input is not interesting to reproduce.

```sh
make
./bugs copy ok        # label: ok
./bugs scale 4        # scaled: 250
./bugs name 1         # name length: 4
```

## Reproduce by hand

```sh
make
./bugs copy AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA   # SIGSEGV
./bugs scale 0                                          # SIGFPE
./bugs name 2                                           # SIGSEGV

gdb --batch -nx -x reference-bug1-overflow.gdb   ./bugs
gdb --batch -nx -x reference-bug2-divzero.gdb    ./bugs
gdb --batch -nx -x reference-bug3-nullderef.gdb  ./bugs
```

The three `reference-*.gdb` files are the hand-written known-good scripts — the
grading target the agent's output is compared against.

## Run the agent on it

```sh
python -m bta1.agent --project-root examples/multi --output-dir output
```

A complete run writes three scripts:

```
output/bug-01-stack-buffer-overflow-in-copy-label.gdb
output/bug-02-division-by-zero-in-scale-value.gdb
output/bug-03-null-pointer-dereference-in-print-name.gdb
```

Finding three bugs takes several times as many turns as finding one, so expect
this to be slow on a small local model, and expect partial results: two of three
is a common outcome. That is honest behaviour, not a malfunction — and it is
exactly why the project needs an evaluation harness rather than a single demo
run.

## Why `-g` is not enough for macros

`reference-bug3-nullderef.gdb` computes the array length with
`sizeof(NAMES) / sizeof(NAMES[0])` rather than printing `NAMES_LEN`. Macros are
not symbols: at `-g` gdb has no record of them at all, and
`print NAMES_LEN` fails with *"No symbol NAMES_LEN in current context"*.

You would need `-g3` for macro information. Worth knowing because in `--batch`
mode a single failing command aborts the rest of the script, so this kind of
mistake costs the whole reproduction. The agent hits it too.

## Discussion questions

**Why does each bug need a separate script?**

Because a reproduction script answers one question: *how do I see this specific
failure?* A script that triggered all three would crash on the first and never
reach the others — the program dies once. Separate artifacts also mean separate
bug reports, separate fixes, and separate regression tests.

**Which bug would a fuzzer find fastest? Which would it miss?**

`scale 0` falls out of almost any input generator. The `name 2` null hole needs
exactly one value out of four. The overflow needs a long string. Thinking about
the input space each bug occupies is good practice before reaching for a tool.

**Two of these are `SIGSEGV`. Is the signal enough to tell bugs apart?**

No — and that is why the scripts assert on the *source location* and the
*corrupted values*, not just the signal.
