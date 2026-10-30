# Example: stack buffer overflow

The target the agent is asked to debug, and the fixture the test suite runs
against.

## The bug

`store_label()` copies a caller-supplied string into `char buf[8]` with
`strcpy()`. `strcpy()` does not know how large the destination is, so any
argument longer than 7 characters writes past the end of the array — over the
saved registers and the saved return address above it on the stack.

The interesting part: **the crash does not happen at the `strcpy()`.** It
happens when `store_label()` returns and the CPU jumps to the corrupted return
address. That gap between where the corruption occurs and where the program dies
is why this needs a debugger, and it is what the agent has to work out.

## Reproduce by hand

```sh
make
./buggy ok                                          # fine
./buggy AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA    # Segmentation fault
gdb --batch -nx -x reference.gdb ./buggy             # the full story
```

The money line in the reference output:

```
rip = 0x... in store_label (buggy.c:30); saved rip = 0x4141414141414141
```

`0x41` is `'A'`. The return address is now forty bytes of our input.

## Build flags

`-g -O0 -fno-stack-protector`. Every one is load-bearing — see the comments in
the `Makefile`. In particular, without `-fno-stack-protector` you get a tidy
`*** stack smashing detected ***` abort instead of the raw `SIGSEGV`, which is a
different and far less instructive failure.

## Why addresses differ between runs

gdb normally disables ASLR so addresses repeat. Inside a container it cannot —
it lacks the privilege — and reports:

```
warning: Error disabling address space randomization: Operation not permitted
```

So pointer values change on every run. The **signal** and the **source
location** do not.

This is a feature, not an obstacle: a reproduction script that depends on a
literal address is not a reproduction script, it is a snapshot of one process on
one machine. `reference.gdb` asserts on the signal, the source line, and the
corrupted *values* — never on raw addresses.

## Discussion questions

**AddressSanitizer would have caught this in one run. Why use gdb?**

ASan is the better tool for *finding* the bug: rebuild with
`-fsanitize=address`, run once, get a precise report. But:

- ASan tells you *that* and *where*. A `.gdb` script reproduces the failure **on
  demand**, which is what you attach to a bug report so someone else can watch
  it happen.
- ASan needs a rebuild. Sometimes you cannot rebuild — a customer's binary, a
  release artifact, a crash that only occurs in one deployment.
- The script is a *shared artifact*. "Run this" beats "trust my description of
  the stack."

Try it: `gcc -g -O0 -fsanitize=address -o buggy_asan buggy.c` and compare what
each approach tells you.

**Why is there a second, lesser bug?**

`main()` checks `argc`, but nothing validates the input length before the copy.
The agent may report either. Both are real; only one causes the crash.
Distinguishing "a bug" from "the bug that produces this failure" is a core
debugging skill.
