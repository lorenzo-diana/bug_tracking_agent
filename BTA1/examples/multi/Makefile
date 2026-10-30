# Every flag here is load-bearing. Please do not "tidy" them.
# See ../overflow/Makefile for the full explanation of each one.
#
#   -g                     debug symbols; without them gdb has nothing
#   -O0                    source lines map to instructions
#   -fno-stack-protector   lets bug 1 produce a raw SIGSEGV instead of a tidy
#                          "stack smashing detected" abort

CC      := gcc
CFLAGS  := -g -O0 -fno-stack-protector -Wall
TARGET  := bugs

.PHONY: all clean

all: $(TARGET)

$(TARGET): bugs.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TARGET)
