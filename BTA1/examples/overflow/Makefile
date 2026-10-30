# Every flag here is load-bearing. Please do not "tidy" them.
#
#   -g                     debug symbols. Without them gdb has nothing to work
#                          with: no line numbers, no variable names, no frames.
#
#   -O0                    no optimization, so source lines map onto
#                          instructions and `buf` is not kept in a register.
#                          At -O2 the overflow may vanish or move.
#
#   -fno-stack-protector   WITHOUT this, gcc inserts a stack canary and the
#                          program dies with a clean
#                          "*** stack smashing detected ***" abort. That is a
#                          different -- and much less instructive -- failure to
#                          reproduce. We want the raw SIGSEGV on return.
#
# See docs/architecture.md, "The example bug".

CC      := gcc
CFLAGS  := -g -O0 -fno-stack-protector -Wall
TARGET  := buggy

.PHONY: all clean

all: $(TARGET)

$(TARGET): buggy.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TARGET)
