# Autonomous reverse-engineering container

A container stack for static analysis and controlled dynamic execution of untrusted binaries. A trusted Docker host admits jobs, verifies immutable inputs, launches a fresh analysis agent, brokers isolated execution requests, and publishes bounded reports. The images were built on a separate Docker host and qualified on a Docker host with `runsc`.

This project processes hostile input. Read [SECURITY.md](SECURITY.md) and [the design](docs/design.md) before deployment.

For an interactive workbench that never runs specimens, open the **[Skellywix RE Kit: Bitrot Den](static-inspection/README.md)**. It starts independently of the container stack and needs no model credentials for file identification, build comparison, or checksum verification.

## Components

| Path | Role |
|---|---|
| `container/Dockerfile` | Multi-stage `agent`, `exec`, and `egress` images. |
| `container/compose.yaml` | Build definitions and the long-running egress proxy. |
| `container/re-dispatcher.py` | Trusted host service for admission, startup cleanup, isolation, execution containment, limits, and publication. |
| `container/runner.py` | Single-job agent entry point. |
| `container/executor.py` | Closed-schema broker for fresh networkless execution containers. |
| `container/re_submit.py` | Hashes and atomically uploads a job over SSH. |
| `container/re_fetch.py` | Retrieves a completed report over SSH. |
| `container/egress-allowlist` | Deny-first Squid policy for named model endpoints. |
| `container/omp-profile/` | Self-contained OMP profile with REA as its only MCP server. |

The images include OMP, REA, Ghidra, JADX, radare2, Binwalk, Unblob, capa, YARA-X, Detect It Easy, WABT, wasm-tools, Binaryen, Wasmtime, TShark, Zeek, Scapy, Frida, and qemu-user in the execution image.

At startup, the dispatcher removes all stale containers carrying its role label, verifies they
are absent, sweeps executor scratch, and only then recovers interrupted jobs or scans the
inbox. Per-job agent and execution containers disable Docker logging. Execution requests are
cancelled and contained when the agent exits; containment requires kill, forced removal, and
an inspect result proving the exact container is absent.

## Requirements

The Docker host needs:

- Linux with Docker Engine and Compose
- gVisor registered as the `runsc` runtime
- Python 3.12 or newer
- systemd for the supplied dispatcher unit
- a dedicated, size-bounded filesystem mounted at `/srv/re-lab`

The sample identity is uid/gid 10001 and group `relab`. The Docker host operator must create the storage, group, inbox permissions, model environment file, and trusted service configuration for the local environment.

Create the bounded filesystem on the Docker host before starting the dispatcher. Verify that
`/etc/fstab` has no existing `/srv/re-lab` entry before appending this example:

```bash
install -d -o root -g root -m 0755 /srv/re-lab
truncate -s 40G /var/lib/re-lab.img
mkfs.ext4 -F -N 1000000 /var/lib/re-lab.img
printf '%s\n' '/var/lib/re-lab.img /srv/re-lab ext4 loop,nodev,nosuid,nofail 0 2' >> /etc/fstab
systemctl daemon-reload
mount /srv/re-lab
findmnt --verify
```

The explicit inode count bounds entry floods. `RequiresMountsFor=/srv/re-lab` prevents the
dispatcher from using the bare directory if the mount is absent.

The image is a sparse file on the host root filesystem, so written blocks consume host root
space up to 40 GiB. Keep that much space free there, and run `fstrim /srv/re-lab` or the
distribution's `fstrim.timer` after jobs are deleted to return unused blocks.

## Build and install

Build all three images, provide an upstream resolver file, and start only the egress proxy:

```bash
docker compose -f container/compose.yaml --profile build-only build re-agent re-exec re-egress
AGENT_IMAGE=$(docker image inspect --format '{{.Id}}' re-lab-agent:local)
EXEC_IMAGE=$(docker image inspect --format '{{.Id}}' re-lab-exec:local)
install -d -m 0700 /etc/re-lab
install -m 0644 /path/to/upstream-resolv.conf /etc/re-lab/egress-resolv.conf
docker compose -f container/compose.yaml up -d re-egress
```

Use a resolver file that lists reachable upstream nameservers, not a host loopback stub.

Install the trusted host files:

