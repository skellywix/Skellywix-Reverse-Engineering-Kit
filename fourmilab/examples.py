"""Fixed discovery probes; runs only inside the networkless reference container."""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def collect():
    exe = '/input/base64'
    with open(exe, 'rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != '0b946702f73b4bc2d478348bc57b5e011a3a02423ca552ae0e2176c9c5f3df22':
            raise ValueError('Wrong Fourmilab reference.')
    commands = [([], x) for x in (b'', b'a', b'ab', b'abc', b'x' * 54, b'x' * 55)]
    commands += [([a], b'TQ') for a in ('--help', '--version', '--copyright', '-u', '-?', '-z', '--unknown', '--dog', '-de', '-ed')]
    commands += [(['-d'], x) for x in (b'A', b'TQ==', b'TQ', b'AA=A', b'A===', b'=AAA', b'====', b'AA==bad', b'QUJD!A', b'A\xffAA', b'T Q=\x00=ignored', b'AA\x1fAA')]
    commands += [(['-dn'], b'QUJD!A'), (['-d', 'missing'], b''), (['-d', '.'], b''),
                 (['in', '.'], b'TQ'), (['missing', '-', 'extra'], b''), (['-', '-'], b'TQ'), (['-e-', 'version'], b'TQ')]
    out = []
    for args, data in commands:
        with tempfile.TemporaryDirectory() as cwd:
            Path(cwd, 'in').write_bytes(data)
            proc = subprocess.run(['base64', *args], executable=exe, input=data, capture_output=True, timeout=3,
                                  cwd=cwd, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
            out.append({'args': args, 'stdin_hex': data.hex(), 'exit': proc.returncode,
                        'stdout': proc.stdout.decode('latin1'), 'stderr': proc.stderr.decode('latin1')})
    return out


if __name__ == '__main__':
    print(json.dumps(collect(), ensure_ascii=True))
