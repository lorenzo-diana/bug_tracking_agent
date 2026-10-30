/* A planted bug: a classic stack buffer overflow.
 *
 * store_label() copies a caller-supplied string into a fixed 8-byte array with
 * strcpy(), which has no idea how large the destination is. Any argument longer
 * than 7 characters (plus the NUL) writes past the end of buf -- over the saved
 * registers and the saved return address that live above it on the stack.
 *
 * The crash therefore happens on *return* from store_label(), not at the
 * strcpy() itself. That gap between "where the corruption happens" and "where
 * the program dies" is exactly what makes this worth a debugger, and what the
 * agent has to work out.
 *
 * Reproduce by hand:
 *     make
 *     ./buggy AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
 */

#include <stdio.h>
#include <string.h>

#define BUF_SIZE 8

static void store_label(const char *input)
{
    char buf[BUF_SIZE];

    strcpy(buf, input); /* BUG: no bound on the copy */

    printf("label: %s\n", buf);
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <label>\n", argv[0]);
        return 1;
    }

    store_label(argv[1]);

    return 0;
}
