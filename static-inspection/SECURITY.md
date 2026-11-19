# Security policy

## Scope

The inspector is an unprivileged Linux CLI. The operator chooses a local directory and supplies a filename. The file and its name are untrusted; the interpreter, script, operating system, local filesystem, and procfs are trusted. Keep the script outside the intake directory and use Python's isolated mode (`-I`).

## Threat model

Only a single filename is accepted. The inspector pins the entry without following links or opening it for I/O. It accepts only a regular file with one hard link, no larger than 2 GiB, then reads through its own pinned descriptor. It bounds reads and checks for visible changes in size, link count, and timestamps before reporting a digest. An optional expected SHA-256 must match before a report is printed.

The report contains a fixed signature label, size, digest, and schema version. No filenames, file content, environment values, or questions are published. The inspector doesn't create reports on disk, open sockets, launch child processes, load tools or plug-ins, or invoke an interpreter on a sample.

## Accepted risks

- A recognized prefix is not proof of format, identity, or safety. A digest can identify a known file; treat reports as sensitive where appropriate.
- Metadata checks detect ordinary concurrent writes; they are not a filesystem snapshot or a guarantee against a privileged writer. For stronger integrity, use a read-only intake and supply a trusted digest.
- Use a normal local filesystem. An unresponsive or attacker-controlled mount can stall a reader; this CLI is not a sandbox or a multi-user upload service. Do not run it as root or give an untrusted party control over its installation.

## Optional workbench

The desk trusts the single-user operator host, browser, Docker host, client, and built web assets. Keep the IPC directory owner-private. Both the local launcher and the manual SSH setup must bind both listeners to loopback. The operator listener doesn’t authenticate a local user: any process that can reach its port can act as the operator. Don’t expose it to a LAN, reverse proxy, untrusted host account, or agent with independent shell or networking access.

The offline worker has separate operator and MCP sockets, no Docker socket or host credentials, and no sample directory. It reads bounded submitted bytes in memory and keeps only selected metadata. Reports are not safety verdicts. The agent key is a bearer capability: keep it out of URLs, chat, logs, and persistent configuration. A case expires or is revoked without automatic renewal. Reset or stop the worker to discard the case; reports and credentials explicitly copied to the operator's computer remain there.

The optional OMP critic adapter runs on the trusted operator side, not in the worker. One explicitly opted-in snapshot permits one model request. The adapter rejects unexpected fields and free text in the categorical brief; only the brief crosses the model boundary. Model output can add one bounded, cited paragraph of plain text to the operator view, never a tool call or a report finding. Any subsequent evidence entry, input change, cancellation, or expiry invalidates an outstanding receipt. This doesn't retract a brief already sent. Like the rest of the operator interface, these routes trust the single-user loopback forward; do not give a model or untrusted process access to it.

## Report a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab and select **Report a vulnerability**. Include affected versions, reproduction steps, impact, and any suggested mitigation. Do not disclose the issue publicly before maintainers have had time to assess it.
