"""Private, one-client, one-run text completion channel. Never mounted in a sandbox."""
import json
import os
import socket
import threading
import time
import uuid

from . import builder
from .sandbox import Cancelled, Unavailable
from .store import wire


class Broker:
    def __init__(self, root, model):
        endpoint, self.connection_hash = builder.connection(root)
        if endpoint['directory'] != str(root) or model != builder.MODEL:
            raise ValueError('This is not the installed candidate deployment.')
        self.model = model
        self.lock = threading.Lock()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(root / 'builder.sock'))
        os.chmod(root / 'builder.sock', 0o600)
        self.socket.listen(1)
        self.peer = None
        self.stream = None
        self.version = 'omp/19.1.1'
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            conn, _ = self.socket.accept()
            conn.settimeout(8)
            stream = conn.makefile('rb')
            try:
                line = stream.readline(4097)
                if len(line) > 4096 or json.loads(line) != {'model': self.model, 'effort': builder.EFFORT,
                                                            'files': builder.identity(), 'omp': self.version,
                                                            'connection': self.connection_hash}:
                    raise ValueError('Mismatched builder.')
                with self.lock:
                    if self.peer:
                        raise ValueError('Builder already connected.')
                    conn.sendall(b'READY\n')
                    self.peer, self.stream = conn, stream
            except (OSError, ValueError):
                stream.close()
                conn.close()

    def finish(self):
        with self.lock:
            if self.peer:
                try:
                    self.peer.sendall(b'END\n')
                except OSError:
                    # A disconnected model companion receives no more requests.
                    self.peer.close()
                finally:
                    self.stream.close()
                    self.peer.close()
                    self.stream = self.peer = None

    def generate(self, evidence, cancel):
        with self.lock:
            conn, stream = self.peer, self.stream
        if not conn:
            raise Unavailable('No matching model companion. Connect the subscription builder and retry this run.')
        tag = uuid.uuid4().hex
        req = {'id': tag, 'model': self.model, 'reference': builder.TARGET, 'evidence': evidence}
        try:
            conn.settimeout(285)
            conn.sendall((wire(req) + '\n').encode('ascii'))
            # Wait in small cancellable slices, then read a bounded response from this trusted companion.
            import select
            end = time.monotonic() + 285
            while time.monotonic() < end and not select.select([conn], [], [], 0.2)[0]:
                if cancel.is_set():
                    raise Cancelled('Cancelled while waiting for builder. No answer will be accepted.')
            left = end - time.monotonic()
            if left <= 0:
                raise Unavailable('Model response timed out.')
            conn.settimeout(left)
            raw = stream.readline(300001)
            if not raw.endswith(b'\n') or len(raw) > 300000:
                raise ValueError('No complete bounded builder receipt.')
            result = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise Unavailable('Subscription builder disconnected or sent an invalid receipt.') from exc
        if result.get('id') != tag:
            raise Unavailable('The provider receipt does not belong to this request.')
        if 'error' in result:
            code = result['error']
            message = builder.ERRORS.get(code, builder.ERRORS['provider']) if isinstance(code, str) else builder.ERRORS['provider']
            raise Unavailable(message)
        if (set(result) != {'id', 'actual_model', 'source', 'stop'} or result['actual_model'] != self.model
                or result['stop'] != 'stop' or not isinstance(result['source'], str)
                or not 0 < len(result['source'].encode('utf-8')) <= 80000):
            raise Unavailable('No completed response from the pinned builder model.')
        return result
