"""Fixed HTTP/1.1 status handler over a stdio transport. Reviewed owned example for the source-port verifier.

Transport: one raw HTTP request on standard input, one raw HTTP response on standard output. No socket is opened.
"""
import re
import sys

MAX_HEAD = 8192
REQUEST_LINE = re.compile(rb'([A-Z]+) (/[!-~]*) HTTP/1\.[01]')


def read_head(stream):
    """The request line and headers, or None when the request ends before a blank line or the head is over 8192 bytes."""
    data = b''
    while b'\r\n\r\n' not in data and len(data) < MAX_HEAD + 4:
        chunk = stream.read1(1024)
        if not chunk:
            break
        data += chunk
    head, separator, _ = data.partition(b'\r\n\r\n')
    return head if separator and len(head) <= MAX_HEAD else None


def respond(status, reason, body, extra=(), head_only=False):
    headers = [(b'Content-Type', b'text/plain; charset=utf-8'),
               (b'Content-Length', str(len(body)).encode('ascii')),
               (b'Connection', b'close')]
    headers.extend(extra)
    out = b'HTTP/1.1 %d %s\r\n' % (status, reason)
    for name, value in headers:
        out += name + b': ' + value + b'\r\n'
    return out + b'\r\n' + (b'' if head_only else body)


def handle(head):
    request = REQUEST_LINE.fullmatch(head.split(b'\r\n', 1)[0]) if head is not None else None
    if request is None:
        return respond(400, b'Bad Request', b'bad request\n')
    method, target = request.groups()
    if target.split(b'?', 1)[0] != b'/status':
        return respond(404, b'Not Found', b'not found\n')
    if method not in (b'GET', b'HEAD'):
        return respond(405, b'Method Not Allowed', b'method not allowed\n', [(b'Allow', b'GET, HEAD')])
    return respond(200, b'OK', b'ok\n', head_only=method == b'HEAD')


def main():
    sys.stdout.buffer.write(handle(read_head(sys.stdin.buffer)))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
