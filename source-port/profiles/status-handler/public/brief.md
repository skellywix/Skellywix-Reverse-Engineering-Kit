# status-handler: proposal brief

Write one Rust source file (`main.rs` contents) that behaves exactly like the Python program in `original/status_handler.py`. Use only the Rust standard library, no threads, and no sockets. Return source text only: no Cargo manifest, build script, dependency, macro crate, command, or test file. It is compiled as a single file with `rustc --edition=2021 -C opt-level=2 -C target-feature=+crt-static -C relocation-model=static -F unsafe_code`, so the program is static and needs no shared library, and it runs as the only file in an otherwise empty container: there is no shell, interpreter, or other program to call. A source that mentions `process`, `Command`, `unsafe`, `asm`, `extern`, `libc`, `syscall`, `include`, `link`, `no_mangle`, `export_name`, `transmute`, `exec`, or `dlopen` anywhere (comments included) is recorded as `used_fallback` and cannot be exported.

## Transport (stdio, not a network server)

The program reads one raw HTTP request from standard input and writes one raw HTTP response to standard output, then exits 0. No socket is opened or needed: this exercises the real request parsing and response bytes of the handler over a fixed in-process stdio transport, not a listening server. It takes no arguments.

## Behavior

1. Read standard input until the first blank line (`CRLF CRLF`). The head is everything before that blank line and may be at most 8192 bytes. If input ends before a blank line, or the head is longer than 8192 bytes, the request is malformed. Ignore the body and every header line.
2. The first head line must be `METHOD SP TARGET SP HTTP/1.0` or `HTTP/1.1`, where METHOD is one or more uppercase ASCII letters and TARGET starts with `/` and holds only printable non-space ASCII (0x21 to 0x7E). Anything else is malformed: respond 400, body `bad request` and LF.
3. The path is TARGET up to the first `?` (if any). If the path is not exactly `/status`: respond 404, body `not found` and LF. This check comes before the method check.
4. If the method is not GET and not HEAD: respond 405, body `method not allowed` and LF, with the extra header `Allow: GET, HEAD`.
5. Otherwise respond 200, body `ok` and LF. For HEAD, send the same headers (including the GET `Content-Length`) and no body bytes.

Every response starts `HTTP/1.1 <code> <reason>` (reason: `OK`, `Bad Request`, `Not Found`, `Method Not Allowed`) and carries `Content-Type: text/plain; charset=utf-8`, `Content-Length: <body byte count>`, and `Connection: close`, each line ending in CRLF, then a blank line.

## How a result is judged

Each run is a fresh container with the request on standard input. The result is the exit status, standard error, and the parsed response (status code, all headers with names compared case-insensitively and in any order, body), framed as `http-v1` in `framing.md`. A response that is not a complete well-formed HTTP/1.1 response never matches. A timeout (10 seconds) or more than 64 KiB on a stream fails the run. The corpus covers each rule above; some cases are held back and are not in this packet.
