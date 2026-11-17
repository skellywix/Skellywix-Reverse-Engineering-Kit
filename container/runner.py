#!/usr/bin/env python3
"""Run one host-prepared reverse-engineering job inside an isolated agent container."""
import argparse
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_JOB_ROOT = Path("/job")
DEFAULT_PROMPT = Path("/opt/re-lab/prompts/re-job.md")
DEFAULT_MODEL = "openai/gpt-6.1-sol"
MAX_COST_USD = 20.0
MIN_COST_USD = 0.01
EXIT_COST_CAP = 3
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,99}$")
STATUS_MAX_BYTES = 4096
STATUS_REASON_MAX = 1000
FAILED_MESSAGE = "the runner failed while running this job; see the runner log"
PROFILE_RELATIVE = Path(".omp") / "profiles" / "re-lab"
HIGH_ROLES = ("default", "smol", "slow", "vision", "plan", "commit", "task", "advisor")
PLAIN_ROLES = ("tiny", "memory", "judge")


class RunnerError(Exception):
    """The prepared job cannot be run safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise RunnerError("job id must match ^[a-z0-9-]{8,64}$")
    return job_id


def clamp_cost(value) -> float:
    if value is None:
        return MAX_COST_USD
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RunnerError("max_cost_usd must be a finite number")
    if value < MIN_COST_USD:
        raise RunnerError(f"max_cost_usd must be at least {MIN_COST_USD}")
    return min(float(value), MAX_COST_USD)


def format_cost(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def validate_model(value) -> str:
    if value is None:
        return DEFAULT_MODEL
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        raise RunnerError("model has an unsupported shape")
    return value


def load_job(job_root: Path, job_id: str) -> tuple[str, float]:
    path = Path(job_root) / "in" / "job.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RunnerError("prepared job metadata is unavailable") from None
    if not isinstance(body, dict) or body.get("job_id") != job_id:
        raise RunnerError("prepared job id does not match")
    return validate_model(body.get("model")), clamp_cost(body.get("max_cost_usd"))


def write_json(path: Path, body: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_vm_lane_request(out_dir: Path) -> str | None:
    """Return a valid VM-lane reason from the agent output; links and open schemas are ignored."""
    path = Path(out_dir) / "status.json"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > STATUS_MAX_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            body = json.loads(handle.read(STATUS_MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    if (set(body) == {"status", "reason"} and body["status"] == "needs-vm-lane"
            and isinstance(reason, str) and 1 <= len(reason) <= STATUS_REASON_MAX):
        return reason
    return None


def build_omp_argv(omp_cmd, model: str, max_cost: float, session_dir: Path,
                   prompt_path: Path, question: Path) -> list:
    return [
        *omp_cmd, "-p", "--profile", "re-lab", "--model", model, "--thinking", "high",
        "--max-cost", format_cost(max_cost), "--max-time", "2h",
        "--session-dir", str(session_dir), "--no-title",
        "--no-extensions", "--no-context-files", "--no-rules",
        "--append-system-prompt", str(prompt_path), "@" + str(question),
    ]


def sync_profile(profile_src: Path, home: Path, model: str) -> None:
    """Rebuild the per-container profile and pin every model role to this job's model."""
    profile = Path(home) / PROFILE_RELATIVE
    if profile.is_symlink():
        profile.unlink()
    elif profile.exists():
        shutil.rmtree(profile)
    dest = profile / "agent"
    dest.mkdir(parents=True)
    for item in Path(profile_src).iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copyfile(item, target)
    config = dest / "config.yml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    lines = [text if not text or text.endswith("\n") else text + "\n", "modelRoles:\n"]
    lines += [f"  {role}: {json.dumps(model + ':high')}\n" for role in HIGH_ROLES]
    lines += [f"  {role}: {json.dumps(model)}\n" for role in PLAIN_ROLES]
    config.write_text("".join(lines), encoding="utf-8")


def _result(job_id: str, status: str, exit_code, model: str | None,
            started: str, reason: str | None = None) -> dict:
    body = {
        "job_id": job_id,
        "status": status,
        "exit_code": exit_code,
        "model": model,
        "started": started,
        "ended": utc_now(),
    }
    if reason is not None:
        body["reason"] = reason
    return body


def _run_job(job_root: Path, job_id: str, omp_cmd, prompt_path: Path,
             profile_src: Path | None, home: Path, started: str) -> dict:
    model, max_cost = load_job(job_root, job_id)
    in_dir = job_root / "in"
    out_dir = job_root / "out"
    session_dir = job_root / "session"
    question = in_dir / "question.md"
    if question.is_symlink() or not question.is_file():
        raise RunnerError("prepared question is unavailable")
    out_dir.mkdir(exist_ok=True)
    session_dir.mkdir(exist_ok=True)
    (out_dir / "status.json").unlink(missing_ok=True)
    if profile_src is not None:
        sync_profile(profile_src, home, model)
    argv = build_omp_argv(omp_cmd, model, max_cost, session_dir, Path(prompt_path), question)
    stdout_path = out_dir / "omp.stdout"
    stderr_path = out_dir / "omp.stderr"
    with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
        completed = subprocess.run(
            argv, cwd=job_root, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            shell=False, check=False)
    exit_code = completed.returncode
    reason = read_vm_lane_request(out_dir) if exit_code == 0 else None
    if reason is not None:
        status = "needs-vm-lane"
    elif exit_code == 0:
        status = "done"
    elif exit_code == EXIT_COST_CAP:
        status = "cost-cap"
    else:
        status = "failed"
    report = out_dir / "report.md"
    if not report.is_file() or report.is_symlink():
        shutil.copyfile(stdout_path, report)
    return _result(job_id, status, exit_code, model, started, reason)


def run_job(job_root: Path, job_id: str, omp_cmd=("omp",),
            prompt_path: Path = DEFAULT_PROMPT, profile_src=None, home=None) -> dict:
    """Run exactly one host-prepared job and leave status/report/session for the host publisher."""
    job_root = Path(job_root)
    check_job_id(job_id)
    started = utc_now()
    out_dir = job_root / "out"
    out_dir.mkdir(exist_ok=True)
    try:
        result = _run_job(
            job_root, job_id, omp_cmd, Path(prompt_path),
            Path(profile_src) if profile_src is not None else None,
            Path(home) if home is not None else Path.home(), started)
    except Exception as exc:
        # Any in-container failure is terminal for this job; expose only its class to avoid data leaks.
        print(f"re-runner: {job_id}: failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        result = _result(job_id, "failed", None, None, started)
        (out_dir / "report.md").write_text(FAILED_MESSAGE + "\n", encoding="utf-8")
    write_json(out_dir / "status.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--job-root", type=Path, default=DEFAULT_JOB_ROOT)
    parser.add_argument("--profile-src", type=Path, default=Path("/opt/re-lab/omp-profile"))
    args = parser.parse_args(argv)
    profile_src = args.profile_src if args.profile_src.is_dir() else None
    result = run_job(
        args.job_root, args.job, profile_src=profile_src, home=Path.home())
    print(f"re-runner: {args.job}: {result['status']}", flush=True)
    return 0 if result["status"] in {"done", "cost-cap", "needs-vm-lane"} else 1


if __name__ == "__main__":
    sys.exit(main())
