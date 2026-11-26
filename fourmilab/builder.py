"""One-run OMP subscription companion; no tool approval, shell, or credential forwarding."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = '0b946702f73b4bc2d478348bc57b5e011a3a02423ca552ae0e2176c9c5f3df22'
MODEL = 'openai-codex/gpt-6.2-astra'
EFFORT = 'medium'
COMMAND = 'fourmilab-compatibility-build'
ERRORS = {
    'approval': 'Interactive approval is required. The unattended builder did not answer or retry.',
    'profile': 'The fixed profile or exact subscription model was unavailable. No model substitution.',
    'provider': 'The pinned provider did not return a completed text-only answer. No retry or fallback.',
    'receipt': 'The model receipt did not match the pinned model and text-only contract.',
    'handoff': 'The builder could not complete the local RPC handoff. No answer was accepted.',
}


def connection(directory):
    """Operator-installed transport identity, never a worker request or command-line override."""
    path = directory / 'connection.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
        raise ValueError('Install the private candidate connection descriptor first.')
    doc = json.loads(path.read_bytes())
    if (not isinstance(doc, dict) or set(doc) != {'host', 'directory'}
            or not isinstance(doc['host'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', doc['host'])
            or not isinstance(doc['directory'], str) or not re.fullmatch(r'/tmp/fourmilab-compat\.[A-Za-z0-9]{8}', doc['directory'])):
        raise ValueError('Invalid private candidate connection descriptor.')
    payload = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode('ascii')
    return doc, hashlib.sha256(payload).hexdigest()


def identity():
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in ('builder.py', 'model_turn.ts')}


def check_route(omp):
    # Inspect quota through OMP, not provider tokens or its credential store.
    result = subprocess.run([omp, 'usage', '--provider', 'openai-codex', '--json'],
                            capture_output=True, timeout=20, check=True)
    quota = json.loads(result.stdout)
    rows = [r for r in quota['capacity']['openai-codex'] if r.get('meter') == 'chat' and r.get('durationMs') == 604800000]
    if len(rows) != 1 or rows[0]['accounts'] != 1 or not 0 <= rows[0]['usedAccounts'] < 0.90:
        raise ValueError('Codex subscription unavailable or at the weekly reserve.')


def _watch_rpc(stream, gate, catalog):
    # Never send a dialog response. Discard notifications locally without recording their text.
    passive = {'notify', 'setStatus', 'setWidget', 'setTitle', 'set_editor_text', 'cancel'}
    for raw in iter(lambda: stream.readline(262145), b''):
        try:
            if len(raw) > 262144 or not raw.endswith(b'\n'):
                gate.set()
                return
            event = json.loads(raw)
            if not isinstance(event, dict) or (event.get('type') == 'extension_ui_request'
                                               and event.get('method') not in passive):
                gate.set()
                return
            if (event.get('type'), event.get('id'), event.get('command')) == (
                    'response', 'fourmilab-catalog', 'get_available_commands') and not catalog.done():
                data = event.get('data')
                commands = data.get('commands') if isinstance(data, dict) else None
                catalog.set_result(event.get('success') is True and isinstance(commands, list)
                                   and any(isinstance(c, dict) and c.get('name') == COMMAND
                                           and c.get('source') == 'extension' for c in commands))
        except (UnicodeError, ValueError):
            gate.set()
            return


def turn(omp, model, evidence):
    if model != MODEL:
        raise ValueError('The requested model is not the fixed builder model.')
    check_route(omp)
    with tempfile.TemporaryDirectory(prefix='fourmilab-turn-') as name:
        tmp = Path(name)
        (tmp / 'input.json').write_text(json.dumps({'model': model, 'reference': TARGET, 'evidence': evidence}), encoding='utf-8')
        env = os.environ.copy()
        env['FOURMILAB_TURN_DIR'] = str(tmp)
        argv = [omp, '--mode', 'rpc', '--no-session', '--no-tools', '--no-context-files', '--no-skills',
                '--no-prewalk', '--no-title', '--model', model, '--thinking', EFFORT,
                '--system-prompt', 'You are a text-only Rust compatibility builder for Fourmilab Base64 1.5.',
                '-e', str(HERE / 'model_turn.ts')]
        # RPC submits only the catalogued local command; the model never runs inside a startup hook.
        # Never use the exchange directory as a process CWD: on Windows a lingering helper locks it.
        with subprocess.Popen(argv, cwd=HERE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            gate = threading.Event()
            catalog = Future()
            reader = threading.Thread(target=_watch_rpc, args=(process.stdout, gate, catalog), daemon=True)
            reader.start()
            receipt = {'error': 'handoff'}
            try:
                deadline = time.monotonic() + 270
                output = tmp / 'output.json'
                process.stdin.write(b'{"type":"get_available_commands","id":"fourmilab-catalog"}\n')
                process.stdin.flush()
                sent = False
                while not gate.is_set():
                    if not sent and catalog.done():
                        if not catalog.result():
                            receipt = {'error': 'profile'}
                            break
                        process.stdin.write(json.dumps({'id': 'fourmilab-turn', 'type': 'prompt',
                                                       'message': '/' + COMMAND}).encode('ascii') + b'\n')
                        process.stdin.flush()
                        sent = True
                    if sent and output.is_file():
                        if output.stat().st_size <= 300000:
                            receipt = json.loads(output.read_bytes())
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        break
                    gate.wait(0.1)
            finally:
                # EOF triggers OMP's documented RPC disposal path, including release of child resources.
                process.stdin.close()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    receipt = {'error': 'handoff'}
                reader.join(timeout=2)
            return {'error': 'approval'} if gate.is_set() else receipt


def serve():
    endpoint, endpoint_hash = connection(HERE.parent.parent)
    host, root = endpoint['host'], endpoint['directory']
    model = MODEL
    omp = shutil.which('omp')
    if not omp:
        raise ValueError('OMP is not installed.')
    version = subprocess.check_output([omp, '--version'], text=True, timeout=12).strip()
    check_route(omp)
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', host, 'python3', '-IB', root + '/app/fourmilab/channel.py', root]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as relay:
        def send(obj):
            relay.stdin.write(json.dumps(obj, ensure_ascii=True).encode('ascii') + b'\n')
            relay.stdin.flush()
        send({'model': model, 'effort': EFFORT, 'files': identity(), 'omp': version, 'connection': endpoint_hash})
        if relay.stdout.readline(64) != b'READY\n':
            raise ValueError('Desk did not accept the builder identity.')
        print('Ready for one Fourmilab run.', flush=True)
        failed = False
        for _ in range(4):  # at most three generations, followed by an explicit close
            line = relay.stdout.readline(300001)
            if line == b'END\n':
                return 2 if failed else 0
            if not line.endswith(b'\n') or len(line) > 300000:
                raise ValueError('Missing or oversized model request.')
            req = json.loads(line)
            if req.get('reference') != TARGET or req.get('model') != model or set(req) != {'id','model','reference','evidence'} or not re.fullmatch(r'[0-9a-f]{32}', req['id']):
                raise ValueError('Unexpected builder request.')
            try:
                reply = turn(omp, model, req['evidence'])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'Local builder error: {type(error).__name__}', flush=True)
                reply = {'error': 'handoff'}
            if 'error' in reply:
                code = reply['error']
                reply = {'error': code if isinstance(code, str) and code in ERRORS else 'provider'}
                print('Builder blocked: ' + ERRORS[reply['error']], flush=True)
                failed = True
            send({'id': req['id'], **reply})
        raise ValueError('Run exceeded its builder request limit.')


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(serve())
