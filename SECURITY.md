# Security policy

## Scope

Skellywix RE Kit processes untrusted files, browser requests, staged release data, compiler output, and qualification evidence.

The active product has two input boundaries:

- **File inspection** reads bounded local files and reports metadata without executing them.
- **BUILD** accepts only the hash-locked Neverball Windows ZIP and fixed request, and compiles operator-staged Rust source. It does not accept arbitrary commands, programs, or model prompts.

## Trust boundary

Trusted components are the Linux host, Docker, gVisor, the private operator socket, pinned compiler image, coordinator, and operator-controlled Windows qualification process. The compiler runs in a networkless container with read-only inputs and no model credentials.

The Neverball compiler runs offline. It never starts the uploaded game or candidate executable on the host. Windows qualification is a separate trusted operator step. The web interface cannot create the receipt that unlocks the download.

The operator desk is served through a Unix socket and intended to be forwarded only to a loopback address. Anyone who can reach that forward can operate the desk. Do not expose it on a LAN or public interface.

## Controls

- Fixed request, original distribution, compiler, recipe, and dependency pins.
- No caller-supplied sandbox command or network destination.
- gVisor, read-only roots and mounts, dropped capabilities, `no-new-privileges`, and resource limits.
- Origin and host checks on browser requests.
- Optional inspector integrations are operator-initiated and metadata-only: one stateless, redacted critic call or four read-only case tools behind a revocable, expiring folio key. Neither path can load or execute a specimen.
- Exclusive file creation, bounded input and output, and immutable per-attempt inputs.
- Run-bound Windows observations and matching package digests before download.
- Download packages only for verified attempts.
- Passive file signatures that do not claim a file is safe.
- Default-deny third-party intake: no external installer, MCP server, client registration, host bridge, agent, prompt, plugin, mesh, or unpinned tool bundle is active. Reference material needs verified provenance and licensing; executable capability needs an independent bounded implementation and file-boundary tests.

## Accepted risks

- gVisor reduces kernel attack surface but is not a virtual-machine boundary.
- A malicious file may exploit a parser before the file is rejected.
- A receipt establishes that the trusted qualification process recorded agreement. It cannot prove that an untrusted operator ran the comparison.
- The optional inspector critic sends limited metadata to its configured provider only at the operator’s request.
- A verified build covers its named cases, not all gameplay, hardware, or input devices.

Host debugger control, process attachment, memory editing, arbitrary binary execution, and autonomous exploit-development workflows are outside the active product boundary.

## Report a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab and select **Report a vulnerability**. Include affected versions, reproduction steps, impact, and any suggested mitigation. Do not disclose the issue publicly before maintainers have had time to assess it.