```bash
install -d /opt/re-lab /etc/re-lab
install -m 0644 container/executor.py /opt/re-lab/executor.py
install -m 0644 container/re-dispatcher.py /opt/re-lab/re-dispatcher.py
install -m 0644 container/re-dispatcher.service /etc/systemd/system/re-dispatcher.service
printf '{"agent_image":"%s","exec_image":"%s"}\n' "$AGENT_IMAGE" "$EXEC_IMAGE" > /etc/re-lab/dispatcher.json
chmod 0600 /etc/re-lab/dispatcher.json
systemctl daemon-reload
systemctl enable --now re-dispatcher.service
```

`report_gid` defaults to `10001` (`relab`) and may be overridden in `dispatcher.json` with an
integer group id. Published directories are mode `0750`; files are mode `0640`.

Create `/srv/re-lab/secrets/models.env` as `root:root` mode `0600`. The dispatcher passes it only to the per-job agent container. Never add credentials to an image, Compose file, repository, or CI secret.

Before accepting untrusted work, verify that `docker info` lists `runsc`, both configured
images are content-addressed, the agent reaches only the intended model endpoints, execution
containers have no network, and the agent sees only the active job's mounts. From an agent
container, also verify that direct connections to the isolated internal bridge gateway fail.

### Running under gVisor

gVisor cannot use Docker's embedded DNS on this user-defined network. The egress proxy mounts
the operator-supplied upstream resolver file, while each agent receives an `/etc/hosts` entry
for the proxy through `--add-host`. The internal network is fixed at `172.30.97.0/24`, with the
proxy at `172.30.97.2`; if that subnet conflicts with another Docker network, change both the
Compose subnet/address and `EGRESS_IP` in `re-dispatcher.py`.

The agent home is a `noexec` tmpfs. The image build therefore extracts OMP's native addon and
installs it beside the OMP binary, where the native loader can map it without weakening the
home mount. The egress proxy starts as its unprivileged user with no added capabilities.

### Qualification limits

Qualification covered one runtime host: an egress probe from an agent container reached only
the five allowlisted model and sign-in hosts, a fill of the lab filesystem stopped at its
40 GiB boundary, and two benign smoke jobs—one static and one through the executor—published
reports end to end.
Another runtime host needs the same checks, starting with the resolver file and internal subnet.

## Submit and fetch

Both helpers require an SSH target. Supply it explicitly or through `RE_LAB_HOST`; there is no default.

```bash
export RE_LAB_HOST=docker-host
python container/re_submit.py ./target "Explain this binary" \
  --executable bin/tool --executable scripts/run --max-cost 5
python container/re_fetch.py <job-id> --dest ./reports
```

Repeat `--executable REL` for each target-relative file that must be staged as mode `0755`.
Existing execute bits are preserved, and SCP preserves staged modes. `--host` overrides
`RE_LAB_HOST`. If neither is set, the helper fails before invoking SSH or SCP.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Run the suite as root on Linux to exercise uid, ownership, `openat`, `O_NOFOLLOW`, and symlink boundaries. Windows runs the portable contract tests and skips POSIX-only cases.

## CI

CI runs on Ubuntu 24.04 with read-only repository permissions:

1. `unit` runs the full suite as root with Python 3.12.
2. `lint` runs Hadolint, validates the Compose model, and byte-compiles every Python file.
3. `image` runs only after unit and lint pass. A matrix builds the `agent`, `exec`, and `egress` targets independently with no push or loaded image and with isolated GitHub Actions caches.

The image job proves that every pinned download URL, SHA-256 check, build stage, and `COPY` path resolves. It does not prove gVisor runtime behavior or the end-to-end job lifecycle on a configured Docker host.

## Supply-chain pins

The base images are digest-pinned, apt uses the dated Ubuntu snapshot `20261118T000000Z`, Python packages are hash-locked in `container/requirements.lock`, and every downloaded artifact is verified before extraction. Exact public URLs and SHA-256 values are in `container/Dockerfile`.

