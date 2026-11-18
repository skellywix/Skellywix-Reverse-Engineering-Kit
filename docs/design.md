# Autonomous reverse-engineering container design

## Scope

The system performs static analysis and controlled dynamic execution of untrusted binaries.
A trusted Docker host owns job admission, source verification, container launch, execution
requests, resource bounds, and report publication. A local, unprivileged console lets analysts
submit work and follow it live.

## Roles and boundaries

1. A fresh `re-agent` container runs OMP, REA, headless Ghidra, and static tools for one
   claimed job. Its prompt forbids executing the target there; the image still contains
   interpreters, so that rule is behavioural (see Accepted risks).
2. Fresh `re-exec` containers run only validated command vectors. They receive no model keys,
   no network, no Docker socket, and only a root-owned copy of the verified target plus a
   private writable output directory.
3. A root-owned `re-dispatcher` service removes stale labeled containers and proves them
   absent before recovery or inbox scanning. It claims one job at a time through pinned
   descriptors, copies and hashes the upload into root-owned trees, prepares private mounts,
   starts the agent, serves only that job's execution requests under the operator's execution
   policy, enforces the wall clock, storage reserve, cancel markers and SIGTERM, and publishes
   bounded output plus a live event feed.
4. The agent uses an isolated internal Docker bridge and reaches named model endpoints through
   a Squid allowlist. Requests from outside the internal subnet, non-CONNECT requests, non-TLS
   ports, literal destinations and unlisted names are refused before any DNS lookup; a listed
   name that resolves to a special-purpose address is refused too. Direct internal-gateway
   connections are denied.
5. Agent and execution containers use gVisor (`runsc`), read-only roots, dropped capabilities,
   `no-new-privileges`, `--pull=never`, fixed uid/gid 10001, memory limits without swap, CPU
   and PID limits, per-container tmpfs (`noexec,nosuid,nodev`), deterministic names, labels, and
   no Docker log driver.
6. The inbox, reports, other jobs, Docker socket, and trusted host configuration are never
   mounted into either job container.
7. The console runs as an unprivileged user in the `relab` group. It writes the inbox, cancel
   markers and its own control directory (API keys, execution policy, approval decisions), and
   reads reports and the live feed. The dispatcher reads console files only through
   `O_NOFOLLOW` descriptors, as regular single-link files owned by the configured console uid.

## Job lifecycle

1. `re_submit.py` or the console stages `target/`, `question.md`, and `job.json`, uploads to a
   hidden inbox name, and renames it into place without ever merging into an existing entry.
2. The dispatcher removes stale labeled containers and scratch, then claims the job: it opens
   the inbox entry with `O_NOFOLLOW`, creates a root-only `work/<id>/`, moves the upload into it,
   and verifies the moved inode. A reused id or an unclaimable entry is quarantined instead of
   stopping the queue.
3. Admission locks each upload directory to root before listing it, copies every regular,
   single-link file into root-owned trees while hashing the copied bytes, compares the result
   with `job.json`, writes the verified manifest, deletes the upload, and builds the agent's
   read-only `in/` view plus private output, session and execution paths.
4. The dispatcher writes a per-job, root-only environment file from validated `models.env` and
   console keys, then starts one digest-pinned, labeled agent image. The runner creates a fresh
   profile, invokes OMP with the selected model, a maximum cost of at most 20 USD, and a
   two-hour limit, and fails closed if its prompt or profile is missing.
5. Execution requests use a closed JSON schema. The policy gate allows, blocks, or holds each
   request for an operator decision. The executor copies the verified source, starts a fresh
   labeled networkless execution container, proves the container gone after it exits, and copies
   regular output through `O_NOFOLLOW` descriptors under byte, entry, and depth caps.
6. When the agent exits, or the wall clock, storage reserve, a cancel marker or SIGTERM stops
   it, active execution is contained and reported as `cancelled`; queued requests do not start.
   Containment kills and forcibly removes the exact container (by cidfile id, or by its
   deterministic name inside the client's create window), then requires an inspect not-found
   result.
7. The dispatcher scans the bounded session for cost (assistant turns and off-transcript model
   usage), publishes group-readable bounded report, evidence and session files with redaction,
   and writes a typed `result.json`.
8. `re_fetch.py` or the console retrieves the completed report once `result.json` exists.

## Fixed limits

- One active job.
- Agent: 3 GiB memory without swap, 512 processes, 4 CPUs (configurable), 512 MiB tmpfs each for
  home and `/tmp` (counted in the memory limit).
- Execution container: at most 1 GiB memory without swap, one CPU, 256 processes, 1 GiB per
  file, 4096 open files, and 600 seconds per request.
- Execution request: at most 256 argv items of 8192 characters each, 256 KiB in total.
- Execution input copy: 2 GiB, 10,000 entries, depth 32.
- Execution output copy: 64 MiB and 10,000 entries.
- Published report: 5 MiB; evidence: 200 MiB total and 25 MiB per file; session: 50 MiB.
- Storage reserve: 1 GiB and 20,000 inodes by default; below it no job is claimed and a running
  job is contained.
- Live feed: 16 KiB per event and 16 MiB per job.
- Dedicated `/srv/re-lab` storage is required so job data cannot fill the host root
  filesystem.

## Threat model

Submitted files, questions, generated model output, execution output, and the live feed are
untrusted. A submitted binary may exploit parsers, the agent, the execution image, gVisor, or
the host kernel; consume bounded resources; alter files while they are read; or attempt to
exfiltrate data through an allowed model endpoint. An uploader may race their own upload or
reuse ids. The trusted computing base is the Docker host, dispatcher, executor, gVisor, Docker,
the pinned images and dependencies, and the configured model provider.

The design limits exposure with fresh containers, descriptor-based claims, copy-and-hash
admission, content-addressed runtime images, no target network, no target secrets, fixed launch
flags, closed schemas, an operator execution policy, incremental `O_NOFOLLOW` walks, resource
caps, a storage reserve, bounded publication, typed results, and fail-closed job recovery.

## Accepted risks

- **Key exposure — accepted risk.** Model keys exist inside each per-job agent container. A
  compromised agent can read them and use an allowed model endpoint as an exfiltration channel.
- **Cost-cap bypass — accepted risk.** The native `$20` maximum is effective only while the
  agent is uncompromised. A compromised agent can call an allowed endpoint directly.
- **Report disclosure — accepted risk.** Output redaction is best effort. Exact, hex, and base64
  replacement does not catch split, transformed, encrypted, or indirect disclosure in report
  output.
- **Isolation escape — accepted risk.** gVisor reduces kernel attack surface but is not a
  virtual-machine boundary; an escape can reach the Docker host's kernel.
- **Prompt-enforced boundary — accepted risk.** The agent is told never to execute the target in
  its own container, but nothing prevents it; the sandbox, not the rule, bounds the impact.
- **Provider failure — accepted risk.** A provider failure ends the job. There is no automatic
  model fallback.

Windows-only dynamic analysis, full-system firmware emulation, and network fuzzing against a
separate guest are outside this container's boundary.
