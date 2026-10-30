# Bug 2: division by zero in scale_value()
#
# The divisor comes from the caller with no zero check, so `scale 0` traps in
# the integer division. A different signal from the other two bugs: SIGFPE.
#
# Observed failure: SIGFPE
# Reproduce with: gdb --batch -nx -x reference-bug2-divzero.gdb ./bugs

set confirm off
set pagination off

set args scale 0
break scale_value
run

echo \n=== divisor is zero, and nothing checks it ===\n
print divisor
print total

echo \n=== the division traps ===\n
continue
backtrace