| Artifact | Version | SHA-256 |
|---|---|---|
| Ubuntu image index | 24.04 | `ab18ecc70c55c703eae968948f9f1dbbe662aa8d9badac7c982e6a6237b9d74c` |
| Rust image index | 1.100.0-bookworm | `ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea` |
| OMP | 19.0.1 | `6ceef04430851bb1050c6fda3af467412eb44a11df0a6ff8121fe601013c5415` |
| Node.js | 22.23.5 | `cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8` |
| REA | 6.9.0 | `322338a1505e2557702a16012e7d46abdb5729641d98938662762dfbfb79262a` |
| REA lockfile | commit `d74549e495c58eb4a560279fd9f05508401ea0a0` | `3cbce8c78e6fa4ab69618dcf9a27ea1f34670bc1664ea777fd7993901e08a163` |
| Temurin JDK | 25.0.5.1+2 | `760f91b22c3efc9bd1b64ff950598231be82928cc807e30691a0e7a2995a62c0` |
| Ghidra | 12.2 | `2c2cd776db3c1f6a097ba418922378ac3649cadd5e844a6f5f088ae628a7be67` |
| JADX | 1.5.7 | `1755e4242ade71a077438723689f404079592ec7764a3a0572f98fef94ebcbf3` |
| Zeek | 9.0.1 | `fbee2eb5ee6ebd64ef0db830fbb9fa1d702d58807643c719e8338a59329197f2` |
| radare2 | 6.2.6 | `0dddf90e217e38a6a658881b9ec7e217198487c75b7f82a332b3cbd30834636f` |
| Binwalk | 3.1.0 | `99d5dc70f5021c55624f098adae4d1de9c1c07d000475664704b675aad7e0811` |
| capa | 10.0.0 | `96cb5115f495024a478b0e9f9f77f9027058155589235fbd4c50e7382055fcca` |
| YARA-X | 1.23.0 | `da0c17f23fc743dcd44ad6d3135c906a4c0d197685b661c4cdc923c2f8967a64` |
| Detect It Easy | 3.21 | `96e8e82e822e3ca8a829536312b92e8b241147fe38c8ab29c9dbfc1ab21aa25d` |
| WABT | 1.0.42 | `84895407a6bbb80e918f33b16b2fb2206021c150b6bc9ff6f761263a745ab131` |
| wasm-tools | 1.263.0 | `21c036c1740a0552124c79c6e2170cb61c5b016005671a6d5bc06cf642165431` |
| Binaryen | 133 | `2dc9c7813f5375db93d96ead4b78222fcc3e2677bbb832297af4797782a37489` |
| Wasmtime | 50.0.3 | `c59133a9334e96eaca5e26f0d4c35199bfe43ab34049aa254ef4b47af1361476` |

### Dependency candidates

The **Dependency candidates** GitHub Actions workflow runs weekly or on demand.
Download its `dependency-candidates` receipt to see candidate versions, hashes,
and checks that failed. The job downloads the candidate bytes on a disposable
runner, but retains only the receipt. It doesn't run candidate programs, push
pin changes, publish images, or deploy.

- A REA release is eligible only when published to npm. Review the tarball and
  immutable, release-matched lockfile as one change. The agent image copies the
  skill shipped in that tarball; don't update it separately.
- Native release assets, the Binwalk crate, and Node LTS stay in the existing
  platform and release lanes. Use the release receipt to review URL and digest
  changes along with any version-specific build paths.
- Review the Python wheel set together against `container/requirements.lock`.
  A workbench UI candidate includes its new manifest and npm lock; rebuild the
  checked-in UI and check its third-party notices in the same pull request.
- Image candidates retain the supported platform. An Ubuntu snapshot candidate
  uses signed archive metadata; the build verifies and resolves the actual
  packages. Review Actions updates as commit pins, not floating tags.

Keep the prior deployed image digest until the new build passes checks, including
the applicable local analysis or workbench smoke tests. Revert the reviewed
dependency commit if the pins need to be rolled back.

## Accepted risks

- **Key exposure — accepted risk.** Model keys exist inside each per-job agent container.
- **Cost-cap bypass — accepted risk.** The native `$20` maximum is effective only while the
  agent is uncompromised.
- **Report disclosure — accepted risk.** Exact-value redaction is not robust against encoded,
  split, transformed, encrypted, or indirect secret disclosure in report output.
- **Isolation escape — accepted risk.** gVisor reduces kernel attack surface but is not a
  virtual-machine boundary.
- **Allowed-endpoint exfiltration — accepted risk.** Allowed model endpoints can carry target
  data.

See [SECURITY.md](SECURITY.md) for the full threat model and vulnerability reporting process.
