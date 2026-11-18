#!/usr/bin/env python3
"""re_submit: copy a target and a question to the Docker host's RE inbox.

  python re_submit.py <target-file-or-dir> "<question>" [--executable REL] [--model M] [--max-cost USD] [--host SSH_TARGET] [--dry-run]

The SSH target comes from --host or RE_LAB_HOST; --dry-run needs none. The helper stages
target/, question.md and job.json (SHA-256 and size of every file, submit time), copies the
directory to a hidden inbox path with modes preserved, then renames it into place so the host
dispatcher never sees a partial job. A failed step removes the hidden upload and exits with a
one-line message. Dropping the job there authorizes it. The job id is printed. Secrets are
never read or sent.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REMOTE_ROOT = "/srv/re-lab"
MAX_COST_USD = 20.0
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,99}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,100}$")
NAME_MAX_BYTES = 255
MISS_LIST_MAX = 20
# Optional job.json metadata a caller of stage_job may add; nothing else is accepted.
EXTRA_KEYS = {
    "agent": re.compile(r"[a-z0-9][a-z0-9-]{0,39}"),
    "conversation": re.compile(r"[a-z0-9-]{1,64}"),
    "source": re.compile(r"[a-z0-9-]{1,32}"),
}


def new_job_id(now=None):
    now = now or datetime.now(timezone.utc)
    return f"job-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(8)}"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_name(part):
    """A path component the inbox accepts; the dispatcher rejects the same set.

    Rejected: empty, '.', '..', '/' or '\\', control characters (C0, DEL, C1), text that is not
    strict UTF-8 (lone surrogates), and more than 255 UTF-8 bytes. Spaces, leading dots, dashes
    and underscores, and other Unicode are allowed.
    """
    if not isinstance(part, str) or part in ("", ".", ".."):
        return False
    if "/" in part or "\\" in part:
        return False
    if any(unicodedata.category(char) == "Cc" for char in part):
        return False
    try:
        encoded = part.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= NAME_MAX_BYTES


def _walk_error(error):
    raise SystemExit(f"re_submit: cannot list a directory inside the target: {type(error).__name__}")


def collect_target(target):
    """Return [(source path, relative posix path)] for a file or a directory tree.

    Links and special files (FIFO, socket, device) are refused before anything is copied.
    """
    target = Path(target)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        raise SystemExit("re_submit: the target does not exist") from None
    except OSError as exc:
        raise SystemExit(f"re_submit: cannot read the target: {type(exc).__name__}") from None
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit("re_submit: the target must not be a symlink")
    if stat.S_ISREG(info.st_mode):
        files = [(target, target.name)]
    elif stat.S_ISDIR(info.st_mode):
        files = []
        for root, dirs, names in os.walk(target, onerror=_walk_error):
            dirs.sort()
            base = Path(root)
            for name in dirs:
                if (base / name).is_symlink():
                    rel = (base / name).relative_to(target).as_posix()
                    raise SystemExit(f"re_submit: refusing symlink inside the target: {rel!r}")
            for name in sorted(names):
                full = base / name
                rel = full.relative_to(target).as_posix()
                try:
                    mode = os.lstat(full).st_mode
                except OSError as exc:
                    raise SystemExit(
                        f"re_submit: cannot read {rel!r} in the target: {type(exc).__name__}") from None
                if stat.S_ISLNK(mode):
                    raise SystemExit(f"re_submit: refusing symlink inside the target: {rel!r}")
                if not stat.S_ISREG(mode):
                    raise SystemExit(
                        "re_submit: refusing special file (FIFO, socket or device) inside the "
                        f"target: {rel!r}")
                files.append((full, rel))
    else:
        raise SystemExit("re_submit: the target must be a regular file or a directory")
    if not files:
        raise SystemExit("re_submit: the target holds no files")
    for _, rel in files:
        if not all(valid_name(part) for part in rel.split("/")):
            raise SystemExit(f"re_submit: unsupported file name in target: {rel!r}")
    return files


def normalize_executable(value):
    """Normalise one --executable value to a target-relative posix path ('./a//b' -> 'a/b')."""
    if not isinstance(value, str) or "\x00" in value:
        raise SystemExit(f"re_submit: --executable must be a relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(
            f"re_submit: --executable must be a relative path inside the target: {value!r}")
    return path.as_posix()


def normalize_executables(values):
    """Normalised --executable values, each once, in order."""
    return list(dict.fromkeys(normalize_executable(value) for value in values))


def check_extra(extra):
    """Validate optional job.json metadata: only agent, conversation and source, as ids."""
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise SystemExit("re_submit: extra job metadata must be a mapping")
    unknown = [key for key in extra if key not in EXTRA_KEYS]
    if unknown:
        raise SystemExit(
            "re_submit: unsupported job metadata: " + ", ".join(repr(key) for key in unknown))
    for key, value in extra.items():
        if not isinstance(value, str) or not EXTRA_KEYS[key].fullmatch(value):
            raise SystemExit(f"re_submit: job metadata {key!r} has an unsupported value")
    return dict(extra)


def stage_job(
        staging, job_id, target, question, model, max_cost, executables=(), *, extra=None):
    """Build the inbox layout under `staging` and return the job.json dict."""
    extra = check_extra(extra)
    files = collect_target(target)
    by_relative = {rel: source for source, rel in files}
    executable_set = set()
    for value in executables:
        relative = normalize_executable(value)
        if relative not in by_relative:
            known = sorted(by_relative)
            listing = ", ".join(repr(rel) for rel in known[:MISS_LIST_MAX])
            more = f" (and {len(known) - MISS_LIST_MAX} more)" if len(known) > MISS_LIST_MAX else ""
            raise SystemExit(
                f"re_submit: --executable {value!r} does not name a collected target file; "
                f"collected: {listing}{more}")
        executable_set.add(relative)
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
    meta.update(extra)
    (staging / "job.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return meta


def run(argv):
    subprocess.run(argv, check=True, shell=False, stdin=subprocess.DEVNULL)


def _remove_remote(host, path):
    try:
        run(["ssh", host, "rm", "-rf", "--", path])
    except (subprocess.CalledProcessError, OSError):
        pass


def upload(host, staging, job_id, executables):
    """Copy the staged job to a hidden inbox path, fix modes, and rename it into place."""
    hidden = f"{REMOTE_ROOT}/jobs/inbox/.incoming-{job_id}"
    final = f"{REMOTE_ROOT}/jobs/inbox/{job_id}"
    steps = [
        ("upload", ["scp", "-p", "-r", "-q", "--", str(staging), f"{host}:{hidden}"]),
        # The setgid inbox grants the deployment group access so the hidden upload can be
        # renamed into place; nobody else receives access.
        ("permissions", ["ssh", host, "chmod", "-R", "u+rwX,g+rwX,o-rwx", "--", hidden]),
    ]
    if executables:
        steps.append(("executable modes", [
            "ssh", host, "chmod", "0755", "--",
            *(shlex.quote(f"{hidden}/target/{relative}") for relative in executables),
        ]))
    # -T: an existing destination makes the rename fail instead of nesting the upload inside it.
    steps.append(("rename", ["ssh", host, "mv", "-T", "--", hidden, final]))
    for step, argv in steps:
        try:
            run(argv)
        except (subprocess.CalledProcessError, OSError) as exc:
            _remove_remote(host, hidden)
            if isinstance(exc, subprocess.CalledProcessError):
                detail = f"exit code {exc.returncode}"
            else:
                detail = f"{argv[0]} could not start: {type(exc).__name__}"
            raise SystemExit(
                f"re_submit: {step} step failed ({detail}) for {job_id}; "
                "the hidden upload was removed (best effort)") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("question")
    parser.add_argument("--model", help="default openai/gpt-6.1-sol")
    parser.add_argument("--max-cost", type=float, help=f"USD, at most {MAX_COST_USD:g}")
    parser.add_argument("--host", default=os.environ.get("RE_LAB_HOST"),
                        help="Docker host SSH target (or set RE_LAB_HOST); not needed for --dry-run")
    parser.add_argument(
        "--executable", action="append", default=[], metavar="REL",
        help="relative target path to stage as mode 0755; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="stage and print job.json; copy nothing")
    args = parser.parse_args(argv)
    if not args.dry_run:
        if not args.host:
            raise SystemExit("re_submit: set --host or RE_LAB_HOST")
        if not HOST_RE.fullmatch(args.host):
            raise SystemExit("re_submit: unsupported host name")
    if args.model and not MODEL_RE.fullmatch(args.model):
        raise SystemExit("re_submit: unsupported model name")
    if args.max_cost is not None and not 0.01 <= args.max_cost <= MAX_COST_USD:
        raise SystemExit(f"re_submit: --max-cost must be between 0.01 and {MAX_COST_USD:g}")
    if not args.question.strip():
        raise SystemExit("re_submit: the question is empty")
    try:
        args.question.encode("utf-8")
    except UnicodeEncodeError:
        raise SystemExit("re_submit: the question is not valid UTF-8") from None

    executables = normalize_executables(args.executable)
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
        upload(args.host, staging, job_id, executables)
    print(job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
