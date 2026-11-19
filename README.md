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

The base images are digest-pinned, apt uses the dated Ubuntu snapshot `20261027T000000Z`, Python packages are hash-locked in `container/requirements.lock`, and every downloaded artifact is verified before extraction. Exact public URLs and SHA-256 values are in `container/Dockerfile`.

| Artifact | Version | SHA-256 |
|---|---|---|
| Ubuntu image index | 24.04 | `73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8` |
| Rust image index | 1.100.0-bookworm | `ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea` |
| OMP | 18.22.0 | `f706b27ba845834d8f8360705a2e9ba1534f3593fa24411d41358272de33642b` |
| Node.js | 22.23.4 | `1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce` |
| REA | 6.8.0 | `8d542ec76a9cb350584b60a6c9c088706ff812d5bb5b094c20c95b6dc25698bb` |
| REA lockfile | commit `160d821ac9a0310afcb8d1fc427862dd6508add5` | `1d8fa0f33b3125d3e601ec832b18d9d35b37453f36340dc0507f065a8de158ad` |
| Temurin JDK | 25.0.5+8 | `68982529869257d0ee6b824ec5aa270ff8b118bb438670da0b97ebdab178d7fb` |
| Ghidra | 12.1.4 | `ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db` |
| JADX | 1.5.6 | `545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974` |
| Zeek | 9.0.1 | `fbee2eb5ee6ebd64ef0db830fbb9fa1d702d58807643c719e8338a59329197f2` |
| radare2 | 6.2.4 | `7019eedc0e0e87d1f53d6b8f5fc62b898567679c5efdce7fbfc557c9e7655e90` |
| Binwalk | 3.1.0 | `99d5dc70f5021c55624f098adae4d1de9c1c07d000475664704b675aad7e0811` |
| capa | 10.0.0 | `96cb5115f495024a478b0e9f9f77f9027058155589235fbd4c50e7382055fcca` |
| YARA-X | 1.22.1 | `c0ff80408d57b94f8348b9b19b5c70fb03e6471e7fdd761c97e0b4876804eecf` |
| Detect It Easy | 3.21 | `96e8e82e822e3ca8a829536312b92e8b241147fe38c8ab29c9dbfc1ab21aa25d` |
| WABT | 1.0.42 | `84895407a6bbb80e918f33b16b2fb2206021c150b6bc9ff6f761263a745ab131` |
| wasm-tools | 1.262.0 | `655b8df67d54c3ef9b52fc237c4e94bbfbb5f5f43825245cb0940a7002a9ed65` |
| Binaryen | 133 | `2dc9c7813f5375db93d96ead4b78222fcc3e2677bbb832297af4797782a37489` |
| Wasmtime | 50.0.0 | `77c19ee0437991f9ac7837b9c269d9c7076744f85dad263fea5214cc2723c7ff` |

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
