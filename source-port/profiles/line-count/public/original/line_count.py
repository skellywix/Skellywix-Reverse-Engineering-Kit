"""Count LF (0x0A) bytes on standard input. Reviewed owned example for the source-port verifier."""
import sys

USAGE = b'usage: line-count\n'
CHUNK = 65536


def count_lf(stream):
    total = 0
    while True:
        block = stream.read(CHUNK)
        if not block:
            return total
        total += block.count(b'\n')


def main(argv):
    if len(argv) != 1:
        sys.stderr.buffer.write(USAGE)
        return 2
    sys.stdout.buffer.write(b'%d\n' % count_lf(sys.stdin.buffer))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
