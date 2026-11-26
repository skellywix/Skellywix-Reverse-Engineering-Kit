# line-count: proposal brief

Write one Rust source file (`main.rs` contents) that behaves exactly like the Python program in `original/line_count.py`. Use only the Rust standard library. Return source text only: no Cargo manifest, build script, dependency, macro crate, command, or test file. It is compiled as a single file with `rustc --edition=2021 -C opt-level=2 -C target-feature=+crt-static -C relocation-model=static -F unsafe_code`, so the program is static and needs no shared library, and it runs as the only file in an otherwise empty container: there is no shell, interpreter, or other program to call.

## Behavior

- The program takes no arguments. Run with none, it reads standard input to end of file as raw bytes and counts the LF bytes (0x0A). It writes the decimal count, then one LF, to standard output, and exits 0.
- A carriage return is not a line break. Bytes other than LF, including NUL and values above 0x7F, are never an error. Input can be larger than any buffer you choose, and can be empty.
- With any argument at all, it reads nothing, writes `usage: line-count` and one LF to standard error, writes nothing to standard output, and exits 2.
- Nothing else is written to either stream.

## How a result is judged

Each run is a fresh container with the given arguments and standard input. The result is the exit status plus the complete bytes of standard output and standard error, framed as `cli-v1` in `framing.md`. A timeout (10 seconds) or more than 64 KiB on a stream fails the run. The program has no network and no files to read. The build forbids `unsafe_code`, and a source that mentions `process`, `Command`, `unsafe`, `asm`, `extern`, `libc`, `syscall`, `include`, `link`, `no_mangle`, `export_name`, `transmute`, `exec`, or `dlopen` anywhere (comments included) is recorded as `used_fallback` and cannot be exported.

The corpus covers each rule above. Some cases are held back and are not in this packet.
