# Skellywix RE Kit design

## Scope

Skellywix RE Kit pairs two local operator surfaces:

1. **BUILD** compiles the staged Neverball 1.6.0 Rust port for Windows and records the evidence used to qualify its download.
2. **File inspection** identifies and compares local files without executing them.

The game desk has one build profile. A browser cannot select a command, model, network destination, source tree, or different target program.

## Product surfaces

The build desk serves its UI and game routes through a private Unix socket. The operator reaches it through a loopback-only SSH forward. A durable journal retains each game run’s transitions, build and package hashes, and qualification state. A saved link selects an existing run without starting another.

The file inspector runs as a paired local worker and server. It keeps only bounded metadata for two uploaded files and exposes identification, comparison, and digest checks. Its worker never executes a submitted file.

Two optional operator-controlled integrations remain inside that metadata boundary. The critic is off by default and can make one stateless model call with check labels, actors, outcomes, counts, and event numbers but no file bytes, paths, digests, keys, or sockets. A case-scoped metadata-agent key exposes four read-only case tools; it cannot load files, start a folio, pair itself, or execute a program, and it expires after a file change, 15 minutes, 32 calls, or explicit revocation.

## Neverball build and qualification

The desk accepts only the pinned original Windows ZIP and fixed request. [`game_jobs.py`](../build_desk/game_jobs.py) checks the staged source and dependency manifest, compiles the Rust game in a networkless container, and journals the candidate package. Download stays locked until a separate Windows qualification receipt binds the original and candidate observations to that run. The web interface cannot issue a receipt.

## Execution boundary

The compiler has a fixed command, a pinned image, read-only inputs, no network, and bounded resources. It does not execute either Windows program. The desk streams the upload into a hash instead of installing it. The browser receives neither a shell nor a model connection.

The operator socket and `games/` journal have separate single-owner locks. Recovery detects incomplete runs rather than releasing an unverified package. The dependency candidate workflow does not deploy or change a running game.

## Third-party capability intake

The Decompile `v0.0.1` release and then-current `main` were assessed as untrusted source and data. No installer, platform bundle, server, client registration, host plugin, agent definition, prompt, mesh component, tool bundle, or catalog record entered the active release.

Admission is default-deny. A candidate must have a verifiable origin and license, an immutable source pin, an independently reviewed implementation, a fixed passive operation, bounded input and resources, no network, and tests at the file boundary. Host applications may contribute only human-created offline exports; the Kit has no host listener or write-back path. Live process control, memory access, arbitrary scripts, model-supplied code, agent-to-agent prompt channels, and unpinned dependency installation remain outside the architecture.

## Threat model

Files, browser requests, release inputs, compiler output, and qualification evidence are untrusted. Relevant failures include parser defects, resource exhaustion, substituted bytes, stale receipts, cross-origin browser requests, and container escape.

Controls include fixed hashes and commands, private Unix sockets, loopback-only forwarding, origin checks, bounded uploads, package and receipt verification, and networkless compilation.

## Accepted limitations

- gVisor reduces kernel attack surface but is not a virtual-machine boundary.
- A verified rebuild covers the named behavior contract, not every input or implementation.
- BUILD requires no model or provider. Optional inspector integrations are separate.
- File signatures identify byte prefixes only and do not establish that a file is safe.
- Host debugger control, process attachment, memory editing, arbitrary execution, and autonomous exploit-development workflows are outside this design.
