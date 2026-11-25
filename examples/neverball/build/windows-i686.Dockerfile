# Offline job image for the pinned Neverball Rust source. Provision the image
# separately; the build context contains no operator data or game archive.
FROM docker.io/library/rust@sha256:1e626a07e55b67d7fe19de7a590519e0cf4b75cf3788b058fa75cf4d599d9b5a
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      binutils-mingw-w64-i686=2.44-3+12+b1 \
      gcc-mingw-w64-i686-win32=14.2.0-19+27+b1 \
 && rustup target add --toolchain 1.100.0 i686-pc-windows-gnu \
 && rm -rf /var/lib/apt/lists/*
ENV HOME=/work CARGO_HOME=/work/cargo CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=1 RUSTUP_HOME=/usr/local/rustup
USER 65534:65534
WORKDIR /work
# The upstream miniquad 0.4.11 crate calls C preprocessor aliases that the
# Rust bindings don't export on i686. Patch only a private work copy. The
# registry archive and Cargo.lock stay unmodified for source verification.
ENTRYPOINT ["/bin/sh", "-ec", "cp -R /vendor/miniquad /work/miniquad && chmod u+w /work/miniquad/src/native /work/miniquad/src/native/windows.rs && patch --batch --fuzz=0 -d /work/miniquad -p1 -i /src/build/miniquad-i686.patch >&2 && cargo --config 'paths=[\"/work/miniquad\"]' --config 'source.crates-io.replace-with=\"vendored-sources\"' --config 'source.vendored-sources.directory=\"/vendor\"' build --manifest-path /src/desktop/Cargo.toml --target-dir /work/target --frozen --release --target i686-pc-windows-gnu --bin neverball >&2 && exec cat /work/target/i686-pc-windows-gnu/release/neverball.exe"]
