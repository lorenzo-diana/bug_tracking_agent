# Reference reproduction script -- hand-written, known good.
#
# This is the grading target: it is what we compare the agent's output against,
# and it is the artifact you would attach to a bug report. Someone who has never
# seen this code can run it and watch the bug happen.
#
# Run it with:
#     gdb --batch -nx -x reference.gdb ./buggy
#
# NOTE ON ADDRESSES: this script deliberately asserts nothing about absolute
# addresses. gdb cannot disable ASLR inside a container (it lacks the privilege),
# so pointer values differ on every run. The signal and the source location are
# stable; the addresses are not. A reproduction script that depends on a literal
# address is not a reproduction script -- it is a snapshot of one process on one
# machine.

set confirm off
set pagination off

# 40 bytes into an 8-byte buffer: comfortably past the saved return address.
# A 1-2 byte overflow often corrupts only padding and does not crash at all,
# which would make this a flaky example.
set args AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

break store_label
run

echo \n=== before the copy ===\n
# buf is uninitialised stack garbage here -- that is expected and worth seeing.
print sizeof(buf)
print input
# The cast is required: libc has no debug info here, so gdb does not know
# strlen's return type and refuses the call without being told. In --batch mode
# a single error like that aborts the entire script, so get it right.
print (int) strlen(input)

# Step past the strcpy so we can see the damage while the frame is still intact.
next
next

echo \n=== after the copy: buf has overflowed ===\n
# 40 bytes of 'A' in a buffer that can hold 8. Reading past the array shows the
# corruption reaching into the saved frame above it.
x/48bx buf
print (char *) buf

echo \n=== the frame is already corrupt ===\n
# The saved return address has been overwritten with 0x41414141...; the crash is
# therefore guaranteed, but it has not happened yet.
info frame

echo \n=== continue: the crash lands on return, not on the copy ===\n
continue

echo \n=== where it died ===\n
backtrace
info registers rip
