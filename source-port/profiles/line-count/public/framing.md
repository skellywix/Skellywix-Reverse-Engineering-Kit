# Observable framing: cli-v1

Transport: a command-line program in a fresh, networkless container. Arguments and standard input come from the case. There is no other input.

An observation is the program's exit status, the complete bytes of standard output, and the complete bytes of standard error. Integers are big-endian; each stream is length-prefixed.

```
frame = "cli-v1\n" || u32(exit_status) || u64(len(stdout)) || stdout || u64(len(stderr)) || stderr
digest = SHA-256(frame)
```

Compared fields: exit status, stdout bytes, stderr bytes, all exact. Docker's own statuses 125, 126, and 127 are never an observation: they are an unavailable sandbox. A timeout or a stream over 64 KiB fails the measurement; it is not a match on a prefix.

The digest of the expected result, of the original, of a second run of the original, and of the candidate must all be equal for a case to match.
