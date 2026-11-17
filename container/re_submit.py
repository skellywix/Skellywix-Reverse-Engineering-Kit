#!/usr/bin/env python3
"""re_submit: copy a target and a question to the Docker host's RE inbox.

  python re_submit.py <target-file-or-dir> "<question>" [--executable REL] [--model M] [--max-cost USD] [--host SSH_TARGET]

The SSH target comes from --host or RE_LAB_HOST. The helper stages target/, question.md and
job.json (SHA-256 and size of every file, submit time), copies the directory to a hidden inbox
path with modes preserved, then renames it into place so the host dispatcher never sees a
partial job. Dropping the job there authorizes it. The job id is printed. Secrets are never
read or sent.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REMOTE_ROOT = "/srv/re-lab"
MAX_COST_USD = 20.0
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,99}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,100}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def new_job_id(now=None):
    now = now or datetime.now(timezone.utc)
    return f"job-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_target(target):
    """Return [(source path, relative posix path)] for a file or a directory tree; refuse links."""
    target = Path(target)
    if target.is_symlink():
        raise SystemExit("re_submit: the target must not be a symlink")
    if target.is_file():
        files = [(target, target.name)]
    elif target.is_dir():
        files = []
        for root, dirs, names in os.walk(target):
            for name in dirs + names:
                if (Path(root) / name).is_symlink():
                    raise SystemExit(f"re_submit: refusing symlink inside the target: {name}")
            for name in sorted(names):
                full = Path(root) / name
                files.append((full, full.relative_to(target).as_posix()))
    else:
        raise SystemExit("re_submit: the target does not exist")
    if not files:
        raise SystemExit("re_submit: the target holds no files")
    for _, rel in files:
        if any(not NAME_RE.fullmatch(part) for part in rel.split("/")):
            raise SystemExit(f"re_submit: unsupported file name in target: {rel!r}")
    return files


def stage_job(
        staging, job_id, target, question, model, max_cost, executables=()):
    """Build the inbox layout under `staging` and return the job.json dict."""
    files = collect_target(target)
    by_relative = {rel: source for source, rel in files}
    executable_set = set(executables)
    if any(rel not in by_relative for rel in executable_set):
        raise SystemExit("re_submit: --executable must name a collected target file")
    entries = []
    for source, rel in files:
        dest = staging / "target" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        if rel in executable_set:
            os.chmod(dest, 0o755)
        else:
            mode = (dest.stat().st_mode & 0o666) | (source.stat().st_mode & 0o111)
            os.chmod(dest, mode)
        entries.append({"path": f"target/{rel}", "sha256": sha256_file(dest), "size": dest.stat().st_size})
    qpath = staging / "question.md"
    qpath.write_text(question.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    entries.append({"path": "question.md", "sha256": sha256_file(qpath), "size": qpath.stat().st_size})
    meta = {"schema": 1, "job_id": job_id,
            "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": entries}
    if model:
        meta["model"] = model
    if max_cost is not None:
        meta["max_cost_usd"] = max_cost
    (staging / "job.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return meta


def run(argv):
    subprocess.run(argv, check=True, shell=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("question")
    parser.add_argument("--model", help="default openai/gpt-6.1-sol")
    parser.add_argument("--max-cost", type=float, help=f"USD, at most {MAX_COST_USD:g}")
    parser.add_argument("--host", default=os.environ.get("RE_LAB_HOST"),
                        help="Docker host SSH target (or set RE_LAB_HOST)")
    parser.add_argument(
        "--executable", action="append", default=[], metavar="REL",
        help="relative target path to stage as mode 0755; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="stage and print job.json; copy nothing")
    args = parser.parse_args(argv)
    if not args.host:
        raise SystemExit("re_submit: set --host or RE_LAB_HOST")
    if not HOST_RE.match(args.host):
        raise SystemExit("re_submit: unsupported host name")
    if args.model and not MODEL_RE.match(args.model):
        raise SystemExit("re_submit: unsupported model name")
    if args.max_cost is not None and not 0.01 <= args.max_cost <= MAX_COST_USD:
        raise SystemExit(f"re_submit: --max-cost must be between 0.01 and {MAX_COST_USD:g}")
    if not args.question.strip():
        raise SystemExit("re_submit: the question is empty")

    executables = list(dict.fromkeys(args.executable))
    job_id = new_job_id()
    with tempfile.TemporaryDirectory(prefix="re-submit-") as tmp:
        staging = Path(tmp) / job_id
        staging.mkdir()
        meta = stage_job(
            staging, job_id, args.target, args.question, args.model, args.max_cost,
            executables=executables)
        if args.dry_run:
            print(json.dumps(meta, indent=2))
            return 0
        hidden = f"{REMOTE_ROOT}/jobs/inbox/.incoming-{job_id}"
        run(["scp", "-p", "-r", "-q", str(staging), f"{args.host}:{hidden}"])
        # The setgid inbox grants the deployment group access so the hidden upload can be
        # renamed into place; nobody else receives access.
        run(["ssh", args.host, "chmod", "-R", "u+rwX,g+rwX,o-rwx", hidden])
        if executables:
            run([
                "ssh", args.host, "chmod", "0755", "--",
                *(shlex.quote(f"{hidden}/target/{relative}") for relative in executables),
            ])
        run(["ssh", args.host, "mv", hidden, f"{REMOTE_ROOT}/jobs/inbox/{job_id}"])
    print(job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
