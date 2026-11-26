# Security policy

## Scope

Skellywix RE Kit processes untrusted files, browser requests, staged release data, compiler output, and qualification evidence.

The workspace separates these operations:

- **File inspection** reads bounded local files and reports metadata without executing them.
- **Build** accepts Rust, C/C++, Python, and static-site projects by format, not by title or checksum. Native build instructions can execute inside the compiler sandbox. Python and website packages are data. Downloaded output is untrusted; a successful build is not a safety or compatibility verdict.
- **Compatibility** is separate from compilation. The Neverball profile checks the original Windows ZIP, staged source, and integrity of each download. Only an independent, run-bound Windows receipt can verify gameplay. The Fourmilab service is limited to its pinned Base64 reference and an explicitly provisioned text-only companion. Convert accepts source proposals for registered projects, not commands or approval from the browser.

## Trust boundary

Trusted components are the Linux host, Docker, gVisor, the private operator socket, pinned compiler image, coordinator, and operator-controlled Windows qualification process. The compiler runs in a networkless container with read-only inputs and no model credentials.

The Neverball compiler runs offline. It never starts the uploaded game or candidate executable on the host. Downloading a completed build does not certify it. The web interface cannot issue a Windows verification receipt.

The public-facing process binds to loopback; the workers communicate through private Unix sockets. Anyone who can reach that loopback port can operate the desk. Use a dedicated single-user host. Do not expose it on a LAN or public interface.

## Controls

- The dependency updater resolves the checkout's declared upstream channels into exact image digests, package locks, and release commits before activation. It trusts those publishers for future releases, not just the versions tested when this Kit was packaged. Failed resolution or verification leaves the current selection in place. Operator-owned originals, compatibility approvals, and the host aren't updated.
- CI trusts GitHub’s `actions/checkout`, `actions/setup-python`, and `actions/upload-artifact` publishers. Their moving major-version tags can change which code runs without a Kit commit. These workflows use disposable GitHub-hosted runners with read-only repository permissions, disable persisted checkout credentials, and do not deploy. A recorded action commit identifies a check; it isn’t an independent audit of that code.
- No caller-supplied sandbox command or network destination.
- gVisor, read-only roots and mounts, dropped capabilities, `no-new-privileges`, and resource limits.
- Origin and host checks on browser requests.
- Optional inspector integrations are operator-initiated and metadata-only: one stateless, redacted critic call or four read-only case tools behind a revocable, expiring folio key. Neither path can load or execute a specimen.
- Exclusive file creation, bounded input and output, and immutable per-attempt inputs.
- Neverball downloads require an intact package that matches the run. A gameplay claim additionally requires matching Windows observations.
- Source builds and corrections use server-assigned project and run IDs. A fix refers to a later build in the same project and retains both artifact identities. A user's retest note is not an independent compatibility or safety verdict. Convert and Fourmilab retain their separate download gates.
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
