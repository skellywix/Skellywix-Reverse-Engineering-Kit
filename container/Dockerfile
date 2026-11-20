# Autonomous reverse-engineering image. Targets: `tools` (shared base layer), `agent` (re-agent),
# `exec` (re-lab-exec, the executor's image, adds qemu-user) and `egress` (re-egress allowlist proxy).
#
# Pinning rule: the base image is pinned by digest, apt reads a dated snapshot, and every
# downloaded artifact goes through fetch-verified, which fails the build on a SHA-256
# mismatch. Nothing here uses `latest`. Sources for every pin are in README.md.
#
# Secrets are never build inputs: no ARG or ENV carries a key, and nothing is copied from
# /srv/re-lab/secrets.

ARG APT_SNAPSHOT=20261118T000000Z

# ---------------------------------------------------------------------------
FROM ubuntu:24.04@sha256:ab18ecc70c55c703eae968948f9f1dbbe662aa8d9badac7c982e6a6237b9d74c AS base
ARG APT_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
# snapshot.ubuntu.com redirects http to https, so apt needs CA certificates before it can read the
# snapshot. Bootstrap ca-certificates from the image's default archive (indexes verified by the
# Ubuntu archive keyring; only this one package's version is unpinned), then switch every source to
# the dated snapshot and reinstall ca-certificates and curl from it, so the final versions are pinned.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources \
 && printf '%s\n' \
      'Types: deb' \
      "URIs: https://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}" \
      'Suites: noble noble-updates noble-security' \
      'Components: main restricted universe multiverse' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/snapshot.sources \
 && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get update \
 && CA_VERSION="$(apt-cache madison ca-certificates | awk 'NR==1 {print $3}')" \
 && test -n "$CA_VERSION" \
 && apt-get install -y --no-install-recommends --allow-downgrades "ca-certificates=${CA_VERSION}" curl \
 && dpkg-query -W -f='${Version}\n' ca-certificates | grep -qxF "$CA_VERSION" \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 <<'EOF' /usr/local/bin/fetch-verified
#!/bin/sh
# fetch-verified URL SHA256 OUTPUT : download over https, then fail unless the digest matches.
set -eu
[ "$#" -eq 3 ] || { echo "usage: fetch-verified URL SHA256 OUTPUT" >&2; exit 2; }
curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 -o "$3" "$1"
echo "$2  $3" | sha256sum --check --strict -
EOF

