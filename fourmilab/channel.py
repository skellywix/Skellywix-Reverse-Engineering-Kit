"""SSH stdio relay to the candidate's private builder socket. No operator API."""
import os
import selectors
import socket
import sys
from pathlib import Path


def relay(root):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock, selectors.DefaultSelector() as sel:
        sock.connect(str(Path(root) / 'builder.sock'))
        sel.register(0, selectors.EVENT_READ)
        sel.register(sock, selectors.EVENT_READ)
        while True:
            ready = sel.select(timeout=900)
            if not ready:
                raise TimeoutError('Builder connection idle.')
            for key, _ in ready:
                if key.fileobj == 0:
                    chunk = os.read(0, 65536)
                    if not chunk:
                        return
                    sock.sendall(chunk)
                else:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()


if __name__ == '__main__':
    relay(sys.argv[1])
