#!/usr/bin/env python3
"""re_fetch: copy a finished job report from the Docker host.

  python re_fetch.py <job-id> [--dest DIR] [--host SSH_TARGET]

The SSH target comes from --host or RE_LAB_HOST. The helper copies
/srv/re-lab/reports/<job-id>/ (report.md, result.json, session.jsonl, evidence/) to
DIR/<job-id> (default: current directory) and prints the job status, cost and exit code.
The destination must not exist. Report content is untrusted: it is written by a model that
read a hostile file.
"""
import argparse
import json
import re
import os
import subprocess
import sys
from pathlib import Path

REMOTE_ROOT = "/srv/re-lab"
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,100}$")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_id")
    parser.add_argument("--dest", type=Path, default=Path.cwd())
    parser.add_argument("--host", default=os.environ.get("RE_LAB_HOST"),
                        help="Docker host SSH target (or set RE_LAB_HOST)")
    args = parser.parse_args(argv)
    if not args.host:
        raise SystemExit("re_fetch: set --host or RE_LAB_HOST")
    if not JOB_ID_RE.match(args.job_id):
        raise SystemExit("re_fetch: job id must match ^[a-z0-9-]{8,64}$")
    if not HOST_RE.match(args.host):
        raise SystemExit("re_fetch: unsupported host name")
    target = args.dest / args.job_id
    if target.exists():
        raise SystemExit(f"re_fetch: {target} already exists; remove it or pick another --dest")
    args.dest.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["scp", "-r", "-q", f"{args.host}:{REMOTE_ROOT}/reports/{args.job_id}", str(args.dest)],
                       check=True, shell=False)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"re_fetch: scp failed with exit {exc.returncode}; "
                         "the job may still be running or the id is wrong") from None
    try:
        result = json.loads((target / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"re_fetch: copied, but result.json is unreadable: {type(exc).__name__}") from None
    print(f"{args.job_id}: status={result.get('status')} cost_usd={result.get('cost_usd')} "
          f"exit_code={result.get('exit_code')}")
    print(f"report: {target / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
