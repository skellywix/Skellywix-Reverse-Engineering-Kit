"""Compare two Linux CLIs using the same argv[0], environment and file names."""
import base64
import itertools
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile


def compare(reference, candidate):
    rng = random.Random(150)
    total = 0
    env = dict(os.environ, LC_ALL='C')
    env.pop('POSIXLY_CORRECT', None)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def check(args=(), data=b'', posix=False):
            nonlocal total
            observed = []
            for exe in (reference, candidate):
                (root / 'in').write_bytes(data)
                (root / '-d').write_bytes(data)
                output = root / 'out'
                output.unlink(missing_ok=True)
                p = subprocess.run(['base64', *args], executable=exe, cwd=root,
                                   input=data, capture_output=True, timeout=5,
                                   env=env | ({'POSIXLY_CORRECT': '1'} if posix else {}))
                observed.append((p.returncode, p.stdout, p.stderr,
                                 output.read_bytes() if output.exists() else None,
                                 (root / 'in').read_bytes(), (root / '-d').read_bytes()))
            if observed[0] != observed[1]:
                raise AssertionError((args, data[:20], [repr(x)[:400] for x in observed]))
            total += 1
        for n in [*range(160), 254, 255, 256, 257, 8191, 8192, 8193, 65537]:
            data = rng.randbytes(n)
            check((), data)
            check(('--decode',), base64.b64encode(data))
        for c in range(256):
            for pos in range(4):
                data = b'QUJD' + b'A'*pos + bytes([c]) + b'A'*(3-pos) + b'YWJj'
                check(('-d',), data)
                check(('-dn',), data)
        for group in itertools.product(b'A=', repeat=4):
            check(('-d',), bytes(group) + b'#!')
        for args in [('-d',), ('--noerrcheck', '-d'), ('-de',), ('-ed',), ('--dog',),
                     ('--help',), ('--version',), ('--copyright',), ('-u',), ('-?',), ('-z',),
                     ('--unknown',), ('--', '-d'), ('-d-',), ('-e-', 'version'), ('-', '-'),
                     ('in', 'out'), ('in', '-d', 'out'), ('-d', '-', 'out'), ('missing',),
                     ('in', '.'), ('in','out','extra'), ('missing', '-', 'extra'), ('-d', '.'),
                     (b'missing_\xff',), ('in', 'in')]:
            for posix in (True, False):
                for data in (b'TQ', b'YWJj', b'T Q=\x00=ignored'):
                    check(args, data, posix)
        for exe in (reference, candidate):
            with subprocess.Popen([exe, '-d'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                proc.stdin.write(b'TQ==')
                proc.stdin.flush()
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:  # Expected: the writer still holds the pipe.
                    stdout, stderr = proc.communicate(b' ' * 252, timeout=3)
                else:
                    raise AssertionError('decoder returned before block was full or EOF')
                if (stdout, stderr, proc.returncode) != (b'M', b'', 0):
                    raise AssertionError('unexpected result after completing the block')
        total += 1
        print(f'{total} comparisons passed: exit status, stdout, stderr, output file')


if __name__ == '__main__':
    compare(*(str(Path(p).resolve(strict=True)) for p in sys.argv[1:]))
