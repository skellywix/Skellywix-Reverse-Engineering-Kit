# Skellywix RE Kit design

## Scope

The [launcher](../tools/workspace.py) serves Build, Inspect, Reconstruct, Convert, Runtime, and Releases at one loopback origin. Build chooses a toolchain by project format; it has no game-title or application allowlist. A compiled executable without source is not a source project.

Ordinary builds and compatibility runs make different claims. Ordinary builds return compiled or packaged output. A compatibility run compares a named original and candidate under a defined contract. Neverball and Fourmilab are examples of the latter, not admission rules for the former.

## Product surfaces

The Rust front end forwards only the named build, project-history, and maintenance routes to the private Build socket. Each project run owns its inputs, events, resulting ZIP, and hashes. A saved link reads that run without resubmitting it. Picking another input clears the old download. The separate Windows journal distinguishes a completed build from independent gameplay verification.

The file inspector runs as a paired local worker and server. It keeps only bounded metadata for two uploaded files and exposes identification, comparison, and digest checks. Its worker never executes a submitted file.

Two optional operator-controlled integrations remain inside that metadata boundary. The critic is off by default and can make one stateless model call with check labels, actors, outcomes, counts, and event numbers but no file bytes, paths, digests, keys, or sockets. A case-scoped metadata-agent key exposes four read-only case tools; it cannot load files, start a folio, pair itself, or execute a program, and it expires after a file change, 15 minutes, 32 calls, or explicit revocation.

## Source builds and updates

The [project service](../build_desk/project_jobs.py) validates the input, selects a recipe, and compiles Rust or C/C++ in a networkless gVisor container. A build script runs only inside that sandbox. Python and static-site profiles package source or site files without running them. None of the four profiles runs the delivered application or compares it with an original.

The first run supplies a persistent project ID; later builds join it by explicit selection, never by filename, project name, or checksum. Legacy ungrouped runs remain separate projects. A mistake report records expected and observed behavior against a terminal run. A retest records its own run, hashes, result, and note without erasing the original observation. Building again does not close a report.

The [maintenance service](../build_desk/maintenance.py) reads the [publisher policy](../workspace-dependencies.json). At bootstrap or update, it resolves compatible packages, image channels, and stable GitHub releases into a private set. The browser supplies no URL, image, or command. Before switching the active pointer, the [offline workspace builder](../build_desk/workspace_build.py) compiles the shell and both interfaces with the staged set’s image IDs, Cargo vendor tree, and npm installations. The launcher uses the same build, keyed by the checkout sources, build recipe, and resolved inputs. A failed build leaves the working selection unchanged. Normal jobs don’t look up upstream versions. Decompile and rust-anything contribute only tag and commit metadata; the three REA files remain data. An update doesn’t change original distributions, compatibility approvals, or previous run evidence.

## Neverball builds and optional gameplay checks

The Neverball example accepts only its original Windows ZIP and fixed request. [`game_jobs.py`](../build_desk/game_jobs.py) checks the staged source and dependency manifest, compiles the Rust game in a networkless container, and journals the candidate package. An intact completed ZIP can be downloaded without a play test. Only a separate Windows receipt binding the original and candidate observations to that run can change the verification claim. The web interface cannot issue a receipt.

## Execution boundary

The Windows compiler uses fixed arguments, an immutable image, read-only inputs, no network, and bounded resources. It does not execute either Windows program. The desk hashes the uploaded original instead of installing it. Neither the source builder nor the Windows compiler gives the browser a host shell or model connection.

The operator socket and `games/` journal have separate single-owner locks. Recovery detects incomplete runs; an incomplete or altered package is not available for download. The dependency candidate workflow does not deploy or change a running game.

## Third-party capability intake

The Decompile `v0.0.1` release and then-current `main` were assessed as untrusted source and data. No installer, platform bundle, server, client registration, host plugin, agent definition, prompt, mesh component, tool bundle, or catalog record entered the active release.

Admission is default-deny. A candidate must have a verifiable origin and license, an immutable source pin, an independently reviewed implementation, a fixed passive operation, bounded input and resources, no network, and tests at the file boundary. Host applications may contribute only human-created offline exports; the Kit has no host listener or write-back path. Live process control, memory access, arbitrary scripts, model-supplied code, agent-to-agent prompt channels, and unpinned dependency installation remain outside the architecture.

## Threat model

Files, browser requests, release inputs, compiler output, and qualification evidence are untrusted. Relevant failures include parser defects, resource exhaustion, substituted bytes, stale receipts, cross-origin browser requests, and container escape.

Controls include fixed hashes and commands, private Unix sockets, loopback-only forwarding, origin checks, bounded uploads, package and receipt verification, and networkless compilation.

## Accepted limitations

- gVisor reduces kernel attack surface but is not a virtual-machine boundary.
- A verified rebuild covers the named behavior contract, not every input or implementation.
- Build and passive inspection require no model or provider. The optional Fourmilab companion and the optional inspector critic have separate contracts.
- File signatures identify byte prefixes only and do not establish that a file is safe.
- Host debugger control, process attachment, memory editing, arbitrary execution, and autonomous exploit-development workflows are outside this design.
