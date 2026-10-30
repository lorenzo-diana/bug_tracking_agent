# Flags are load-bearing -- see ../overflow/Makefile for the full reasoning.
#   -g -O0                debug symbols; source maps onto instructions
#   -fno-stack-protector  bug 1 must produce a raw fault, not a canary abort
CC      := gcc
CFLAGS  := -g -O0 -fno-stack-protector -Wall
TARGET  := parser

.PHONY: all clean
all: $(TARGET)
$(TARGET): parser.c
	$(CC) $(CFLAGS) -o $@ $<
clean:
	rm -f $(TARGET)
