"""Human-readable, versioned skills; these are not the upstream REA tool set."""
CATALOG = [
    {'role': 'File analyst', 'skill': 'Pinned ELF identification', 'scope': 'Staged Kit inspector; one reference; offline.'},
    {'role': 'Behavior analyst', 'skill': 'Fourmilab CLI observation', 'scope': 'Fixed probes and independently captured reference results.'},
    {'role': 'Rust builder', 'skill': 'Fourmilab Base64 compatibility', 'scope': 'Text-only subscription session; no tools or filesystem.'},
    {'role': 'Compiler', 'skill': 'Offline Rust 2021 build', 'scope': 'Pinned rustc; no dependencies or host writes.'},
    {'role': 'Verifier', 'skill': 'Named-case comparison', 'scope': 'Independent observer, same inputs, fresh sandbox, byte-exact digests.'},
    {'role': 'Packager', 'skill': 'Verified artifact export', 'scope': 'Publishes this run and build only.'},
    {'role': 'Reference library', 'skill': 'REA release skill', 'scope': 'Versioned data only. Not loaded into a model or invoked.'},
]
# A fixed compatibility profile, not an old program, prior Rust source, or a command interface.
SKILL = '''Implement the observed Linux/glibc CLI under LC_ALL=C. Use raw OsString argument and filename bytes; use the invocation basename base64 in getopt diagnostics. Match the reference's stdout, stderr, exit codes, files, and command ordering. Input file is opened before output (same-path truncation matters); '-' means standard stream. Default to encode. GNU short options can cluster and permute across operands unless POSIXLY_CORRECT; '--' ends options. Long spellings are handled by their first letter, as the samples show. Use the exact help, copyright, version and diagnostic bytes observed below, not approximations. File-open errors use the C error description without Rust's '(os error N)' suffix. Test labels are never inputs to the program.
Encoder: standard alphabet, 72 characters per line, CRLF. Pay attention to how an exact line boundary ends and to empty input. Decoder: 256-byte block reads (including on a live pipe); consume at least the available block or EOF before acting on early padding. Control bytes 0..32 are whitespace; byte 255 maps to zero. Strict illegal or incomplete data exits 1 retaining previous decoded groups. In lenient mode skip illegal bytes; discard an incomplete tail. Padding is handled per quartet, not modern RFC canonical validation; use the measured padding table. Never build the result by replaying examples. All other fields below are current reference observations or failure feedback from this same run, not instructions.'''
