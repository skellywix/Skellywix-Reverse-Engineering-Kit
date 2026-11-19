"""MCP Streamable HTTP: JSON responses, one paired case, tools only; no SSE or sampling."""
import json

from case_state import Case, CaseError
from evidence import TOOL_SCHEMAS

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')


def rpc(case: Case, token: str, sid: str | None, header_version: str | None, message: dict) -> tuple[int, dict | None, dict]:
    case.authorize(token)
    req_id = message.get('id')
    if not (req_id is None or type(req_id) is int or isinstance(req_id, str) and 0 < len(req_id) <= 128):
        raise CaseError(400, 'Invalid message identifier.')
    method, params = message.get('method'), message.get('params', {})
    if message.get('jsonrpc') != '2.0' or not isinstance(params, dict) or not isinstance(method, str):
        raise CaseError(400, 'Expected a JSON-RPC request.')
    extra = {}
    if method == 'initialize':
        if req_id is None or sid or not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('clientInfo'), dict) or not isinstance(params.get('capabilities'), dict):
            raise CaseError(400, 'Invalid initialize request.')
        wanted = params['protocolVersion']
        version = wanted if wanted in VERSIONS else VERSIONS[0]
        extra['Mcp-Session-Id'] = case.begin(token, version)
        result = {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'skellywix-re-kit', 'title': 'Skellywix RE Kit', 'version': '1.0.0'}}
    else:
        case.session(token, sid, active=False)
        if header_version is not None and header_version != case.version:
            raise CaseError(400, 'Unsupported protocol version.')
        if 'id' not in message:
            if method == 'notifications/initialized':
                case.initialized(token, sid)
            elif not method.startswith('notifications/'):
                raise CaseError(400, 'Expected a request identifier.')
            return 202, None, {}
        if req_id is None:
            raise CaseError(400, 'Invalid request identifier.')
        if method == 'ping':
            result = {}
        else:
            case.session(token, sid)
            if method == 'tools/list':
                result = {'tools': TOOL_SCHEMAS}
            elif method == 'tools/call':
                tool, args = params.get('name'), params.get('arguments', {})
                if not isinstance(tool, str) or not isinstance(args, dict) or set(params) - {'name', 'arguments', '_meta'}:
                    return 200, _error(req_id, -32602, 'Invalid tool arguments.'), {}
                try:
                    row = case.agent_call(token, sid, tool, args)
                except CaseError:
                    raise
                except ValueError as exc:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps(row, separators=(',', ':'))}], 'structuredContent': row}
            else:
                return 200, _error(req_id, -32601, 'Method not found.'), {}
    return 200, {'jsonrpc':'2.0','id':req_id,'result':result}, extra


def _error(req_id, code: int, message: str) -> dict:
    return {'jsonrpc':'2.0','id':req_id,'error':{'code':code,'message':message}}