# ---------------------------------------------------------------------------
# Zeek has no release binary for this base: build the pinned source tarball.
FROM base AS zeek-build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake flex bison libpcap-dev libssl-dev zlib1g-dev python3 \
 && rm -rf /var/lib/apt/lists/*
RUN fetch-verified \
      https://github.com/zeek/zeek/releases/download/v9.0.1/zeek-9.0.1.tar.gz \
      fbee2eb5ee6ebd64ef0db830fbb9fa1d702d58807643c719e8338a59329197f2 /tmp/zeek.tar.gz \
 && mkdir /src && tar -xzf /tmp/zeek.tar.gz -C /src --strip-components=1
WORKDIR /src
RUN ./configure --prefix=/opt/zeek --build-type=Release --disable-spicy --disable-python \
      --disable-javascript --disable-zeekctl --disable-zkg --disable-zeek-client --disable-btest \
      --disable-btest-pcaps --disable-cpp-tests --disable-broker-tests --disable-cluster-backend-zeromq \
 && make -j2 \
 && make install

# ---------------------------------------------------------------------------
# Binwalk 3 ships no release binaries: build the pinned crate with its own Cargo.lock.
FROM rust:1.100.0-bookworm@sha256:ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea AS binwalk-build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libfontconfig1-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      -o /tmp/binwalk.crate https://crates.io/api/v1/crates/binwalk/3.1.0/download \
 && echo "99d5dc70f5021c55624f098adae4d1de9c1c07d000475664704b675aad7e0811  /tmp/binwalk.crate" \
      | sha256sum --check --strict - \
 && mkdir /src && tar -xzf /tmp/binwalk.crate -C /src --strip-components=1
WORKDIR /src
RUN cargo build --release --locked \
 && install -m 0755 target/release/binwalk /usr/local/bin/binwalk

# ---------------------------------------------------------------------------
FROM base AS tools
ENV DEBIAN_FRONTEND=noninteractive

RUN echo 'wireshark-common wireshark-common/install-setuid boolean false' | debconf-set-selections \
 && apt-get update && apt-get install -y --no-install-recommends \
      binutils bzip2 cpio e2fsprogs file gdb jq libfontconfig1 libfreetype6 libmagic1t64 libpcap0.8t64 \
      libssl3t64 lz4 lzop p7zip-full procps python3 python3-venv ripgrep squashfs-tools \
      tshark unzip xxd xz-utils zlib1g zstd \
 && rm -rf /var/lib/apt/lists/*

# JDK 25 (Ghidra 12.2 runtime)
RUN fetch-verified \
      "https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.5.1%2B2/OpenJDK25U-jdk_x64_linux_hotspot_25.0.5.1_2.tar.gz" \
      760f91b22c3efc9bd1b64ff950598231be82928cc807e30691a0e7a2995a62c0 /tmp/jdk.tgz \
 && mkdir /opt/jdk && tar -xzf /tmp/jdk.tgz -C /opt/jdk --strip-components=1 && rm /tmp/jdk.tgz

# Ghidra 12.2
RUN fetch-verified \
      https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.2_build/ghidra_12.2_PUBLIC_20261016.zip \
      2c2cd776db3c1f6a097ba418922378ac3649cadd5e844a6f5f088ae628a7be67 /tmp/ghidra.zip \
 && unzip -q /tmp/ghidra.zip -d /opt && mv /opt/ghidra_12.2_PUBLIC /opt/ghidra && rm /tmp/ghidra.zip \
 && chmod 0755 /opt/ghidra/support/analyzeHeadless /opt/ghidra/support/launch.sh

# JADX 1.5.7
RUN fetch-verified https://github.com/skylot/jadx/releases/download/v1.5.7/jadx-1.5.7.zip \
      1755e4242ade71a077438723689f404079592ec7764a3a0572f98fef94ebcbf3 /tmp/jadx.zip \
 && unzip -q /tmp/jadx.zip -d /opt/jadx && rm /tmp/jadx.zip

# radare2 6.2.6 and Detect It Easy 3.21 (.deb)
RUN fetch-verified https://github.com/radareorg/radare2/releases/download/6.2.6/radare2_6.2.6_amd64.deb \
      0dddf90e217e38a6a658881b9ec7e217198487c75b7f82a332b3cbd30834636f /tmp/radare2.deb \
 && fetch-verified https://github.com/horsicq/DIE-engine/releases/download/3.21/die_3.21_Ubuntu_24.04_amd64.deb \
      96e8e82e822e3ca8a829536312b92e8b241147fe38c8ab29c9dbfc1ab21aa25d /tmp/die.deb \
 && apt-get update && apt-get install -y --no-install-recommends /tmp/radare2.deb /tmp/die.deb \
 && rm -rf /var/lib/apt/lists/* /tmp/radare2.deb /tmp/die.deb

# capa v10.0.0 and YARA-X v1.23.0
RUN fetch-verified https://github.com/mandiant/capa/releases/download/v10.0.0/capa-v10.0.0-linux.zip \
      96cb5115f495024a478b0e9f9f77f9027058155589235fbd4c50e7382055fcca /tmp/capa.zip \
 && unzip -q /tmp/capa.zip -d /opt/capa && chmod 0755 /opt/capa/capa && ln -s /opt/capa/capa /usr/local/bin/capa \
 && rm /tmp/capa.zip \
 && fetch-verified https://github.com/VirusTotal/yara-x/releases/download/v1.23.0/yara-x-v1.23.0-x86_64-unknown-linux-gnu.tar.gz \
      da0c17f23fc743dcd44ad6d3135c906a4c0d197685b661c4cdc923c2f8967a64 /tmp/yrx.tgz \
 && mkdir /opt/yara-x && tar -xzf /tmp/yrx.tgz -C /opt/yara-x && rm /tmp/yrx.tgz \
 && ln -s "$(find /opt/yara-x -name yr -type f | head -n 1)" /usr/local/bin/yr

# WebAssembly suite: WABT 1.0.42, wasm-tools 1.263.0, Binaryen 133, Wasmtime 50.0.3
RUN fetch-verified https://github.com/WebAssembly/wabt/releases/download/1.0.42/wabt-1.0.42-linux-x64.tar.gz \
      84895407a6bbb80e918f33b16b2fb2206021c150b6bc9ff6f761263a745ab131 /tmp/wabt.tgz \
 && mkdir /opt/wabt && tar -xzf /tmp/wabt.tgz -C /opt/wabt --strip-components=1 \
 && fetch-verified https://github.com/bytecodealliance/wasm-tools/releases/download/v1.263.0/wasm-tools-1.263.0-x86_64-linux.tar.gz \
      21c036c1740a0552124c79c6e2170cb61c5b016005671a6d5bc06cf642165431 /tmp/wasm-tools.tgz \
 && mkdir /opt/wasm-tools && tar -xzf /tmp/wasm-tools.tgz -C /opt/wasm-tools --strip-components=1 \
 && fetch-verified https://github.com/WebAssembly/binaryen/releases/download/version_133/binaryen-version_133-x86_64-linux.tar.gz \
      2dc9c7813f5375db93d96ead4b78222fcc3e2677bbb832297af4797782a37489 /tmp/binaryen.tgz \
 && mkdir /opt/binaryen && tar -xzf /tmp/binaryen.tgz -C /opt/binaryen --strip-components=1 \
 && fetch-verified https://github.com/bytecodealliance/wasmtime/releases/download/v50.0.3/wasmtime-v50.0.3-x86_64-linux.tar.xz \
      c59133a9334e96eaca5e26f0d4c35199bfe43ab34049aa254ef4b47af1361476 /tmp/wasmtime.txz \
 && mkdir /opt/wasmtime && tar -xJf /tmp/wasmtime.txz -C /opt/wasmtime --strip-components=1 \
 && rm /tmp/wabt.tgz /tmp/wasm-tools.tgz /tmp/binaryen.tgz /tmp/wasmtime.txz

COPY --from=zeek-build /opt/zeek /opt/zeek
COPY --from=binwalk-build /usr/local/bin/binwalk /usr/local/bin/binwalk

# Node 22 LTS, then the REA npm tarball and its release-matched lockfile.
RUN fetch-verified https://nodejs.org/dist/v22.23.5/node-v22.23.5-linux-x64.tar.xz \
      cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8 /tmp/node.txz \
 && mkdir /opt/node && tar -xJf /tmp/node.txz -C /opt/node --strip-components=1 && rm /tmp/node.txz
ENV PATH=/opt/node/bin:${PATH} npm_config_audit=false npm_config_fund=false npm_config_update_notifier=false
RUN fetch-verified https://registry.npmjs.org/rea-agents/-/rea-agents-6.9.0.tgz \
      322338a1505e2557702a16012e7d46abdb5729641d98938662762dfbfb79262a /tmp/rea.tgz \
 && mkdir /opt/rea && tar -xzf /tmp/rea.tgz -C /opt/rea --strip-components=1 && rm /tmp/rea.tgz \
 && fetch-verified https://raw.githubusercontent.com/morluto/rea/d74549e495c58eb4a560279fd9f05508401ea0a0/package-lock.json \
      3cbce8c78e6fa4ab69618dcf9a27ea1f34670bc1664ea777fd7993901e08a163 /opt/rea/package-lock.json \
 && npm --prefix /opt/rea ci --omit=dev --omit=optional --ignore-scripts

# Python tools (Unblob, Frida, Scapy) from a hash-locked set; pip refuses any unhashed or source-only package.
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/re-venv \
 && /opt/re-venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

RUN useradd --uid 10001 --create-home --shell /bin/bash re

ENV JAVA_HOME=/opt/jdk GHIDRA_INSTALL_DIR=/opt/ghidra \
    PATH=/opt/re-venv/bin:/opt/jdk/bin:/opt/jadx/bin:/opt/ghidra/support:/opt/zeek/bin:/opt/wabt/bin:/opt/binaryen/bin:/opt/wasm-tools:/opt/wasmtime:/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---------------------------------------------------------------------------
# re-agent: the tools plus OMP, the runner, the prompt and the OMP profile `re-lab` (REA's skill is
# copied in so the profile is self-contained). It parses hostile files and never executes them,
# so it has no qemu-user.
FROM tools AS agent
# OMP 19.0.1 standalone binary. Its native addon is embedded and normally extracted into
# ~/.omp/natives, but the agent's home is a noexec tmpfs where mmap of the addon fails. Extract
# it at build time and stage it beside the binary, where the loader also looks.
RUN fetch-verified https://github.com/can1357/oh-my-pi/releases/download/v19.0.1/omp-linux-x64 \
      6ceef04430851bb1050c6fda3af467412eb44a11df0a6ff8121fe601013c5415 /usr/local/bin/omp \
 && chmod 0755 /usr/local/bin/omp \
 && HOME=/tmp/omp-natives omp --help >/dev/null \
 && install -m 0644 /tmp/omp-natives/.omp/natives/19.0.1/pi_natives.linux-x64*.node /usr/local/bin/ \
 && rm -rf /tmp/omp-natives
COPY runner.py /opt/re-lab/runner.py
COPY prompts/ /opt/re-lab/prompts/
COPY omp-profile/ /opt/re-lab/omp-profile/
RUN mkdir -p /opt/re-lab/omp-profile/skills \
 && cp -r /opt/rea/skills/reverse-engineer-anything /opt/re-lab/omp-profile/skills/ \
 && mkdir -p /srv/re-lab && chown re:re /srv/re-lab

USER re
WORKDIR /home/re
# No default process: re-dispatcher supplies the fixed `python3 /opt/re-lab/runner.py --job ID` argv.

# ---------------------------------------------------------------------------
# re-exec: the image executor.py runs target commands in (runsc, no network, read-only root).
# It adds qemu-user for foreign-architecture targets to the same tools; no OMP, no runner.
# Build it with `docker compose --profile build-only build re-exec`. No ENTRYPOINT and no CMD:
# the executor passes the request argv straight after the image reference.
FROM tools AS exec
RUN apt-get update && apt-get install -y --no-install-recommends qemu-user \
 && rm -rf /var/lib/apt/lists/*
USER re
WORKDIR /job/out

# ---------------------------------------------------------------------------
FROM base AS egress
RUN apt-get update && apt-get install -y --no-install-recommends squid \
 && rm -rf /var/lib/apt/lists/*
COPY egress-allowlist /etc/squid/squid.conf
# Start as the unprivileged proxy user: under gVisor, squid cannot reopen the root-owned
# /dev/stderr pipe after dropping from root, and starting unprivileged needs no capabilities.
USER proxy
EXPOSE 3128
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
