# Autonomous reverse-engineering container design

## Scope

The system performs static analysis and controlled dynamic execution of untrusted binaries.
A trusted Docker host owns job admission, source verification, container launch, execution
requests, resource bounds, and report publication.

## Roles and boundaries

1. A fresh `re-agent` container runs OMP, REA, headless Ghidra, and static tools for one
   claimed job. It parses hostile files but does not execute them directly.
2. Fresh `re-exec` containers run only validated command vectors. They receive no model keys,
   no network, no Docker socket, and only a root-owned copy of the submitted target plus a
   private writable output directory.
3. A root-owned `re-dispatcher` service removes stale labeled containers and proves them
   absent before recovery or inbox scanning. It claims one job by same-filesystem rename,
   verifies its closed manifest and hashes, prepares private mounts, starts the agent, serves
   only that job's execution requests, and publishes bounded output.
4. The agent uses an isolated internal Docker bridge and reaches named model endpoints through
   a Squid allowlist. Literal destinations, private destinations, non-TLS ports, unlisted
   domains, and direct internal-gateway connections are denied.
5. Agent and execution containers use gVisor (`runsc`), read-only roots, dropped capabilities,
   `no-new-privileges`, fixed uid/gid 10001, PID and memory limits, per-container tmpfs, labels,
   and no Docker log driver.
6. The inbox, reports, other jobs, Docker socket, and trusted host configuration are never
   mounted into either job container.

## Job lifecycle

1. `re_submit.py` stages `target/`, `question.md`, and `job.json`, preserving execute bits and
   applying repeatable `--executable REL` overrides. It uploads with preserved modes to a
   hidden directory under `/srv/re-lab/jobs/inbox/` and atomically renames it into place.
2. The dispatcher cleans up stale labeled containers and scratch, claims the job, rejects
   unexpected entries, verifies every listed file, and creates root-owned source plus isolated
   input, output, session, and execution paths.
3. The dispatcher starts one digest-pinned, labeled agent image. The runner creates a fresh
   profile, invokes OMP with the selected model, a maximum cost of 20 USD, and a two-hour limit.
4. Execution requests use a closed JSON schema. The executor copies from the root-owned
   source, starts a fresh labeled networkless execution image, and copies regular output
   through `O_NOFOLLOW` descriptors under byte, entry, and depth caps.
5. When the agent exits, active execution is contained and reported as `cancelled`; queued
   requests do not start. Containment kills and forcibly removes the exact cidfile container,
   then requires an inspect not-found result.
6. The dispatcher scans bounded session input for cost, records incomplete accounting,
   publishes group-readable bounded report, evidence, and session files, applies exact-value
   text redaction, and writes `result.json`.
7. `re_fetch.py` copies a completed report from the Docker host.

## Fixed limits

- One active job.
- Agent: 3 GiB memory and 512 processes.
- Execution container: at most 1 GiB, one CPU, 256 processes, and 600 seconds per request.
- Execution input copy: 2 GiB, 10,000 entries, depth 32.
- Execution output copy: 64 MiB and 10,000 entries.
- Published report: 5 MiB; evidence: 200 MiB total and 25 MiB per file; session: 50 MiB.
- Dedicated `/srv/re-lab` storage is recommended so job data cannot fill the host root
  filesystem.

## Threat model

Submitted files, questions, generated model output, and execution output are untrusted. A
submitted binary may exploit parsers, the agent, the execution image, gVisor, or the host
kernel; consume bounded resources; alter files while they are read; or attempt to exfiltrate
data through an allowed model endpoint. The trusted computing base is the Docker host,
dispatcher, executor, gVisor, Docker, the pinned images and dependencies, and the configured
model provider.

The design limits exposure with fresh containers, root-owned immutable source copies,
content-addressed runtime images, no target network, no target secrets, fixed launch flags,
closed schemas, incremental `O_NOFOLLOW` walks, resource caps, bounded publication, and
fail-closed job recovery.

## Accepted risks

- **Key exposure — accepted risk.** Model keys exist inside each per-job agent container. A
  compromised agent can read them and use an allowed model endpoint as an exfiltration channel.
- **Cost-cap bypass — accepted risk.** The native `$20` maximum is effective only while the
  agent is uncompromised. A compromised agent can call an allowed endpoint directly.
- **Report disclosure — accepted risk.** Output redaction is best effort. Exact-value
  replacement does not catch encoded, split, transformed, encrypted, or indirect disclosure
  in report output.
- **Isolation escape — accepted risk.** gVisor reduces kernel attack surface but is not a
  virtual-machine boundary; an escape can reach the Docker host's kernel.
- **Provider failure — accepted risk.** A provider failure ends the job. There is no automatic
  model fallback.

Windows-only dynamic analysis, full-system firmware emulation, and network fuzzing against a
separate guest are outside this container's boundary.
