# Skellywix RE Kit

**Build** accepts games and applications by project format, not by title. Upload source to compile a Linux program or package a Python app or static site. **Inspect** compares files without running them. Neither a successful build nor a matching file size proves that two programs behave alike.

This project processes hostile input. Read [SECURITY.md](SECURITY.md) and [the design](docs/design.md) before deployment.

## Purpose

Skellywix RE Kit targets security research, cheat detection, defensive engineering, game preservation, and reverse-engineering education. These are product domains, not a claim that the current release accepts every binary or engine.

- Build Rust and C/C++ software without granting build scripts access to the host or network.
- Package Python source or an already-built static site. The workspace doesn't install the uploaded project's dependencies or run its output.
- Identify files, compare bytes, and check independent digests. A compiled binary without source belongs in Inspect, not the source builder.
- Use a separate compatibility profile when you need evidence against a specific original. Neverball is one such profile, not the admission rule for Build.

Use the Kit only on files and systems you own or are authorized to analyze.

## Analyze behavior for a rewrite

Use original source, observations from a running program, or both. For example, inspect the included line-count source without running it:

```sh
python -IB tools/semantics.py analyze --source source-port/profiles/line-count/public/original/line_count.py
```

The offline tool extracts facts from Python source and compares measured behavior from any language. Source-derived facts, observations, and hypotheses stay separate. You can share a behavior-only spec without sharing source, or keep the source in your workflow when its terms allow. The [semantic analysis guide](docs/semantic-analysis.md) includes a runnable comparison and its limits.

## Start the workspace

Use a non-root account on a single-user Linux x86-64 host with Python 3.12 or later, Docker Engine, gVisor registered as `runsc`, and the systemd socket tools. No host Rust or Node installation, model account, or existing case is needed for Build and Inspect.

```sh
git clone https://github.com/skellywix/Skellywix-Reverse-Engineering-Kit.git
cd Skellywix-Reverse-Engineering-Kit
python3 -B tools/workspace.py
```

