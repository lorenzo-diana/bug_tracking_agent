# Bug 3: NULL pointer dereference in print_name()
#
# NAMES[2] is a deliberate NULL hole. The bounds check validates the index
# against the array length but never checks that the entry itself is non-NULL,
# so strlen() dereferences NULL.
#
# Observed failure: SIGSEGV
# Reproduce with: gdb --batch -nx -x reference-bug3-nullderef.gdb ./bugs

set confirm off
set pagination off

set args name 2
break print_name
run

echo \n=== the index passes the bounds check ===\n
print index
# NAMES_LEN is a preprocessor macro, so there is NO symbol for it at -g -- gdb
# would need -g3 to see macros. Compute the length from the array instead.
print sizeof(NAMES) / sizeof(NAMES[0])

echo \n=== but the entry it selects is NULL ===\n
print NAMES[0]
print NAMES[1]
print NAMES[index]

echo \n=== strlen(NULL) faults ===\n
continue
backtrace
