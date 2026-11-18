# Security policy

## Scope

This project performs static analysis and dynamic execution of untrusted binaries. Treat submitted files, questions, fetched URLs, model output, execution output, live-feed content, and fetched reports as hostile data.

## Trust boundaries

- **Trusted:** the Docker host, the root dispatcher and its executor, gVisor, Docker, the pinned images and dependencies, and the configured model provider.
- **Agent container:** parses hostile files and talks to model endpoints through the egress proxy. Assume it can be fully compromised; it then controls everything it can write (`out/`, `session/`, `exec/requests/`) and can race those files while the host reads them.
- **Execution containers:** run validated command vectors with no network, no model keys, no Docker socket, and only a private copy of the verified target plus a private output directory.
- **Console:** an unprivileged host service (`re-console`, group `relab`) bound to loopback. It can write the inbox, cancel markers, and its own control directory (API keys, execution policy, approval decisions); it can read reports and the live feed. It never receives the Docker socket. A compromised console can submit or cancel jobs, change keys, and loosen the execution policy, but every execution still runs in the same networkless sandbox.
- **Submitters:** members of `relab` are authorized, but their uploads are untrusted data and they may hold open descriptors into what they uploaded.

## Threat model

The design assumes a submitted binary may exploit a parser, analysis tool, agent process, execution image, gVisor, or the host kernel; consume bounded resources (CPU, memory, bytes, inodes); race files while they are read; or attempt to exfiltrate target data through an allowed model endpoint. It also assumes an uploader may try to change bytes after verification, swap directories for links during a claim, or reuse a job id.

Controls include: claims through pinned `O_NOFOLLOW` descriptors into a root-only work directory; admission that locks upload directories before listing them and copies every file while hashing the copied bytes; fresh per-job containers with content-addressed images, `--pull=never`, read-only roots, dropped capabilities, `no-new-privileges`, memory without swap, CPU, PID and file-size limits; a deny-first egress proxy that refuses unlisted names before resolving them and refuses special-purpose destinations; no execution-container network; a closed request schema with an operator policy and approvals; containment that kills, removes, and proves absent every container, including one stopped inside the Docker client's create window and after every normal exit; a storage reserve; a typed `result.json` built from dispatcher-owned facts; redaction of every published file, file name, and result field; bounded, sanitized live events; fail-closed recovery; and a hardened systemd unit (no network, `@system-service` syscalls, `ExecStopPost` cleanup, start limits).

## Accepted risks

- Model keys exist inside each per-job agent container. A compromised agent can read them.
- The model cost cap is enforced only for an uncompromised agent, and is soft by one request. A compromised agent can call an allowed endpoint directly.
- Redaction is best effort. Exact, hex, and base64 forms are replaced; split, transformed, encrypted, or indirect disclosure is not caught.
- Allowed model endpoints can carry target data.
- gVisor reduces kernel attack surface but is not a virtual-machine boundary.
- "The agent never executes the target" is a prompt rule. The agent image contains interpreters, so a confused or compromised agent can run target code next to its keys, inside gVisor.
- Execution-policy rules are a safety net, not a boundary; an agent can obfuscate a command line. Approvals put a person in the loop but the person sees only the argv.
- The console fetches URLs from the host. It refuses private, loopback, link-local and other non-global destinations unless the operator enables private fetches, and the bytes it stores are never executed on the host.
- Ubuntu's `tshark` and `p7zip-full` builds have known parser bugs; they run only inside the gVisor sandboxes.

## Report a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab and select **Report a vulnerability**. Include affected versions, reproduction steps, impact, and any suggested mitigation. Do not disclose the issue publicly before maintainers have had time to assess it.
