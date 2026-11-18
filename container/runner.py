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
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_JOB_ROOT = Path("/job")
DEFAULT_PROMPT = Path("/opt/re-lab/prompts/re-job.md")
DEFAULT_PROFILE_SRC = Path("/opt/re-lab/omp-profile")
DEFAULT_MODEL = "openai/gpt-6.1-sol"
MAX_COST_USD = 20.0
MIN_COST_USD = 0.01
EXIT_COST_CAP = 3
# OMP's --max-time ends the run but exits 0 (or 1 when it aborts a request), so the runner times
# the run itself: a run that used the whole budget, short of this margin, is a time cap.
MAX_TIME_S = 7200
TIME_CAP_MARGIN_S = 5
JOB_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,99}$")
STATUS_MAX_BYTES = 4096
STATUS_REASON_MAX = 1000
# Keeps the runner's status.json under STATUS_MAX_BYTES whatever characters the reason uses.
STATUS_REASON_MAX_BYTES = 3000
QUESTION_MAX_BYTES = 5 * 1024 ** 2
REPORT_STDOUT_MAX_BYTES = 1024 ** 2
FAILED_MESSAGE = ('the runner could not complete this job; the "error" field in result.json '
                  'names the reason')
PROFILE_RELATIVE = Path(".omp") / "profiles" / "re-lab"
HIGH_ROLES = ("default", "smol", "slow", "vision", "plan", "commit", "task", "advisor")
PLAIN_ROLES = ("tiny", "memory", "judge", "widget")
OK_STATUSES = frozenset({"done", "cost-cap", "needs-vm-lane", "time-cap"})
# status.json "error" vocabulary; omp-exit-<N> covers OMP's non-zero exit codes 1..255.
ERROR_CODES = frozenset({
    "invalid-job", "invalid-model", "invalid-cost", "invalid-question", "prompt-missing",
    "profile-missing", "omp-launch-failed", "output-unwritable", "runner-error",
})
OMP_EXIT_ERROR_RE = re.compile(r"omp-exit-(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]?)")
# Names in out/ the runner writes or reads after OMP (same uid) has had the directory.
GUARDED_OUT_NAMES = ("report.md", "status.json", "status.json.tmp")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class RunnerError(Exception):
    """The prepared job cannot be run safely; `code` is its status.json error value."""

    def __init__(self, message: str, code: str = "runner-error"):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "runner-error"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise RunnerError("job id must match ^[a-z0-9-]{8,64}$", "invalid-job")
    return job_id


def is_error_code(value) -> bool:
    if not isinstance(value, str):
        return False
    return value in ERROR_CODES or OMP_EXIT_ERROR_RE.fullmatch(value) is not None


def clamp_cost(value) -> float:
    if value is None:
        return MAX_COST_USD
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RunnerError("max_cost_usd must be a finite number", "invalid-cost")
    if value < MIN_COST_USD:
        raise RunnerError(f"max_cost_usd must be at least {MIN_COST_USD}", "invalid-cost")
    return min(float(value), MAX_COST_USD)


