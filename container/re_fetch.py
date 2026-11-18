#!/usr/bin/env python3
"""re_fetch: copy a finished job report from the Docker host.

  python re_fetch.py <job-id> [--dest DIR] [--host SSH_TARGET] [--wait [--timeout S] [--interval S]] [--status]

The SSH target comes from --host or RE_LAB_HOST. The helper first checks that
/srv/re-lab/reports/<job-id>/result.json exists; the dispatcher writes it last, so until then
the job is queued, running, or unknown and the helper exits 3. --wait polls instead, every
--interval seconds (default 15) for at most --timeout seconds (default 7800). The report
(report.md, result.json, session.jsonl, evidence/) is copied into a private temporary directory
inside DIR (default: current directory) and renamed to DIR/<job-id> only when the copy is
complete; a failed copy leaves nothing behind. DIR/<job-id> must not exist. --status prints
only the status line and copies nothing else. Report content is untrusted: it is written by a
model that read a hostile file, so printed values are validated or shown escaped.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REMOTE_ROOT = "/srv/re-lab"
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,100}$")
STATUS_RE = re.compile(r"^[a-z][a-z-]{1,40}$")
EXIT_NOT_READY = 3
DEFAULT_TIMEOUT_S = 7800
DEFAULT_INTERVAL_S = 15
RESULT_MAX_BYTES = 1024 ** 2
SHOWN_MAX = 80


def shown(value, valid):
    """A result.json value for the terminal: as is when valid, else an escaped, bounded repr."""
    if valid:
        return "None" if value is None else str(value)
    text = ascii(value)
    return text if len(text) <= SHOWN_MAX else text[:SHOWN_MAX - 3] + "..."


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def status_line(job_id, result):
    """One line naming status, cost and exit code; nothing untrusted is printed raw."""
    if not isinstance(result, dict):
        return f"{job_id}: result.json is not a JSON object"
    status = result.get("status")
    cost = result.get("cost_usd")
    exit_code = result.get("exit_code")
    parts = [
        f"status={shown(status, isinstance(status, str) and STATUS_RE.fullmatch(status) is not None)}",
        f"cost_usd={shown(cost, _is_finite_number(cost))}",
        f"exit_code={shown(exit_code, exit_code is None or _is_int(exit_code))}",
    ]
    if "error" in result:
        parts.append(f"error={shown(result['error'], False)}")
    return f"{job_id}: " + " ".join(parts)


def result_ready(host, job_id):
    """True once the host has written reports/<id>/result.json (the last file it writes)."""
    argv = ["ssh", "-n", host, "test", "-f", f"{REMOTE_ROOT}/reports/{job_id}/result.json"]
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, check=False, shell=False)
    except OSError as exc:
        raise SystemExit(f"re_fetch: cannot run ssh: {type(exc).__name__}") from None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise SystemExit(f"re_fetch: ssh failed with exit {completed.returncode}")


def wait_until_ready(host, job_id, wait, timeout, interval):
    deadline = time.monotonic() + timeout
    while True:
        if result_ready(host, job_id):
            return True
        remaining = deadline - time.monotonic()
        if not wait or remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def scp(argv):
    try:
        subprocess.run(argv, check=True, shell=False, stdin=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"re_fetch: scp failed with exit {exc.returncode}") from None
    except OSError as exc:
        raise SystemExit(f"re_fetch: cannot run scp: {type(exc).__name__}") from None


def read_result(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(RESULT_MAX_BYTES + 1)
        if len(data) > RESULT_MAX_BYTES:
            raise ValueError("result.json is too large")
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit(
            f"re_fetch: copied, but result.json is unreadable: {type(exc).__name__}") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_id")
    parser.add_argument("--dest", help="directory to copy into (default: current directory)")
    parser.add_argument("--host", default=os.environ.get("RE_LAB_HOST"),
                        help="Docker host SSH target (or set RE_LAB_HOST)")
    parser.add_argument("--wait", action="store_true",
                        help="poll until the report is ready instead of exiting 3")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, metavar="SECONDS",
                        help=f"with --wait, give up after this long (default {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, metavar="SECONDS",
                        help=f"with --wait, time between checks (default {DEFAULT_INTERVAL_S})")
    parser.add_argument("--status", action="store_true",
                        help="print only the status line; copy no report")
    args = parser.parse_args(argv)
    if not args.host:
        raise SystemExit("re_fetch: set --host or RE_LAB_HOST")
    if not JOB_ID_RE.fullmatch(args.job_id):
        raise SystemExit("re_fetch: job id must match ^[a-z0-9-]{8,64}$")
    if not HOST_RE.fullmatch(args.host):
        raise SystemExit("re_fetch: unsupported host name")
    if not (math.isfinite(args.timeout) and args.timeout >= 0):
        raise SystemExit("re_fetch: --timeout must be a non-negative number of seconds")
    if not (math.isfinite(args.interval) and args.interval > 0):
        raise SystemExit("re_fetch: --interval must be a positive number of seconds")
    dest_text = os.getcwd() if args.dest is None else args.dest
    if not dest_text or dest_text.startswith("-"):
        raise SystemExit("re_fetch: --dest must be a directory path that does not start with '-'")
    # Absolute, so scp never reads a ':' in the path as a remote host.
    dest = Path(os.path.abspath(dest_text))
    target = dest / args.job_id
    if not args.status and os.path.lexists(target):
        raise SystemExit(f"re_fetch: {target} already exists; remove it or pick another --dest")
    if not wait_until_ready(args.host, args.job_id, args.wait, args.timeout, args.interval):
        print(f"re_fetch: {args.job_id}: not ready (queued, running, or unknown id)",
              file=sys.stderr)
        return EXIT_NOT_READY
    remote = f"{args.host}:{REMOTE_ROOT}/reports/{args.job_id}"
    if args.status:
        with tempfile.TemporaryDirectory(prefix="re-fetch-") as tmp:
            local = Path(tmp) / "result.json"
            scp(["scp", "-q", "--", f"{remote}/result.json", str(local)])
            result = read_result(local)
        print(status_line(args.job_id, result))
        return 0
    try:
        dest.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".re-fetch-", dir=dest))
    except OSError as exc:
        raise SystemExit(f"re_fetch: cannot use {dest} as --dest: {type(exc).__name__}") from None
    try:
        scp(["scp", "-r", "-q", "--", remote, str(staging)])
        copied = staging / args.job_id
        result = read_result(copied / "result.json")
        if os.path.lexists(target):
            raise SystemExit(f"re_fetch: {target} already exists; remove it or pick another --dest")
        try:
            os.rename(copied, target)
        except OSError as exc:
            raise SystemExit(f"re_fetch: cannot move the copy into place: {type(exc).__name__}") from None
    finally:
        # A failed or interrupted copy leaves nothing behind, so a retry is never blocked.
        shutil.rmtree(staging, ignore_errors=True)
    print(status_line(args.job_id, result))
    print(f"report: {target / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
