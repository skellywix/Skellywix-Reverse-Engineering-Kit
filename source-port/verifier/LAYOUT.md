# Protected layout v1

Contract: `docs/superpowers/contracts/source-port-v1.md`. This file fixes where each party reads and writes. A change to any path, field, or permission below is a new version of this document.

Four owners share one protected root. No owner can write another's files.

```
<root>/
  registry/<project_id>/     operator writes; verifier reads; web reads public/ only
  approvals/<project_id>/    operator writes; verifier reads; web has no access
  queue/<project_id>/<attempt_id>/   web (control plane) writes; verifier reads
  results/<project_id>/      verifier writes; web reads; nobody else writes
  work/                      verifier-private scratch, removed after each attempt
```

Suggested POSIX owners: `operator:verifier` mode 0750 for `registry/` and `approvals/` (files 0640, never group- or other-writable: the verifier refuses them otherwise), `web:verifier` for `queue/`, `verifier:web` for `results/`, `verifier` only for `work/` (0700). The web user must not read `registry/*/verifier/` (held-out cases) or `approvals/`, except for the two named read-only objects in "Server gate projection". The held-out directory is separate so one directory ACL enforces this.

## Server gate projection (revision 2)

Where the web runs in a container, the portable layout in "Operator setup (revision 3)" is used instead of the symbolic links described here: `expected.json` has one physical home, `registry/<project_id>/gate/expected.json`, and the web is given the `gate` directory, never `verifier/`.

The Rust server needs two read-only facts to judge a download: that the operator approved this exact attempt, and the expected case digests. They are exposed as a projection, never as copies:

```
<web-root>/gate/<project_id>/approval  ->  <root>/approvals/<project_id>/                 (directory)
<web-root>/gate/<project_id>/expected.json  ->  <root>/registry/<project_id>/verifier/expected.json
```

So the server reads `gate/<project_id>/approval/<attempt_id>.json` (shape under "approvals" below) and `gate/<project_id>/expected.json` (`[{"id","held_out","expected_sha256"}]`, sorted by id, exactly as the verifier reads it). Both are symbolic links outside `registry/`, to the same backing objects the verifier reads. They are not copies and not caches: the server opens them by path on every evaluation, and removing or replacing an approval, or reinstalling `expected.json`, is visible at once. A single-file bind mount of `expected.json` is not supported (it pins the old inode). Where the web service runs in a container, mount only `<root>/approvals/<project_id>/` read-only; give it `expected.json` through the same ACL path, not by mounting `registry/<project_id>/verifier/`.

