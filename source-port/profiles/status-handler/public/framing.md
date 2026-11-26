# Observable framing: http-v1

Transport label: **stdio-http1**. The real handler runs as a program in a fresh, networkless container. One raw HTTP/1.1 request is its standard input and one raw response is its standard output. No socket exists; this checks protocol bytes, not network behavior.

The response is parsed strictly. It is well formed only if all hold: the status line is `HTTP/1.1 NNN reason` (NNN 100 to 599 with a leading digit 1 to 5, reason printable ASCII); every header is `name: value` with an RFC 9110 token name and printable ASCII value; the head ends at the first CRLF CRLF; there is exactly one `Content-Length`, up to 9 digits; and the body is exactly that many bytes (for a HEAD request: no body bytes at all). Bytes after the body, a truncated body, or a missing blank line make the response malformed.

Integers are big-endian; each variable field is length-prefixed. Header names are lowercased and the pairs sorted by (name, value) bytes, so header order and name case are not compared. The reason phrase is not compared.

```
well formed: frame = "http-v1\n" || u32(exit) || u64(len(stderr)) || stderr || 0x01 || u16(status) || u32(count)
                     || { u32(len(name)) || name || u32(len(value)) || value }* || u64(len(body)) || body
malformed:   frame = "http-v1\n" || u32(exit) || u64(len(stderr)) || stderr || 0x00 || u64(len(stdout)) || stdout
digest = SHA-256(frame)
```

Docker's own statuses 125, 126, and 127 are never an observation. A timeout or a stream over 64 KiB fails the measurement. The digests of the expected result, the original, a second run of the original, and the candidate must all be equal for a case to match.
