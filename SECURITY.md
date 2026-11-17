# Security policy

## Scope

This project performs static analysis and dynamic execution of untrusted binaries. Treat submitted files, questions, model output, execution output, and fetched reports as hostile data.

The trusted boundary includes the Docker host, dispatcher, executor, gVisor, Docker, pinned images and dependencies, and configured model provider. The agent container parses hostile files. The execution container runs validated command vectors with no network, no model keys, no Docker socket, and only private per-job mounts.

## Threat model

The design assumes a submitted binary may exploit a parser, analysis tool, agent process, execution image, gVisor, or the host kernel; consume bounded resources; race files while they are read; or attempt to exfiltrate target data through an allowed model endpoint.

Controls include fresh per-job containers, root-owned source copies, content-addressed runtime images, a deny-first egress proxy, no execution-container network, no target secrets, closed request schemas, fixed launch flags, incremental `O_NOFOLLOW` walks, byte and entry caps, bounded publication, and fail-closed recovery.

## Accepted risks

- Model keys exist inside each per-job agent container. A compromised agent can read them.
- The model cost cap is enforced only for an uncompromised agent. A compromised agent can call an allowed endpoint directly.
- Redaction is best effort. Exact-value replacement does not catch encoded, split, transformed, encrypted, or indirect disclosure.
- Allowed model endpoints can carry target data.
- gVisor reduces kernel attack surface but is not a virtual-machine boundary.

## Report a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab and select **Report a vulnerability**. Include affected versions, reproduction steps, impact, and any suggested mitigation. Do not disclose the issue publicly before maintainers have had time to assess it.
