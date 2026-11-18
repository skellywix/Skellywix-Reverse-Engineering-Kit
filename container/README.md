# Container source

This directory contains the image build context (`Dockerfile`, `requirements.lock`, the OMP/REA
profile and the job prompt), the runtime configuration (`compose.yaml`, `egress-allowlist`), the
trusted host services (`re-dispatcher.py` with `executor.py`, `execpolicy.py` and `livefeed.py`;
`re-dispatcher.service`), the single-job agent runner (`runner.py`), the SSH submit/fetch
helpers, and the local web console (`console/`, `re-console.service`).

See the repository [README](../README.md) for architecture, deployment, verification, pinned
dependencies, and accepted risks; [docs/design.md](../docs/design.md) for the security
boundaries and job lifecycle; [docs/operations.md](../docs/operations.md) for day-two operation;
and [docs/console.md](../docs/console.md) for the console.