Open the loopback URL printed by the launcher (default: <http://127.0.0.1:48784/>). The first start resolves dependencies from upstream and builds both interfaces. Later starts use the installed set without refreshing it. To use a different data directory or port, pass `--root` or `--port`. Export Inspect evidence before stopping: the current folio lives in memory. For file inspection alone, use the [standalone inspector](static-inspection/README.md).

## Build a game or application

| Build profile | Input | Download |
|---|---|---|
| Rust project | One `.rs` file or a Cargo project archive; include vendored dependencies when needed | Linux build output |
| C / C++ project | C or C++ source, or a CMake, Make, or configured release archive | Linux build output, if the installed compiler image has the required tools and libraries |
| Python application | A Python source file or project archive | Source package; no installed dependencies |
| Static website | An archive containing a site with `index.html`, including browser games | Site files; no frontend build step |

Choose **New project** (or select an existing project for another build), choose a profile and source file, then click **Build project**. Each project has its own history, even if two projects have the same name. The result shows the run ID and SHA-256 hashes of the input and output. Selecting another file or profile clears the old download. Unsupported input or missing dependencies produce an error, not a substitution. The workspace doesn't reconstruct arbitrary executables into source.

To track a mistake, select the project and the affected build under **Build history and corrections**. Record what you expected and what you observed. Build the correction in the **same project**, retest that output, and record **Still occurs** or **Confirmed fixed** with what you checked. A successful build alone never closes a report. The original observation and all retests stay in the private workspace data directory, not in GitHub.

If the desk cannot read a saved build, it warns that the history may be incomplete. It leaves the saved records untouched. Opening a build link keeps its project selected for the next revision, even while the linked build is running.

Click **Update all dependencies** on the front page to check the publishers directly. No new Kit commit is required. Expand **Component versions and update results** to see which versions are installed or tracked. Before switching, the updater builds the workspace shell and both interfaces offline with the candidate dependencies. It reuses a completed build when the sources and build inputs match. If a build fails, the previous installation stays selected. Updating doesn’t install host packages, fetch an uploaded project’s dependencies, or restart an open case. The browser supplies no URL or command.

## Windows game profile: Neverball 1.6.0

The [Rust port](examples/neverball/) includes the simulation and desktop source, not the original executable or game data. See [NOTICE.md](examples/neverball/NOTICE.md) for the license and third-party notices.

1. Follow the [Neverball setup guide](examples/neverball/README.md#optional-set-up-the-windows-rebuild-profile). The dependency updater builds the Windows compiler and installs the locked crates. Supply the original Windows release separately.
2. Start the workspace with the `--root` and `--game-release` paths printed by the preparation tool. Use a separate data root and port if another workspace is open.
3. Open **Windows example: Neverball 1.6.0**, choose the original Windows ZIP, and click **Rebuild for Windows**. Reopen the resulting address to see that run without starting another.

Download the intact Windows ZIP when the build finishes. **Built** means the port compiled; it does not mean anyone played it. An independent Windows comparison can verify a particular run, but it isn't required to build or download. The game requires working OpenGL. Joystick input, title demos, and the full original options menu aren't implemented.

## Active components

| Path | Role |
|---|---|
| [`build_desk/`](build_desk/) | Source builds, per-project corrections, downloads, dependency updates, and the separate Windows build profile. |
| [`static-inspection/`](static-inspection/) | Passive file identification, build comparison, and digest checking. |
| [`frontend/`](frontend/), [`src/`](src/) | Workspace shell and Inspect, Reconstruct, Convert, Runtime, and Releases views. |
| [`fourmilab/`](fourmilab/), [`examples/fourmilab-base64/`](examples/fourmilab-base64/) | Operator-provisioned compatibility service and separately buildable source example. |
| [`source-port/`](source-port/) | Reviewed Convert examples and their independent verifier. A proposal is not an approved or verified port. |
| [`examples/neverball/`](examples/neverball/) | GPL-licensed Rust simulation, desktop game, build recipe, and tests for Neverball 1.6.0. |
| [`tools/dependency_candidates.py`](tools/dependency_candidates.py) | Records dependency candidates for review without installing them into the product. |
| [`tests/`](tests/) | Build, package, qualification, HTTP, and dependency checks. |

Build accepts source projects but exposes no general-purpose command, debugger, or remote execution API.

## Operating boundary

- **INSPECT** reads local bytes and never executes a file.
- **Build** runs uploaded project build instructions only inside the isolated compiler. It doesn't run the downloaded application. The Neverball example checks its original release; it reports verified gameplay only when independent Windows evidence matches that run.
- The optional INSPECT integrations are metadata-only: the operator-approved critic receives bounded labels and counts in one stateless call; a case-scoped key exposes four read-only tools and cannot load files or run programs.

Verification covers the named profiles and scenarios, not arbitrary executables, game engines, or complete gameplay parity.

## Assessed Decompile inventory

On 2026-11-24, the upstream `1llu50ry/Decompile` release `v0.0.1` (`a7b4baed…`) and then-current `main` (`e40f12bd…`) were inspected as untrusted source and data. Nothing from the project was installed, executed, registered, or loaded into a model. Its headline numbers are inventory counts, not proof that every entry is implemented, safe, or interoperable.

| Upstream area | Source-observed inventory | Skellywix disposition |
|---|---|---|
| Release, CLI, server, and setup | 39 release assets include 16 platform archives. Setup names 42 client slugs, writes only the selected clients, and defaults a non-interactive run to `claude_code`. Plugin manifests launch unpinned `decompile@latest`. | The updater reads the latest stable tag and commit only. It doesn't download release assets or install the CLI, server, or client registrations. |
| MCP tools | 315 `server.tool(` call sites and 313 distinct literal tool names. HTTP authentication is optional, and the filesystem policy is open when `MCP_FS_ALLOW` is unset. | No runtime import. Passive parser behavior may be used only as a specification for independently implemented, bounded file analysis. |
| RE and debug backends | 66 catalog rows; 32 are explicitly phantom because no MCP server is published. Several remaining entries are placeholders, unrelated packages, or live-debug adapters. The tool bundle downloads latest or unpinned dependencies, writes `env.json`, and prepends paths to `PATH`. | Excluded as a bundle and as model-callable adapters. An offline analyzer needs its own source review, license check, immutable pin, fixed command, and resource boundary before admission. |
| Host bridges | Nine plugin directories: Ghidra, IDA Pro, Binary Ninja, Cheat Engine, dnSpy, ILSpy, ReClass.NET, x64dbg, and Frida. The listeners have no authentication and allow cross-origin requests. The Cheat Engine bridge decodes every request body with `loadstring`. | Excluded. The Kit exposes no bridge listener, process attach, debugger control, memory write, script execution, or host-database write-back. A future adapter may only ingest a human-created, offline JSON export and emit an inert annotation sidecar. |
| Native emulation | `emu_run`, `emu_decrypt_xor`, and `emu_arm64_run` are bounded interpreters over caller-supplied decoded instructions, not general byte decoders or Unicorn-compatible emulators. Their correctness suite was not run during the assessment. | Candidate for independent offline implementation and dedicated tests; no upstream emulator code is active. |
| Skills, agents, subagents, plugins, and prompts | The database contains 6,088 skills, 194 agents, 163 subagents, 193 Markdown plugin records, and 3,349 prompts. Its metadata says `with_source=no`; thousands of records have scrubbed or missing origin and license data. | Catalogued, not imported. Executable agent, subagent, plugin, and prompt definitions stay out. Only passive, license-verified, provenance-complete reference text can enter a future default-deny review. |
| Agent mesh and runtime harness | The upstream catalog includes mesh tools and program execution capabilities. | Excluded. The Kit exposes neither an agent mesh nor a general-purpose execution service. |

Safe candidates are independently implemented offline parsers for formats such as PE, ELF, Mach-O, DEX, WASM, ZIP/APK, and .NET; bounded entropy, string, hash, PDB/DWARF, packet-capture, and YARA analysis; bounded operand interpretation; human-created read-only exports; and license-verified inert reference material. Live attach, spawn, injection, memory editing, breakpoint control, arbitrary scripts, network interception, exploit or payload helpers, unpinned installation, mesh, hub, and model-supplied code execution remain outside the product.

## Requirements

See [Start the workspace](#start-the-workspace) for host requirements. Specialized profiles have additional inputs: for example, the original Windows game and the compiler for its port. Fourmilab reconstruction uses a separately provisioned desk (`--fourmilab-socket`). Runtime and Releases distinguish configuration from execution evidence.

The launcher installs the two fixed **Convert** examples into the private data directory. It does not approve a proposal or start a verifier. See the [operator procedure](source-port/verifier/LAYOUT.md#operator-setup-revision-3) for the independent approval, the candidate-only runtime review, and the receipt required for a download. For the optional [Fourmilab desk](fourmilab/README.md), supply the original program and connect the separate service. An unavailable profile is not a passing test.

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

## What updates

| Source | Update behavior |
|---|---|
| [Decompile](https://github.com/1llu50ry/Decompile), [rust-anything](https://github.com/erkamyaman/rust-anything) | Record the latest stable release tag and commit. Neither project runs inside the Kit. |
| [REA](https://github.com/morluto/rea) | Read the package declaration, lockfile, and authored skill at the latest stable release commit. Keep them as data. No npm install, agent registration, or model access. |
| Workspace compilers, Python, Node.js, inspector | Follow the publisher's stable Rust, Python 3, and Node.js LTS image channels. Resolve the native compiler packages from Debian. Running processes retain the image they started with. |
| Workspace Cargo and both npm projects | Resolve the latest versions compatible with the package declarations. Install from the resulting private locks, not the checkout's development locks. |
| CI | Use the declared GitHub Actions major release tags. The weekly **Dependency candidates** workflow records upstream observations on a disposable runner. It doesn't deploy. |

The [policy](workspace-dependencies.json) selects publishers. The private `dependencies/sets/<set>/resolution.json` and `receipt.json` record what was selected, so two installations can be compared. Original binaries, the Neverball compatibility recipe and locks, and independent verifier images are fixed inputs to specific comparisons. Existing approvals and results do not carry over to different inputs.

See [SECURITY.md](SECURITY.md) for the trust boundary and [the design](docs/design.md) for the current architecture.
