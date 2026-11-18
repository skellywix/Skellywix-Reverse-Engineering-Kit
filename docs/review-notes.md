# Review notes

## 2026-11-18: cross-provider adversarial review

Earlier reviews of the container code ran on OpenAI Codex models. This review was run with
Claude Code (Anthropic), which closes the open cross-provider gap.

**Method.** Six scoped reviewers covered:

- the dispatcher;
- the executor;
- the agent side: runner, prompt and OMP profile;
- the SSH clients;
- infrastructure: Dockerfile, Compose, Squid and the systemd unit;
- CI, tests and docs.

Each reviewer had to reproduce its findings with scratch tests. An independent verifier per
dimension then tried to refute every finding. Three research agents checked external facts
against primary sources: OMP v18.22.0 and REA 6.8.0 source; Docker, gVisor, Squid 6.14 and
systemd behaviour; and CVEs plus newer releases for every pin. A final review covered the
implemented diff.

### Fixed

- **Claim race (critical).** The dispatcher renamed whatever `inbox/<id>` was at claim time. A
  `relab` member could swap a directory for a symlink (`RENAME_EXCHANGE`), and later root
  operations would then chmod or read host files through it. Claims now pin `O_NOFOLLOW`
  descriptors, move the upload into a root-only work directory, and verify the moved inode.
- **Verified inputs could change after verification.** Hashing happened in place on a tree the
  uploader still owned, and open descriptors survived chown/chmod. Admission now locks every
  directory before listing it and copies each file while hashing the copied bytes.
- **Crash loops.** A reused job id, an inbox entry that vanished mid-scan, or ENOSPC while
  writing results could each stop the queue forever. These are now quarantined or isolated per
  job. The unit has start limits.
- **Unbounded storage.** Agent and sandbox writes had no budget. A storage reserve now pauses
  claiming and contains running jobs.
- **Containment gaps:**
  - containers survived `systemctl stop`;
  - a timeout inside the Docker client's create window raised a false containment failure;
  - a container was never proven gone after a normal client exit;
  - deep output trees defeated cleanup.

  All four are fixed.
- **Forgeable results.** Agent-written `status.json` fields reached `result.json` and the
  terminal of anyone running `re_fetch`. Results are now built from dispatcher facts, and
  `re_fetch` escapes everything it prints.
- **Redaction.** Redaction skipped evidence by file suffix, including the prompt's own
  `.c` example, and short env values corrupted reports. Every file, name and result field is
  now redacted (exact, hex and base64 forms), and short values are ignored.
- **DNS exfiltration channel.** Squid resolved any requested name before checking the
  allowlist. Unlisted names are now refused first; this was verified live with a logging
  resolver. Special-purpose destination ranges and client source checks were added, and the
  proxy no longer reports its version.
- **Agent runtime:**
  - OMP's web search, fetch and browser tools were on by default; they are now disabled;
  - REA's MCP calls had a 30 s timeout against a 330 s first-query budget;
  - `--max-time` expiry was reported as `done`;
  - a missing prompt or profile ran an unhardened agent silently;
  - files planted in `out/` could crash the runner or redirect its writes.
- **Clients.** Job ids had 16 bits of entropy, `mv` merged uploads into existing jobs, failed
  uploads were left behind, and fetch raced publication.
- **CI.** Checkout credentials were persisted. Actions were pinned to the removed Node 20
  runtime. The hadolint image was mutable. Test dependencies were undeclared, and the run used a
  single interpreter.
- **Pins.** APT snapshot, Ghidra, radare2, YARA-X, Wasmtime and Node.js were bumped for
  published security fixes. Every hash was re-verified.

### Residual risks

These are documented in SECURITY.md:

- model keys in the agent container;
- the soft cost cap;
- best-effort redaction;
- the prompt-only "never execute in the agent" rule;
- execution-policy rules being a safety net rather than a boundary;
- known Ubuntu `tshark` and `p7zip` parser bugs, which only run inside gVisor.

Images and the dispatcher changed, so the runtime host needs requalification before untrusted
work resumes. The README lists the checks.
