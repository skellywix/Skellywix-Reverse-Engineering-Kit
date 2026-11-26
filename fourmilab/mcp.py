"""Read-only stdio MCP for a single mounted public run view. No operator API, paths or run selector."""
import json
import sys
from pathlib import Path

VERSION = '2025-11-25'
NAMES = {'run_status': 'Read this run, pinned manifest and event history.', 'skill_catalog': 'Read the declared roles and skills for this run.'}


def answer(req, load):
    key = req.get('id')
    def error(code, msg):
        return {'jsonrpc': '2.0', 'id': key, 'error': {'code': code, 'message': msg}}
    if req.get('jsonrpc') != '2.0' or not isinstance(req.get('method'), str):
        return error(-32600, 'Invalid request.')
    method, params = req['method'], req.get('params', {})
    if key is None and method.startswith('notifications/'):
        return None
    if not isinstance(params, dict):
        return error(-32602, 'Expected parameters object.')
    if method == 'initialize':
        result = {'protocolVersion': VERSION, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fourmilab-run', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': n, 'description': d, 'inputSchema': {'type': 'object', 'properties': {},
                  'additionalProperties': False}, 'annotations': {'readOnlyHint': True}} for n, d in NAMES.items()]}
    elif method == 'tools/call':
        if params.keys() - {'name', 'arguments', '_meta'} or params.get('name') not in NAMES or params.get('arguments', {}) != {}:
            return error(-32602, 'Only the two argument-free read tools are available.')
        try:
            view = load()
            data = view['run' if params['name'] == 'run_status' else 'catalog']
            result = {'content': [{'type': 'text', 'text': json.dumps(data, separators=(',', ':'))}]}
        except (OSError, ValueError, KeyError):
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Pinned run view unavailable.'}]}
    else:
        return error(-32601, 'Method not found.')
    return {'jsonrpc': '2.0', 'id': key, 'result': result}


def main():
    while line := sys.stdin.buffer.readline(16385):
        if len(line) > 16384 or not line.endswith(b'\n'):
            raise SystemExit('Oversized or incomplete request.')
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError('Expected a request object.')
            response = answer(req, lambda: json.loads(Path('/view/run.json').read_bytes()))
        except (ValueError, TypeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON request.'}}
        if response is not None:
            print(json.dumps(response, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    main()