def format_cost(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_max_time(seconds: int) -> str:
    """OMP --max-time value for a whole number of seconds ("2h" for MAX_TIME_S)."""
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def validate_model(value) -> str:
    if value is None:
        return DEFAULT_MODEL
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        raise RunnerError("model has an unsupported shape", "invalid-model")
    return value


def load_job(job_root: Path, job_id: str) -> tuple[str, float]:
    path = Path(job_root) / "in" / "job.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise RunnerError("prepared job metadata is unavailable", "invalid-job") from None
    if not isinstance(body, dict) or body.get("job_id") != job_id:
        raise RunnerError("prepared job id does not match", "invalid-job")
    return validate_model(body.get("model")), clamp_cost(body.get("max_cost_usd"))


def _read_regular(path: Path, cap: int, *, follow: bool) -> bytes | None:
    """Return a regular file's bytes if it holds at most `cap`; FIFOs and devices never block."""
    flags = os.O_RDONLY | _NONBLOCK | _BINARY | (0 if follow else _NOFOLLOW)
    try:
        if not follow and not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
                return None
            data = handle.read(cap + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= cap else None


def check_question(path: Path) -> None:
    """The question must be a regular file (never a link), at most 5 MiB, in valid UTF-8."""
    data = _read_regular(path, QUESTION_MAX_BYTES, follow=False)
    if data is None:
        raise RunnerError("prepared question is not a bounded regular file", "invalid-question")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise RunnerError("prepared question is not UTF-8", "invalid-question") from None


def _is_nonempty_file(path: Path) -> bool:
    """True for a readable, non-empty regular file (following links, as OMP does)."""
    flags = os.O_RDONLY | _NONBLOCK | _BINARY
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            return stat.S_ISREG(info.st_mode) and info.st_size > 0 and handle.read(1) != b""
    except (OSError, ValueError):
        return False


def check_prompt(path: Path) -> None:
    # OMP's --append-system-prompt treats an unreadable path as literal prompt text.
    if not _is_nonempty_file(Path(path)):
        raise RunnerError("prompt file is missing, empty, or not a regular file", "prompt-missing")


def check_profile(profile_src) -> Path:
    """Require the hardened profile source: a directory with non-empty config.yml and mcp.json."""
    if profile_src is None:
        raise RunnerError("profile source is not configured", "profile-missing")
    source = Path(profile_src)
    try:
        is_dir = stat.S_ISDIR(os.stat(source).st_mode)
    except (OSError, ValueError):
        is_dir = False
    if not is_dir or not all(_is_nonempty_file(source / name) for name in ("config.yml", "mcp.json")):
        raise RunnerError("profile source lacks config.yml or mcp.json", "profile-missing")
    return source


def read_vm_lane_request(out_dir: Path) -> str | None:
    """Return a valid VM-lane reason from the agent output; links and open schemas are ignored."""
    data = _read_regular(Path(out_dir) / "status.json", STATUS_MAX_BYTES, follow=False)
    if data is None:
        return None
    try:
        body = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or set(body) != {"status", "reason"}:
        return None
    reason = body["reason"]
    if (body["status"] == "needs-vm-lane" and isinstance(reason, str)
            and 1 <= len(reason) <= STATUS_REASON_MAX and reason.isprintable()
            and len(reason.encode("utf-8")) <= STATUS_REASON_MAX_BYTES):
        return reason
    return None


def build_omp_argv(omp_cmd, model: str, max_cost: float, session_dir: Path,
                   prompt_path: Path, question: Path) -> list:
    return [
        *omp_cmd, "-p", "--profile", "re-lab", "--model", model, "--thinking", "high",
        "--max-cost", format_cost(max_cost), "--max-time", format_max_time(MAX_TIME_S),
        "--session-dir", str(session_dir), "--no-title",
        "--no-extensions", "--no-context-files", "--no-rules",
        "--append-system-prompt", str(prompt_path), "@" + str(question),
    ]


def omp_environment(base=None) -> dict:
    """OMP's environment: a job without its MCP servers (REA) fails instead of reporting done."""
    env = dict(os.environ if base is None else base)
    env["OMP_MCP_REQUIRE_READY"] = "1"
    return env


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


def normalize_exit_code(returncode: int) -> int:
    """Map a subprocess return code to 0..255; a signal N becomes 128+N like a shell."""
    if 0 <= returncode <= 255:
        return returncode
    if returncode < 0:
        return min(128 - returncode, 255)
    return 255


def classify_exit(exit_code: int, elapsed_s: float, vm_reason: str | None) -> tuple[str, str | None]:
    """Return (status, error) for a finished OMP run."""
    if exit_code == EXIT_COST_CAP:
        return "cost-cap", None
    if elapsed_s >= MAX_TIME_S - TIME_CAP_MARGIN_S:
        return "time-cap", None
    if exit_code == 0:
        return ("needs-vm-lane" if vm_reason is not None else "done"), None
    return "failed", f"omp-exit-{exit_code}"


def _quarantine(out_dir: Path, name: str) -> bool:
    """Rename a non-regular out/<name> (link, directory, FIFO...) aside without following it."""
    path = Path(out_dir) / name
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISREG(info.st_mode):
        return False
    os.rename(path, Path(out_dir) / f".quarantine-{name}-{uuid.uuid4().hex}")
    return True


def _quarantine_entries(out_dir: Path, names=GUARDED_OUT_NAMES) -> None:
    for name in names:
        try:
            _quarantine(out_dir, name)
        except OSError:
            # A later exclusive write to the same name surfaces any real failure.
            pass


def _write_atomic(out_dir: Path, name: str, data: bytes) -> None:
    """Atomically place `data` at out/<name>: O_EXCL temporary file, then rename over the name."""
    out_dir = Path(out_dir)
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            try:
                # mkstemp creates 0600; keep the umask-default mode earlier runners wrote.
                os.fchmod(handle.fileno(), 0o644)
            except (AttributeError, OSError):
                pass
            handle.write(data)
        try:
            os.replace(temporary, out_dir / name)
        except OSError:
            # Something non-regular took the name since the last check: move it aside, retry once.
            _quarantine(out_dir, name)
            os.replace(temporary, out_dir / name)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _create_output(out_dir: Path, name: str):
    """Create a fresh out/<name> for OMP's output without following or reusing anything there."""
    path = Path(out_dir) / name
    try:
        _quarantine(out_dir, name)
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o644)
    except OSError:
        raise RunnerError(f"cannot create out/{name}", "output-unwritable") from None
    return os.fdopen(fd, "w+b")


def _stdout_excerpt(handle) -> str | None:
    """At most REPORT_STDOUT_MAX_BYTES of OMP's stdout, read through the runner's own descriptor."""
    if handle is None:
        return None
    try:
        handle.flush()
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        handle.seek(0)
        data = handle.read(REPORT_STDOUT_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not data:
        return None
    text = data[:REPORT_STDOUT_MAX_BYTES].decode("utf-8", errors="replace")
    if len(data) > REPORT_STDOUT_MAX_BYTES:
        text += "\n[output truncated at 1 MiB]"
    return text


def _ensure_report(out_dir: Path, status: str, stdout_handle) -> None:
    """Keep a non-empty regular out/report.md; otherwise write a fixed stub naming the status."""
    try:
        info = os.lstat(Path(out_dir) / "report.md")
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and info.st_size > 0:
        return
    parts = [f"# Report: incomplete\nThe agent did not write out/report.md; status: {status}.\n"]
    if status == "failed":
        parts.append(f"\n{FAILED_MESSAGE}.\n")
    excerpt = _stdout_excerpt(stdout_handle)
    if excerpt is not None:
        parts.append(f"\n## Final agent output (unstructured)\n\n{excerpt}")
        if not excerpt.endswith("\n"):
            parts.append("\n")
    _write_atomic(out_dir, "report.md", "".join(parts).encode("utf-8"))


def _result(job_id: str, status: str, exit_code, model: str | None,
            started: str, reason: str | None = None, error: str | None = None) -> dict:
    body = {
        "job_id": job_id,
        "status": status,
        "exit_code": exit_code,
        "model": model,
        "started": started,
        "ended": utc_now(),
    }
    if reason is not None and status == "needs-vm-lane":
        body["reason"] = reason
    if status == "failed":
        body["error"] = error if is_error_code(error) else "runner-error"
    return body


def _status_bytes(body: dict) -> bytes:
    return (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class _Run:
    """Mutable outcome of one job: failed (error undecided) until the run proves otherwise."""

    def __init__(self):
        self.status = "failed"
        self.error = None
        self.exit_code = None
        self.model = None
        self.reason = None
        self.stdout = None
        self.stderr = None

    def fail(self, error: str) -> None:
        # The first failure is the cause; later ones (such as an unwritable report) are fallout.
        if self.status != "failed" or self.error is None:
            self.error = error
        self.status = "failed"
        self.reason = None

    def close(self) -> None:
        for handle in (self.stdout, self.stderr):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass


def _run_omp(job_root: Path, job_id: str, omp_cmd, prompt_path: Path, profile_src,
             home: Path, run: _Run) -> None:
    run.model, max_cost = load_job(job_root, job_id)
    in_dir = job_root / "in"
    out_dir = job_root / "out"
    session_dir = job_root / "session"
    question = in_dir / "question.md"
    check_question(question)
    check_prompt(prompt_path)
    profile = check_profile(profile_src)
    try:
        session_dir.mkdir(exist_ok=True)
        _quarantine(out_dir, "status.json")
        (out_dir / "status.json").unlink(missing_ok=True)
    except OSError:
        raise RunnerError("job output directories are unusable", "output-unwritable") from None
    sync_profile(profile, home, run.model)
    argv = build_omp_argv(omp_cmd, run.model, max_cost, session_dir, prompt_path, question)
    run.stdout = _create_output(out_dir, "omp.stdout")
    run.stderr = _create_output(out_dir, "omp.stderr")
    began = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=job_root, stdin=subprocess.DEVNULL, stdout=run.stdout, stderr=run.stderr,
            env=omp_environment(), shell=False, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RunnerError("OMP could not be started", "omp-launch-failed") from None
    elapsed = time.monotonic() - began
    run.exit_code = normalize_exit_code(completed.returncode)
    capped = run.exit_code != EXIT_COST_CAP and elapsed >= MAX_TIME_S - TIME_CAP_MARGIN_S
    reason = read_vm_lane_request(out_dir) if run.exit_code == 0 and not capped else None
    run.status, error = classify_exit(run.exit_code, elapsed, reason)
    run.reason = reason if run.status == "needs-vm-lane" else None
    run.error = error


def run_job(job_root: Path, job_id: str, omp_cmd=("omp",),
            prompt_path: Path = DEFAULT_PROMPT, profile_src=None, home=None) -> dict:
    """Run exactly one host-prepared job and leave status/report/session for the host publisher."""
    job_root = Path(job_root)
    check_job_id(job_id)
    started = utc_now()
    out_dir = job_root / "out"
    out_dir.mkdir(exist_ok=True)
    run = _Run()
    try:
        try:
            _run_omp(job_root, job_id, omp_cmd, Path(prompt_path), profile_src,
                     Path(home) if home is not None else Path.home(), run)
        except RunnerError as exc:
            _log_failure(job_id, exc.code, exc)
            run.fail(exc.code)
        except Exception as exc:
            # Only the class name is logged; exception text never reaches status or report.
            _log_failure(job_id, "runner-error", exc)
            run.fail("runner-error")
        try:
            # OMP (same uid) may have left links, directories or FIFOs at the names written below.
            _quarantine_entries(out_dir)
            _ensure_report(out_dir, run.status, run.stdout)
        except Exception as exc:
            _log_failure(job_id, "output-unwritable", exc)
            run.fail("output-unwritable")
    finally:
        run.close()
        result = _result(job_id, run.status, run.exit_code, run.model, started,
                         run.reason, run.error)
        _write_atomic(out_dir, "status.json", _status_bytes(result))
    return result


def _log_failure(job_id: str, code: str, exc: BaseException) -> None:
    print(f"re-runner: {job_id}: failed: {code} ({type(exc).__name__})",
          file=sys.stderr, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--job-root", type=Path, default=DEFAULT_JOB_ROOT)
    parser.add_argument("--profile-src", type=Path, default=DEFAULT_PROFILE_SRC)
    args = parser.parse_args(argv)
    try:
        result = run_job(args.job_root, args.job, prompt_path=DEFAULT_PROMPT,
                         profile_src=args.profile_src, home=Path.home())
    except (RunnerError, OSError) as exc:
        # No status.json could be written (invalid job id or unwritable out/): the host fails it.
        print(f"re-runner: failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 1
    error = result.get("error")
    print(f"re-runner: {args.job}: {result['status']}" + (f" ({error})" if error else ""),
          flush=True)
    return 0 if result["status"] in OK_STATUSES else 1


if __name__ == "__main__":
    sys.exit(main())
