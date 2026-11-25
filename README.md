# Skellywix RE Kit

**BUILD** compiles the Neverball 1.6.0 Rust port for Windows. The download stays locked until an independent Windows comparison qualifies the package. **INSPECT** identifies a file, compares two files, or checks a SHA-256 digest without running either file.

This project processes hostile input. Read [SECURITY.md](SECURITY.md) and [the design](docs/design.md) before deployment.

## Purpose

Skellywix RE Kit targets security research, cheat detection, defensive engineering, game preservation, and reverse-engineering education. These are product domains, not a claim that the current release accepts every binary or engine.

- The current release provides passive file metadata and a source-based Windows game port.
- Anti-cheat teams can use its passive evidence and bounded rebuild proof; it does not generate bypasses, attach to games, or modify memory.
- BUILD accepts only the original Neverball Windows ZIP. It is not a general-purpose binary runner.
- PE, ELF, IL2CPP, Unreal Engine 4, and Source 2 are intended curriculum and future passive-profile topics. The current inspector recognizes PE and ELF structure but has no engine-specific profile.
- Preservation groups can retain metadata and verified packages for supported profiles; arbitrary game ecosystems are not reconstructed.

Use the Kit only on files and systems you own or are authorized to analyze.

**Open the paired workspace:** [SKELLYWIX RE KIT](http://127.0.0.1:48784/). This is a private loopback address, not a hosted demo. Choose **INSPECT** for the file tools or **BUILD** for runs. The old inspection bookmark leads back to the same home. For a file-only installation, use the [standalone inspector](static-inspection/README.md); it needs no model credentials.

## Windows game profile: Neverball 1.6.0

The [Rust port](examples/neverball/) includes the simulation and desktop source, not the original executable or game data. See [NOTICE.md](examples/neverball/NOTICE.md) for the license and third-party notices.

1. On a Linux host, prepare the [release layout](build_desk/game_jobs.py): the source tree, vendored dependencies, original Windows ZIP, and SHA-256 manifest. Preload the pinned compiler image.
2. From the checkout, run `python3 -B -m build_desk.desk "$ROOT" --game-release "$RELEASE"`. Use an absolute, operator-owned, mode-0700 state directory for `ROOT` and the absolute staged release directory for `RELEASE`. The desk creates `$ROOT/desk.sock` and keeps game runs in `$ROOT/games`. It needs no model or account.
3. Forward `127.0.0.1:48784` over SSH to the private `desk.sock` and open the workspace link above. Forward only to loopback. Anyone who can reach that forward can operate the desk.
4. Choose the original Windows ZIP and select **Rebuild for Windows**. The address bar records the run ID. Reopen that address to see the same run without starting another one.

Compilation alone doesn’t enable **Download for Windows**. The operator must supply independent Windows observations for the original and the candidate, bound to that run and package. The game requires working OpenGL. Joystick input, title demos, and the full original options menu aren’t implemented.

## Active components

| Path | Role |
|---|---|
| [`build_desk/`](build_desk/) | Game build UI, upload checks, offline compilation, run journal, and Windows qualification checks. |
| [`static-inspection/`](static-inspection/) | Passive file identification, build comparison, and digest checking. |
| [`examples/neverball/`](examples/neverball/) | GPL-licensed Rust simulation, desktop game, build recipe, and tests for Neverball 1.6.0. |
| [`tools/dependency_candidates.py`](tools/dependency_candidates.py) | Records dependency candidates for review without installing them into the product. |
| [`tests/`](tests/) | Build, package, qualification, HTTP, and dependency checks. |

The general-purpose job dispatcher, arbitrary-job submission helpers, and execution broker are no longer shipped.

## Operating boundary

- **INSPECT** reads local bytes and never executes a file.
- **BUILD** accepts the exact Neverball Windows ZIP and one fixed request. It compiles operator-staged Rust source offline and never executes the upload.
- The optional INSPECT integrations are metadata-only: the operator-approved critic receives bounded labels and counts in one stateless call; a case-scoped key exposes four read-only tools and cannot load files or run programs.

Verification covers the named profiles and scenarios, not arbitrary executables, game engines, or complete gameplay parity.

## Assessed Decompile inventory

On 2026-11-24, the upstream `1llu50ry/Decompile` release `v0.0.1` (`a7b4baed…`) and then-current `main` (`e40f12bd…`) were inspected as untrusted source and data. Nothing from the project was installed, executed, registered, or loaded into a model. Its headline numbers are inventory counts, not proof that every entry is implemented, safe, or interoperable.

| Upstream area | Source-observed inventory | Skellywix disposition |
|---|---|---|
| Release, CLI, server, and setup | 39 release assets include 16 platform archives. Setup names 42 client slugs, writes only the selected clients, and defaults a non-interactive run to `claude_code`. Plugin manifests launch unpinned `decompile@latest`. | Documented only. No upstream installer, archive, CLI, server, client registration, or automatic update path is active. |
| MCP tools | 315 `server.tool(` call sites and 313 distinct literal tool names. HTTP authentication is optional, and the filesystem policy is open when `MCP_FS_ALLOW` is unset. | No runtime import. Passive parser behavior may be used only as a specification for independently implemented, bounded file analysis. |
| RE and debug backends | 66 catalog rows; 32 are explicitly phantom because no MCP server is published. Several remaining entries are placeholders, unrelated packages, or live-debug adapters. The tool bundle downloads latest or unpinned dependencies, writes `env.json`, and prepends paths to `PATH`. | Excluded as a bundle and as model-callable adapters. An offline analyzer needs its own source review, license check, immutable pin, fixed command, and resource boundary before admission. |
| Host bridges | Nine plugin directories: Ghidra, IDA Pro, Binary Ninja, Cheat Engine, dnSpy, ILSpy, ReClass.NET, x64dbg, and Frida. The listeners have no authentication and allow cross-origin requests. The Cheat Engine bridge decodes every request body with `loadstring`. | Excluded. The Kit exposes no bridge listener, process attach, debugger control, memory write, script execution, or host-database write-back. A future adapter may only ingest a human-created, offline JSON export and emit an inert annotation sidecar. |
| Native emulation | `emu_run`, `emu_decrypt_xor`, and `emu_arm64_run` are bounded interpreters over caller-supplied decoded instructions, not general byte decoders or Unicorn-compatible emulators. Their correctness suite was not run during the assessment. | Candidate for independent offline implementation and dedicated tests; no upstream emulator code is active. |
| Skills, agents, subagents, plugins, and prompts | The database contains 6,088 skills, 194 agents, 163 subagents, 193 Markdown plugin records, and 3,349 prompts. Its metadata says `with_source=no`; thousands of records have scrubbed or missing origin and license data. | Catalogued, not imported. Executable agent, subagent, plugin, and prompt definitions stay out. Only passive, license-verified, provenance-complete reference text can enter a future default-deny review. |
| Agent mesh and runtime harness | The upstream catalog includes mesh tools and program execution capabilities. | Excluded. The Kit exposes neither an agent mesh nor a general-purpose execution service. |

Safe candidates are independently implemented offline parsers for formats such as PE, ELF, Mach-O, DEX, WASM, ZIP/APK, and .NET; bounded entropy, string, hash, PDB/DWARF, packet-capture, and YARA analysis; bounded operand interpretation; human-created read-only exports; and license-verified inert reference material. Live attach, spawn, injection, memory editing, breakpoint control, arbitrary scripts, network interception, exploit or payload helpers, unpinned installation, mesh, hub, and model-supplied code execution remain outside the product.

## Requirements

- Linux Docker host with the `runsc` runtime registered.
- Python 3.12 or newer.
- The pinned offline compiler image identified in [`build_desk/game_jobs.py`](build_desk/game_jobs.py), provisioned separately.

The standalone file inspector has its own launcher and requirements in [`static-inspection/README.md`](static-inspection/README.md).

## Checks

Run the bounded profile contracts from the repository root:

```sh
sudo python3 -IB -m unittest discover -s tests -t . -v
```

Run the passive inspector contracts from its directory:

```sh
cd static-inspection
python3 -IB -m unittest discover -s tests -v
```

Both suites target Linux because they exercise Unix sockets and Linux file-descriptor controls; the root suite is not a Windows compatibility check.

The inspector README lists its browser regression. See the [Neverball README](examples/neverball/README.md) for simulation and desktop checks.

## Dependency review

The weekly **Dependency candidates** workflow downloads candidate bytes on a disposable GitHub runner, records versions and SHA-256 digests, and retains only the receipt. It does not run candidate programs, push pin changes, publish images, or deploy.

`tools/dependency_candidates.py::REA_BASELINE_VERSION` is the explicit reviewed REA promotion baseline. A promotion must update that constant in the same reviewed change; the candidate job does not infer promotion from an untrusted staged receipt.

See [SECURITY.md](SECURITY.md) for the trust boundary and [the design](docs/design.md) for the current architecture.
