/* Three independent, planted bugs -- the multi-bug test case.
 *
 * Each is reached through a different first argument, so each needs its own
 * reproduction script with its own `set args`. That is the point: one bug per
 * script, and a script that does not set the right input reproduces nothing.
 *
 *   ./bugs copy <string>   -> stack buffer overflow  (SIGSEGV)
 *   ./bugs scale <n>       -> division by zero       (SIGFPE)
 *   ./bugs name <index>    -> NULL pointer deref     (SIGSEGV)
 *
 * The three failures are genuinely different: a corrupted return address, an
 * integer division trap, and a dereference of a NULL table entry. They fail at
 * different places, with two distinct signals.
 *
 * Reproduce by hand:
 *     make
 *     ./bugs copy AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
 *     ./bugs scale 0
 *     ./bugs name 2
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ bug 1 */
/* strcpy() into a fixed 8-byte array. Any argument longer than 7 characters
 * overruns buf and overwrites the saved return address above it, so the crash
 * lands on RETURN from this function, not on the copy itself. */
static void copy_label(const char *input)
{
    char buf[8];

    strcpy(buf, input); /* BUG 1: unbounded copy into a fixed buffer */

    printf("label: %s\n", buf);
}

/* ------------------------------------------------------------------ bug 2 */
/* The divisor comes straight from the caller with no zero check, so
 * `scale 0` traps in the integer division. */
static void scale_value(int divisor)
{
    int total = 1000;

    printf("scaled: %d\n", total / divisor); /* BUG 2: divisor may be zero */
}

/* ------------------------------------------------------------------ bug 3 */
/* The table has a deliberate NULL hole, and the index is only checked against
 * the upper bound -- never against the entry being valid. */
static const char *const NAMES[] = {"alpha", "beta", NULL, "delta"};
#define NAMES_LEN (sizeof(NAMES) / sizeof(NAMES[0]))

static void print_name(int index)
{
    if (index < 0 || (unsigned long) index >= NAMES_LEN) {
        fprintf(stderr, "index out of range\n");
        return;
    }

    /* BUG 3: NAMES[2] is NULL and nothing checks for it */
    printf("name length: %zu\n", strlen(NAMES[index]));
}

/* -------------------------------------------------------------------------- */

static void usage(const char *argv0)
{
    fprintf(stderr, "usage: %s <copy|scale|name> <value>\n", argv0);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "copy") == 0) {
        copy_label(argv[2]);
    } else if (strcmp(argv[1], "scale") == 0) {
        scale_value(atoi(argv[2]));
    } else if (strcmp(argv[1], "name") == 0) {
        print_name(atoi(argv[2]));
    } else {
        usage(argv[0]);
        return 1;
    }

    return 0;
}