Permissions (POSIX ACL, fixture service identity `gate` = the Rust server's user; no other grant, no browser route, no model access, no live permission change by this code):

- `--x` (traverse only) for `gate` on `<root>`, `registry/`, `registry/<project_id>/`, `registry/<project_id>/verifier/`, and `approvals/`.
- `r--` for `gate` on `registry/<project_id>/verifier/expected.json` only.
- `r-x` for `gate` on `approvals/<project_id>/`, with a default ACL of `r--` so each new approval is readable; the files stay 0640 and never group- or other-writable.
- Nothing for `gate` on `verifier/corpus.json`, `verifier/expected_observables.json`, any other directory entry, or `work/`. Without `r` on `verifier/` the identity cannot list it, and without a grant it cannot read the held-out files it could name.

What this exposes: approvals hold only IDs and digests; `expected.json` holds only IDs, `held_out` flags, and result digests, never inputs or observables. Digests of low-entropy results can be guessed offline, so they are for server-side equality only: never place `gate/` data in a proposal packet, a model prompt, a browser response, or a log. The packet builder reads `registry/<project_id>/public/` only. Held-out input bytes and expected observables (`verifier/corpus.json`, `verifier/expected_observables.json`) are not in any packet and are not mounted or readable for the web process.

The web process otherwise writes only `queue/<project_id>/<attempt_id>/candidate.rs` on proposal and `job.json` after approval on compare, and reads `registry/<project_id>/public/` and `results/<project_id>/`. The verifier's input paths and profile pins are unchanged. The approval remains the operator's decision: the server treats it as authoritative only when all seven Target fields equal the values it recomputes from `public/profile.json` and the candidate bytes, and the verifier still refuses anything else.

## registry/\<project_id\>/ (operator-installed)

Only `line-count` and `status-handler` exist; the verifier compiles in both IDs, their recipes, and the SHA-256 of each `manifest.json`. A directory that differs from its pin, lists other files, or holds an unlisted file or a symbolic link is refused.

| File | Reader | Content |
| --- | --- | --- |
| `manifest.json` | verifier | `{"schema_version":1,"project_id","files":{path:sha256}}` over every file below, canonical JSON |
| `public/profile.json` | web, verifier | `{"schema_version":1,"id","name","language","kind","source_sha256","corpus_sha256","recipe_sha256","toolchain_sha256","required_cases"}` |
| `public/original/<file>` | web (packet), verifier | The frozen reviewed original source |
| `public/brief.md` | web (packet) | Public proposal brief. No held-out values |
| `public/framing.md` | web, verifier | The observable framing; pinned into `corpus_sha256` |
| `verifier/corpus.json` | verifier only | Cases with inputs, including held-out |
| `verifier/expected.json` | verifier only | `[{"id","held_out","expected_sha256"}]`, sorted by id |
| `verifier/expected_observables.json` | verifier only | The independently authored expected results the hashes come from |

`public/profile.json` is the web's source for the project list, `required`, and the Target digests. The verifier recomputes every field and refuses a mismatch. Copy only the files listed here; `.gitattributes` in the source tree is not installed.

Digests (all over exact bytes, no line-ending conversion):

- `source_sha256`: the original source file.
- `corpus_sha256`: SHA-256 of canonical JSON `{"schema_version":1,"files":{"public/framing.md","verifier/corpus.json","verifier/expected.json"}}` mapping each path to its SHA-256.
- `recipe_sha256`: canonical JSON of the project ID, kind, framing ID, and the fixed container recipe in `containers.py` (build script, argv, limits, every docker flag).
- `toolchain_sha256`: canonical JSON of the compiler and observer image IDs.
- `candidate_sha256`: the candidate bytes.

## approvals/\<project_id\>/\<attempt_id\>.json (operator-installed)

`{"schema_version":1,"target":{"project_id","attempt_id","source_sha256","candidate_sha256","corpus_sha256","recipe_sha256","toolchain_sha256"}}`. The verifier computes the Target itself and requires equality in all seven fields. Created after the operator reviews the exact bytes. Not reusable: the attempt ID is in the name and in the target.

## queue/\<project_id\>/\<attempt_id\>/ (web-owned)

- `candidate.rs`: the proposal, one UTF-8 Rust source file, 1 byte to 256 KiB, no NUL.
- `job.json`: `{"schema_version":1,"project_id","attempt_id","candidate_sha256"}`.

The verifier reads `candidate.rs` once and builds those bytes. A mismatch with `job.json` is refused. Nothing else in the queue is read or trusted. IDs: project `[a-z0-9][a-z0-9-]{0,62}`; attempt `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.

## results/\<project_id\>/ (verifier-written)

- `<attempt_id>/result.json`: `{"schema_version":1,"target":{...},"build_exit":int|null,"cases":[{"id","original_sha256","replay_sha256","candidate_output_sha256","used_fallback"}]}`. Written once, after approval and preflight (a `blocked` result is written after approval alone). `cases` is empty unless every case was measured: a failed or blocked run never leaves partial rows. `build_exit` is the compiler's real status, or null if no build completed. The export gate in the contract is the only judge; this file is evidence, not a verdict.
- `<attempt_id>/diagnostic.json`: `{"schema_version":1,"state","detail"}`, plain text, at most 2000 characters, built only from fixed phrases, the compiler's exit status, and case IDs. Safe to show as the blocker. It never carries compiler, Docker, or exception text, host paths, or program output.
- `runtime.json`: latest observation, replaced each time: `{"schema_version":1,"project_id","attempt_id","recipe_sha256","toolchain_sha256","state","detail"}`. `attempt_id` is null when the preflight failed before any attempt. Absent means `configured`.

`state` values: `exercised` (all cases measured, whatever they matched), `failed` (build error, timeout, output bound, unstable or disagreeing original, changed program, lost result), `unavailable` (no Linux, Docker, pinned image, or runsc runtime; or Docker itself failed), `blocked` (the fixed environment cannot establish a candidate-only runtime; see "Execution boundary"). A preflight failure writes `runtime.json` only: it does not claim the attempt. A `blocked` result is written before preflight and before anything runs: `build_exit` null, `cases` empty, nothing built or executed. It is a recorded refusal, never a receipt; the export gate has no rows to accept.

An attempt directory exists at most once. Refusals before the claim (bad approval, digest drift, unknown project, bad candidate) write nothing.

## Running

`cd source-port && python -B -m verifier --root <root> run <project_id> <attempt_id>` runs one attempt and exits 0 only when every case was measured. It takes no other input: no command, flag, or environment variable approves, selects a recipe, or disables containment. There is no listener.

## Execution boundary

Real runs use only fresh containers on the two preloaded image IDs, `--runtime=runsc --network=none --read-only --cap-drop=ALL --pids-limit`, user 65534, no writable mounts, bind-mounted read-only single files, and fixed argv. Order: original twice and expected check for every case, then one compile of the exact candidate bytes (`-F unsafe_code`), then the candidate once per case.

**Status: blocked in the fixed environment.** A runtime claim of "no fallback" needs a candidate-only runtime: no original program, no interpreter, no shell, no host network or mounts. The candidate role is pinned to `OBSERVER_IMAGE`, the same image the original runs `python3` on, and the compiler image carries the toolchain and a shell. The fixed environment preloads no other image, so `containers.candidate_runtime_blocker()` returns a reason and `run.py` records state `blocked` (above) instead of measuring. No weaker isolation, new tool, or live change is used to get around this. The blocker stays unconditional until `REVIEWED_CANDIDATE_ONLY_IMAGES` (empty) names a distinct image a reviewer has shown to hold only the candidate; a merely different image ID does not clear it. Adding one is a reviewed code change (new `IMAGES` entry, new `recipe_sha256`, new approvals) verified by the operator on the isolated Linux fixture.

When a candidate-only image is pinned, the invocation is bound as follows: the compiler's stdout bytes are hashed once (`Candidate program sha256` in the diagnostic detail), written once to the verifier-private work directory, re-hashed before every candidate run (a difference is `failed`, no rows), bind-mounted read-only as the only mount at `/candidate`, and started with `--entrypoint /candidate`. The candidate container holds no corpus, registry, queue, approval, or result. `--pids-limit=1` stops fork, not execve, and nothing is traced inside the sandbox, so the verifier does not observe that the container ran only that file beyond what Docker's fixed argv guarantees.

`used_fallback` is computed by `screen.py` from the exact candidate bytes and is advisory only: a hit (source names `process`, `Command`, `unsafe`, `asm`, `extern`, `libc`, `syscall`, `include`, `link`, `no_mangle`, `export_name`, `transmute`, `exec*`, or `dlopen`, comments included) records true and the gate refuses it, but a clean scan never records a success or substitutes for the runtime proof. The pattern is part of `recipe_sha256`.

## Static build and candidate-only runtime (revision 3, prepared, not reviewed)

The compile step adds `-C target-feature=+crt-static -C relocation-model=static`, and `elf.py` checks the compiler's output before anything is written or run: a little-endian x86-64 ELF64 executable whose program headers hold a loadable segment and no `PT_INTERP`. A loader-dependent program is a `failed` run with the fixed phrase "dynamically linked", `build_exit` 0, no rows, and no candidate run. The flags are part of `recipe_sha256`, so both profiles' `profile.json`, `manifest.json`, and pins were regenerated from the files on disk with `python -B -m verifier describe <id> profiles/<id>`; `corpus_sha256`, `source_sha256`, and `toolchain_sha256` did not change.

`candidate_image.py` generates the runtime image as a `docker load` archive from constants: a manifest and a configuration (linux/amd64, created 1970, no entrypoint, command, environment, user, or layer), USTAR headers with owner 0:0, mode 0644, and time 0. It holds no shell, interpreter, loader, library, original program, or file, so the candidate at `/candidate` is the only file in its container. `ARCHIVE_SHA256` is the digest of the generator's output and only detects a generator change; it is not a review.

`containers.py` still pins `CANDIDATE_IMAGE = OBSERVER_IMAGE` and `REVIEWED_CANDIDATE_ONLY_IMAGES` is still empty, so the blocker above is unchanged and no receipt can be issued. Preflight now also requires, for a candidate image distinct from the other two, the generated shape read from Docker (no layers, no entrypoint, command, or environment, linux/amd64). That is a check, not a review.

Human gate (exact bytes and identity), in order:

1. `python -B -m portsetup runtime-archive --output <file>` writes the archive (refuses an existing path).
2. On the Linux host: `docker load -i <file>`; read the ID Docker reports.
3. `python -B -m portsetup runtime-review --archive <file> --image <ID>` prints the generator facts, whether the file equals the generator's bytes, and Docker's facts for the image. It pins nothing.
4. A reviewer who accepts them edits `CANDIDATE_IMAGE` and `REVIEWED_CANDIDATE_ONLY_IMAGES` in `containers.py`, regenerates both profiles (the recipe digest changes), and re-approves every attempt. No command in this repository does this step.

## Operator setup (revision 3)

`cd source-port && python -B -m portsetup COMMAND` (no environment input, no listener, nothing executed, no command approves anything):

- `install --root <root>`: stages each profile, verifies it with the verifier's own loader, and only then renames it to `registry/<id>/`; creates `approvals/<id>/`, `queue/<id>/`, `results/<id>/`, and `work/` (0700). An existing profile is verified and left alone, never replaced or migrated, in either layout below.
- `check --root <root>`: verifies a root and writes nothing; reports the layout of each profile; exit 1 on any problem.
- `approval-review --root <root> <id> <attempt>`: prints the target, the candidate bytes (ASCII-escaped), the exact approval file content, whether an approval already matches, and whether a receipt is possible. It writes nothing; the operator writes `approvals/<id>/<attempt>.json` by hand.

Portable layout (installed by `install`, for a web service that runs in a container; this replaces the symbolic links and the hard link of earlier text, and needs no second tree, no link, no copy, and no sync). `expected.json` is one physical file, `registry/<id>/gate/expected.json`; it is the single authoritative file for the logical profile member `verifier/expected.json`, whose pinned name, digest, and manifest are unchanged. The loader accepts exactly one of two physical homes for it: the original `registry/<id>/verifier/expected.json` (an existing root keeps working untouched, reported as "original layout") or the portable `registry/<id>/gate/expected.json`. Both present, neither present, a symbolic link, an extra file, or an extra directory is refused. The portable layout also holds one empty directory, `registry/<id>/gate/approval/`, as the mount point for approvals; it is allowed only there and any file in it is refused. `describe` gives the same answer for either layout.

Mounts for the web container (read-only unless stated; mounts are the operator's, nothing here performs them): `registry/<id>/gate` at `/ports/gate/<id>` (its parent of `expected.json`), then `approvals/<id>` at `/ports/gate/<id>/approval` (nested, after the first), `registry/<id>/public` at `/ports/registry/<id>/public`, `queue/<id>` read-write, and `results/<id>` read-only. Never mount `registry/<id>/verifier`, `registry/<id>/manifest.json`, or `work/`; the web receives no held-out file. Replacing or removing `gate/expected.json` is seen by the next load as a digest or presence failure, and the verifier refuses before it writes anything. Ownership and ACLs stay the operator's: `install` sets modes only (directories 0750, files 0640, `work/` 0700).
