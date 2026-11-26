# The installer builds this recipe without any operator files in the build context.
FROM docker.io/library/rust:1-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make cmake \
 && cc --version && c++ --version && make --version && cmake --version \
 && rm -rf /var/lib/apt/lists/*
USER 65534:65534
