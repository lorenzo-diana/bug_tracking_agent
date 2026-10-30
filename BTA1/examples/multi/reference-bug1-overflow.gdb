# Bug 1: stack buffer overflow in copy_label()
#
# strcpy() copies an unbounded argument into char buf[8], overwriting the saved
# return address. The crash therefore happens on RETURN from copy_label(), not
# at the strcpy itself.
#
# Observed failure: SIGSEGV
# Reproduce with: gdb --batch -nx -x reference-bug1-overflow.gdb ./bugs

set confirm off
set pagination off

set args copy AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
break copy_label
run

echo \n=== the destination can hold 8 bytes; the input is 40 ===\n
print sizeof(buf)
print (int) strlen(input)

next
next

echo \n=== after the copy: the saved return address is 0x4141...  ===\n
x/48bx buf
info frame

echo \n=== the crash lands on return ===\n
continue
backtrace
