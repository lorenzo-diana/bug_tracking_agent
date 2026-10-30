/* Three bugs of deliberately DIFFERENT deduction difficulty.
 *
 * examples/overflow and examples/multi are both "the bug is visible on the
 * line it lives on". These are not, and that is the point: BTA1 discovers bugs
 * by READING and DEDUCING, never by trying inputs, so the interesting question
 * is how far that deduction reaches.
 *
 *   config <path>  unchecked fopen()      -- the bug is a MISSING line
 *   release <n>    use-after-free         -- ordering, not a single bad line
 *   mode <name>    magic-string gate      -- only "verbose" reaches the fault
 *
 * Reproduce by hand:
 *     make
 *     ./parser config /no/such/file    # unchecked fopen
 *     ./parser release 1               # use-after-free
 *     ./parser mode verbose            # null deref behind a string compare
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- bug 1 */
/* An unchecked return value.
 *
 * fopen() returns NULL when the file does not exist, and nothing here checks
 * it -- so fgets() dereferences NULL. The bug is the MISSING line, which is
 * why it cannot be spotted by looking at any line that IS present.
 *
 * It is also the only bug here whose trigger is not a value found in the
 * source: you have to realise the input must name a file that does NOT exist.
 * No amount of reading the code tells you which path that is. */
static void read_config(const char *path)
{
    FILE *f = fopen(path, "r");      /* BUG 1: no NULL check */
    char line[64];

    fgets(line, sizeof line, f);     /* dereferences NULL when fopen failed */
    printf("first line: %s", line);
    fclose(f);
}

/* ---------------------------------------------------------------- bug 2 */
/* Freed inside the loop, then used again on the next iteration -- so this is
 * a use-after-free READ followed by a DOUBLE FREE. The read often passes
 * silently (the memory is still mapped), and it is glibc's allocator that
 * notices the second free and aborts. So the observable failure is SIGABRT
 * from the allocator, NOT a segfault at the line that is actually wrong:
 * where it dies and where the bug lives are different places. */
static void release_labels(int rounds)
{
    char *label = malloc(16);
    if (label == NULL)
        return;
    strcpy(label, "first");

    for (int i = 0; i <= rounds; i++) {
        printf("round %d: %s\n", i, label);  /* BUG 2: read after free */
        free(label);
    }
}

/* ---------------------------------------------------------------- bug 3 */
/* The table has no "verbose" entry, so lookup_mode returns NULL -- and the
 * caller dereferences it without checking. Reaching this needs the EXACT
 * string "verbose": no other input gets here. */
struct mode { const char *name; const char *describe; };
static const struct mode MODES[] = {
    {"quiet", "no output"},
    {"normal", "default output"},
    {NULL, NULL},
};

static const struct mode *lookup_mode(const char *name)
{
    for (const struct mode *m = MODES; m->name != NULL; m++)
        if (strcmp(m->name, name) == 0)
            return m;
    return NULL;
}

static void show_mode(const char *name)
{
    if (strcmp(name, "verbose") == 0) {
        const struct mode *m = lookup_mode(name);
        printf("mode: %s\n", m->describe);  /* BUG 3: m is NULL here */
        return;
    }
    const struct mode *m = lookup_mode(name);
    printf("mode: %s\n", m ? m->describe : "unknown");
}

/* ------------------------------------------------------------------------ */

static void usage(const char *argv0)
{
    fprintf(stderr, "usage: %s <config|release|mode> <value>\n", argv0);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "config") == 0)
        read_config(argv[2]);
    else if (strcmp(argv[1], "release") == 0)
        release_labels(atoi(argv[2]));
    else if (strcmp(argv[1], "mode") == 0)
        show_mode(argv[2]);
    else {
        usage(argv[0]);
        return 1;
    }
    return 0;
}
