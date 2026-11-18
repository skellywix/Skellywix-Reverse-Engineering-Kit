# Operations

This runbook covers day-two operation of the lab host: what each control does, how to read its
output, and what to check after an upgrade. Installation is in the [README](../README.md); the
console has its own guide in [console.md](console.md).

## Preflight

```bash
python3 /opt/re-lab/re-dispatcher.py --config /etc/re-lab/dispatcher.json --check
```

Each line is `PASS`, `WARN` or `FAIL`. The unit runs the same command as `ExecStartPre=`, so any
`FAIL` keeps the dispatcher stopped (`systemctl status re-dispatcher` shows the report).

| Check | FAIL when | Fix |
|---|---|---|
| Directory tree | a required directory is missing, a link, owned by the wrong user, writable by anyone but its owner (and the report group where the layout grants it), or `exec/runs` grants any group/other access | `--init-layout` |
| `models.env` | not a single-link regular root file with mode 0600 | `chown root:root`, `chmod 0600` |
| Docker | unreachable, or older than 28.0 | upgrade Docker Engine (29.10+ recommended) |
| runsc | not listed by `docker info` | register gVisor as `runsc` |
| Images | `agent_image` or `exec_image` not present | rebuild, then update `dispatcher.json` |
| Network | not internal, gateway not isolated, or subnet not `172.30.97.0/24` | recreate it with the current `compose.yaml` on Docker 28+ |

Warnings (not blocking): cosmetic owner/mode differences, missing optional directories
(`jobs/cancel`, `jobs/quarantine`, `live`), a low storage reserve, a stopped egress proxy, and a
`runsc` release older than release-20260831.0.

## Statuses, cancellation and stopping

The status table is in the README. Operationally:

- A cancel marker is any entry named `jobs/cancel/<job-id>`. The dispatcher only checks for the
  active job's name, so a flood of markers costs nothing. It removes the marker after use.
- `systemctl stop` sends SIGTERM to the dispatcher alone (`KillMode=mixed`). The active job's
  agent and sandbox runs are contained, the job is published as `failed` with
  `error: dispatcher stopped`, and the service exits. `ExecStopPost=--cleanup-only` then removes
  every container labeled `re-lab.role`, and proves none remains, after any stop, crash or failed
  start. The start limit (5 starts in 15 minutes) turns a deterministic failure into a failed unit.
- `recovered-after-restart` marks a job that was claimed but never published because the host or
  the service died. Recovery runs only after stale containers are removed and stale run
  directories are swept.

## Admission and the inbox

A claim opens `jobs/inbox/<id>` with `O_NOFOLLOW`, creates a root-only `jobs/work/<id>/`, moves
the upload into it as `upload/`, and checks that the moved inode is the one it opened. Admission
then locks every upload directory to root before listing it, copies each file into root-owned
trees while hashing the copied bytes, compares the result with `job.json`, and deletes the
upload. An uploader's open descriptors therefore reach only the discarded upload.

An inbox entry that cannot be claimed (a reused id, a link, a vanished entry) is moved to
`jobs/quarantine/<id>.<ns>` and logged; the queue continues. Rejections that happen after the
claim are published as `rejected-job` or `rejected-hash` with a fixed `error` text.

## Storage reserve

`min_free_mib` (default 1024) and `min_free_inodes` (default 20000) in `dispatcher.json` define
the reserve, measured as an unprivileged writer sees the filesystem. Below it:

- no new job is claimed, and the heartbeat state becomes `storage-low`;
- a running job is contained and published as `storage-cap`; its `out/` and `session/` trees are
  then deleted, because they caused the shortage (the published, bounded copies remain).

Free space with `--prune DAYS` (finished jobs only) and `fstrim /srv/re-lab`.

## Redaction

Every published file (report, each evidence file, the merged session log) and every string
field of `result.json` is redacted for every value of at least 8 bytes in `models.env` and the
console's key file, including their hex and base64 encodings at all three alignments. Text keeps
a visible `[REDACTED]` marker; files containing NUL bytes get a same-length mask so offsets stay
valid. File names are redacted too. Shorter values (`1`, `true`) are never treated as secrets.
Split, transformed, or encrypted disclosure remains an accepted risk.

## Execution policy and approvals

The console writes `/srv/re-lab/console/exec-policy.json`; the dispatcher reads it as root for
every execution request, with `O_NOFOLLOW`, and accepts it only as a regular, single-link file
owned by `console_uid` that group and others cannot write.

```json
{"v": 1, "default_action": "allow", "approval_timeout_s": 600,
 "rules": [{"id": "r1", "name": "Hold shells for review", "enabled": true, "priority": 100,
            "match": "program", "value": "sh", "action": "ask"}]}
```

- Enabled rules are sorted by priority (highest first; ties keep file order). The first match
  wins; otherwise `default_action` applies.
- `program` compares the basename of `argv[0]`; `contains` is a case-insensitive substring of the
  space-joined argv (the whole argv, never truncated); `prefix` is a case-sensitive prefix of it.
- `block` answers the request as `rejected` (`blocked by execution policy`). `ask` emits an
  `exec.approval_requested` event carrying a fresh `approval_id` and waits up to
  `approval_timeout_s` (30–3600 s) for
  `/srv/re-lab/console/approvals/<job-id>/<request-name>` =
  `{"approval_id": "...", "decision": "approve"|"deny"}`. A stale or mismatched decision counts
  as a denial, and a timeout is `approval timed out`.
- No console configured (`console_uid` unset) or no policy file means allow everything. A
  policy file that exists but cannot be trusted or parsed blocks every request.

Rules are a safety net, not a boundary: every request already runs in a fresh networkless gVisor
container, and an agent can always obfuscate a command line. Use `ask` to put a person in the
loop.

## Live feed

The dispatcher writes `/srv/re-lab/live/dispatcher.json` (heartbeat: `state` idle, running,
storage-low or stopped; `active_job`; `queue`; free bytes and inodes) and one
`/srv/re-lab/live/<job-id>/events.jsonl` per job: JSON lines of at most 16 KiB, at most 16 MiB
per job, with `v`, `seq`, `ts`, `job` and `type`. Types: `job.claimed`, `job.verified`,
`job.files`, `job.rejected`, `job.cancel_requested`, `agent.started`, `agent.text`, `llm.usage`,
`tool.call`, `tool.result`, `exec.request`, `exec.approval_requested`, `exec.approval_resolved`,
`exec.result`, `storage.low`, `agent.exited`, `feed.truncated` and `job.finished`. Strings come
from hostile data; they are redacted, stripped of control and bidirectional-override
characters, and truncated before they are written. The full field list is in
[console.md](console.md).

## Upgrades and requalification

Any change to an image (a pin bump, a new APT snapshot, a base-image digest) or to the dispatcher
needs the README's isolation checks and both smoke jobs (one static, one through the executor)
before untrusted work resumes. Keep `docker info` showing `runsc`, keep `--check` free of FAIL
lines, and confirm from an agent container that only the five model hosts are reachable and that
a CONNECT to any other name is refused without a DNS lookup.
