"""One-click dependency maintenance for the portable workspace.

The checkout defines the allowed publishers, update channels, package declarations, and fixed qualification inputs.
An update resolves moving channels once, verifies the concrete dependencies, and records them in a private set.
Only a fully installed set replaces ``<root>/dependencies/active.json``. Status and normal runs do not resolve
channels, execute upstream reference material, or edit a checkout.

Trust boundaries:
  * The browser supplies no URL, tag, path, or command. First-party policy discovery and publisher resolution are
    separate. A downloaded policy cannot add a capability or switch the publishers authorized by the checkout.
  * Only the Docker client runs on the host. Cargo and npm resolve packages as an unprivileged container user;
    npm lifecycle scripts are disabled. Offline builds consume the selected immutable inputs.
  * REA retains three fixed reference files as data. Decompile and rust-anything retain release identities only.
    None of these projects installs software, registers a tool, or supplies instructions to a model.
  * Fixed compatibility fixtures and qualification evidence are not ordinary rolling dependencies.
  * An update writes only under ``<root>/dependencies``. Open cases, journals and run inputs live elsewhere. Loaded
    processes keep what they loaded: ``status()['restart_required']`` says when one must restart; new jobs read the
    selection through ``image_for()`` / ``asset_path()`` as they start.

CI tooling (GitHub Actions pins) is a separate ``ci`` scope: checked against the workflows, never installed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

MANIFEST_NAME = "workspace-dependencies.json"
STATE_DIR = "dependencies"
SCHEMA = 1
FIRST_PARTY = ("skellywix/Skellywix-Reverse-Engineering-Kit", "main")
MANIFEST_LIMIT = 1 << 20
RECEIPT_LIMIT = 4 << 20
ARCHIVE_LIMIT = 1 << 29
REFERENCE_FILE_LIMIT = 8 << 20
MESSAGE_LIMIT = 400
PLATFORM = "linux/amd64"
CORE_IMAGE_KEYS = ("compiler", "python", "node")

SHA256 = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
SET_ID = re.compile(r"r[0-9]+-[0-9a-f]{12}(?:-[0-9a-f]{6})?")
REGISTRIES = ("docker.io", "ghcr.io", "quay.io", "mcr.microsoft.com", "gcr.io")
REPOSITORY = re.compile(r"(?:[a-z0-9.-]+)(?:/[a-z0-9._-]+){1,4}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CRATES_SOURCES = ("registry+https://github.com/rust-lang/crates.io-index", "sparse+https://index.crates.io/")
FETCH_HOSTS = frozenset({"codeload.github.com", "github.com", "api.github.com", "raw.githubusercontent.com"})
VERSION_TOOLS = frozenset({"rustc", "cargo", "node", "npm", "python3", "i686-w64-mingw32-ld", "cc", "c++", "make", "cmake"})
FAMILIES = ("oci-image", "oci-build", "cargo-lock", "npm-lock", "reference-archive", "composite",
            "release-metadata", "operator-asset", "ci-actions")
INSTALLABLE = ("oci-image", "oci-build", "cargo-lock", "npm-lock", "reference-archive", "release-metadata")
SOURCE_BOUND = ("oci-image", "oci-build", "cargo-lock", "npm-lock")
ACTIVATIONS = ("new-jobs", "restart")
QUIET_DISCOVERY = ("current", "newer", "bootstrap")

COMMON_KEYS = {"name", "family", "scope", "optional", "version", "activation", "note", "track"}
FAMILY_KEYS = {
    "oci-image": {"key", "repository", "tag", "digest", "verify", "checkout_pin"},
    "oci-build": {"key", "base", "recipe", "recipe_sha256", "source_files", "reproducible", "verify"},
    "cargo-lock": {"image", "manifest", "manifest_sha256", "context", "lock", "lock_sha256"},
    "npm-lock": {"image", "manifest", "manifest_sha256", "lock", "lock_sha256"},
    "reference-archive": {"repository", "tag", "commit", "files"},
    "release-metadata": {"repository", "tag", "commit"},
    "composite": {"requires"},
    "operator-asset": {"path", "sha256"},
    "ci-actions": {"workflows", "actions"},
}
FAMILY_OPTIONAL = {
    "oci-image": {"checkout_pin"},
    "oci-build": {"base", "verify", "source_files", "reproducible"},
    "cargo-lock": {"manifest_sha256"},
}
TRACK_OMITS = {"oci-image": {"digest"}, "oci-build": set(), "cargo-lock": {"lock_sha256"},
               "npm-lock": {"lock_sha256"}, "reference-archive": {"tag", "commit"}, "release-metadata": {"tag", "commit"}}
TOP_KEYS = {"schema", "revision", "reviewed", "review", "distribution", "components"}

Runner = Callable[[list, int], "tuple[int, str]"]
Fetcher = Callable[[str, Path, int], str]


class MaintenanceError(RuntimeError):
    """An update step refused or failed. The message is safe to show."""


class ManifestError(MaintenanceError):
    """The reviewed manifest is malformed, unreviewed or inconsistent."""


class PrerequisiteError(MaintenanceError):
    """A tool or service the update needs is not available."""


class NotFound(MaintenanceError):
    """The publisher definitively has no such file (HTTP 404), as opposed to being unreachable."""


class Damaged(MaintenanceError):
    """The active selection or its receipt is not what was installed."""


class Unavailable(MaintenanceError):
    """No pinned image or asset exists for the requested key."""


# ------------------------------------------------------------------------------------------------ small helpers

def _clip(text: Any, limit: int = MESSAGE_LIMIT) -> str:
    text = "".join(c if c == "\n" or 32 <= ord(c) < 127 or ord(c) > 159 else "?" for c in str(text)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _reject_duplicates(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _loads(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError) as error:
        raise MaintenanceError(f"{label} is not valid JSON") from error


def _read_limited(path: Path, limit: int, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as error:
        raise MaintenanceError(f"{label} cannot be read") from error
    if len(data) > limit:
        raise MaintenanceError(f"{label} is too large")
    return data


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _fsync_write(path: Path, data: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return  # already gone: the goal of the call


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step; readers see the old bytes or the new bytes, never a mixture."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        _fsync_write(temporary, data)
        os.replace(temporary, path)
    except BaseException:
        try:
            _remove_file(temporary)
        except OSError:  # cleanup of our own temporary file; the original error is re-raised below
            pass
        raise


def _rmtree(path: Path) -> None:
    def writable(function, target, _error):
        os.chmod(target, stat.S_IWRITE | stat.S_IRUSR | stat.S_IXUSR | stat.S_IWUSR)
        function(target)

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def _file_sha(path: Path, *, text: bool) -> str:
    """SHA-256 of a regular file. Text pins hash LF-normalized bytes, which equal the committed git blob."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if text:
            digest.update(_lf(stream.read()))
        else:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> tuple[str, int]:
    """Digest every file and relative symlink under ``root``. Links that escape the tree are refused."""
    root = Path(root)
    entries: list[str] = []

    def link(rel: str, full: str) -> None:
        target = os.readlink(full)
        norm = os.path.normpath(os.path.join(os.path.dirname(rel), target)).replace("\\", "/")
        if os.path.isabs(target) or norm == ".." or norm.startswith("../"):
            raise MaintenanceError("an installed tree contains a link that leaves it")
        entries.append(f"L {rel} {target}")

    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in list(directories):
            full = os.path.join(current, name)
            if _is_link(os.lstat(full)):
                directories.remove(name)
                link(os.path.relpath(full, root).replace(os.sep, "/"), full)
        for name in sorted(files):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            info = os.lstat(full)
            if _is_link(info):
                link(rel, full)
            elif stat.S_ISREG(info.st_mode):
                entries.append(f"F {rel} {info.st_size} {_file_sha(Path(full), text=False)}")
            else:
                raise MaintenanceError("an installed tree contains a special file")
    entries.sort()
    return _sha("\n".join(entries).encode()), len(entries)


def _short_repository(repository: str) -> set:
    names = {repository}
    if repository.startswith("docker.io/"):
        names.add(repository[len("docker.io/"):])
        if repository.startswith("docker.io/library/"):
            names.add(repository[len("docker.io/library/"):])
    return names


# ------------------------------------------------------------------------------------------------ manifest

class Manifest:
    """A validated, immutable view of one reviewed ``workspace-dependencies.json``."""

    def __init__(self, raw: dict, data: bytes):
        self.raw = raw
        self.data = data
        self.sha256 = _sha(data)
        self.revision: int = raw["revision"]
        self.components: list = raw["components"]
        self.by_name = {c["name"]: c for c in self.components}
        self.images = {c["key"]: c for c in self.components if c["family"] in ("oci-image", "oci-build")}
        self._identity: dict = {}

    def identity(self, name: str) -> str:
        """Digest of one component entry plus the identity of the image it installs with."""
        if name not in self._identity:
            c = self.by_name[name]
            dependency = self.identity(c["image"]) if c.get("image") else None
            self._identity[name] = _sha(_canonical({"c": c, "image": dependency}))
        return self._identity[name]

    def ordered(self) -> list:
        """Installable components, base images first."""
        rank = {"oci-image": 0, "oci-build": 1}
        return sorted((c for c in self.components if c["family"] in INSTALLABLE),
                      key=lambda c: rank.get(c["family"], 2))


def _fail(message: str) -> None:
    raise ManifestError(message)


def _text(value: Any, label: str, pattern: re.Pattern | None = None, limit: int = 300) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(ch) < 32 for ch in value):
        _fail(f"{label} must be a short plain string")
    if pattern is not None and not pattern.fullmatch(value):
        _fail(f"{label} has an invalid form")
    return value


def _relative(value: Any, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if (text.startswith("/") or "\\" in text or ":" in text or text == ".."
            or any(part in ("", "..") for part in parts if part != ".")):
        _fail(f"{label} must be a relative path inside the checkout")
    return text


def _digest(value: Any, label: str) -> str:
    return _text(value, label, SHA256, 64)


def _verify_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        _fail(f"{label} needs at least one executable check")
    for item in value:
        if not isinstance(item, dict) or set(item) != {"argv", "expect"}:
            _fail(f"{label} checks need exactly argv and expect")
        argv = item["argv"]
        if (not isinstance(argv, list) or not all(isinstance(part, str) for part in argv)
                or not (argv == ["rustup", "target", "list", "--installed"]
                        or len(argv) == 2 and argv[0] in VERSION_TOOLS and argv[1] == "--version")):
            _fail(f"{label} runs a command that is not allowed")
        try:
            re.compile(_text(item["expect"], f"{label} expectation"))
        except re.error:
            _fail(f"{label} expectation is not a valid pattern")


def _image_reference(repository: Any, label: str) -> str:
    text = _text(repository, f"{label} repository", REPOSITORY, 200)
    if text.split("/", 1)[0] not in REGISTRIES:
        _fail(f"{label} names a registry that is not on the public allow list")
    return text


def validate_manifest(raw: Any, data: bytes) -> Manifest:
    if not isinstance(raw, dict) or set(raw) - TOP_KEYS or not {"schema", "revision", "reviewed", "components"} <= set(raw):
        _fail("manifest has unexpected or missing top-level keys")
    if raw["schema"] != SCHEMA:
        _fail("manifest schema is not supported by this workspace build")
    if not isinstance(raw["revision"], int) or isinstance(raw["revision"], bool) or raw["revision"] < 1:
        _fail("manifest revision must be a positive integer")
    if raw["reviewed"] is not True:
        _fail("manifest is not marked reviewed; an unreviewed revision is never activated")
    components = raw["components"]
    if not isinstance(components, list) or not components:
        _fail("manifest lists no components")
    names: set = set()
    keys: set = set()
    for c in components:
        if not isinstance(c, dict):
            _fail("a component is not an object")
        name = _text(c.get("name"), "component name", NAME, 41)
        if name in names:
            _fail(f"component {name} is listed twice")
        names.add(name)
        family = c.get("family")
        if family not in FAMILIES:
            _fail(f"{name}: unknown dependency family")
        allowed = COMMON_KEYS | FAMILY_KEYS[family]
        required = {"name", "family", "scope", "version"} | (FAMILY_KEYS[family] - FAMILY_OPTIONAL.get(family, set()))
        tracking = c.get("track", False)
        if "track" in c:
            if tracking is not True or family not in TRACK_OMITS:
                _fail(f"{name}: invalid upstream channel")
            allowed = allowed - TRACK_OMITS[family]
            required -= TRACK_OMITS[family]
        if set(c) - allowed or required - set(c):
            _fail(f"{name}: unexpected or missing fields")
        if c["scope"] != ("ci" if family == "ci-actions" else "runtime"):
            _fail(f"{name}: CI tooling and runtime dependencies must stay separate")
        _text(c["version"], f"{name} version")
        if "optional" in c and not isinstance(c["optional"], bool):
            _fail(f"{name}: optional must be true or false")
        if c.get("activation", "new-jobs") not in ACTIVATIONS:
            _fail(f"{name}: unknown activation")
        c.setdefault("optional", False)
        c.setdefault("activation", "new-jobs")
        if "note" in c:
            _text(c["note"], f"{name} note", limit=600)
        if family in ("oci-image", "oci-build"):
            key = _text(c["key"], f"{name} key", NAME, 41)
            if key in keys:
                _fail(f"{name}: image key {key} is used twice")
            keys.add(key)
            if family == "oci-image" or "base" in c:
                _verify_list(c.get("verify"), name)
        if family == "oci-image":
            _image_reference(c["repository"], name)
            _text(c["tag"], f"{name} tag", TAG, 128)
            if not tracking:
                _text(c["digest"], f"{name} digest", DIGEST, 71)
            pin = c.get("checkout_pin")
            if pin is not None:
                if not isinstance(pin, dict) or set(pin) != {"path", "text"}:
                    _fail(f"{name}: checkout_pin needs path and text")
                _relative(pin["path"], f"{name} pin path")
                _text(pin["text"], f"{name} pin text")
        elif family == "oci-build":
            if "base" in c:
                base = c["base"]
                field = "tag" if tracking else "digest"
                if not isinstance(base, dict) or set(base) != {"repository", field}:
                    _fail(f"{name}: base needs repository and {field}")
                _image_reference(base["repository"], name)
                _text(base[field], f"{name} base {field}", TAG if tracking else DIGEST)
            elif c.get("verify"):
                _verify_list(c["verify"], name)
            _relative(c["recipe"], f"{name} recipe")
            _digest(c["recipe_sha256"], f"{name} recipe digest")
            files = c.get("source_files", {})
            if not isinstance(files, dict):
                _fail(f"{name}: source_files must be an object")
            for path, digest in files.items():
                _relative(path, f"{name} source file")
                _digest(digest, f"{name} source file digest")
            if "reproducible" in c and not isinstance(c["reproducible"], bool):
                _fail(f"{name}: reproducible must be true or false")
        elif family == "cargo-lock":
            for field in ("manifest", "context", "lock"):
                _relative(c[field], f"{name} {field}")
            if tracking or "manifest_sha256" in c:
                _digest(c.get("manifest_sha256"), f"{name} manifest digest")
            if not tracking:
                _digest(c["lock_sha256"], f"{name} lock digest")
        elif family == "npm-lock":
            for field in ("manifest", "lock"):
                _relative(c[field], f"{name} {field}")
            _digest(c["manifest_sha256"], f"{name} manifest digest")
            if not tracking:
                _digest(c["lock_sha256"], f"{name} lock digest")
        elif family in ("reference-archive", "release-metadata"):
            from tools import dependency_candidates as dep
            repo = _text(c["repository"], f"{name} repository", GITHUB_REPO, 120)
            if family == "release-metadata" and dep.REFERENCE_REPOSITORIES.get(name) != repo:
                _fail(f"{name}: unknown metadata-only repository")
            if not tracking:
                _text(c["tag"], f"{name} tag", TAG, 128)
                _text(c["commit"], f"{name} commit", COMMIT, 40)
            if family == "reference-archive":
                files = c["files"]
                if tracking:
                    if name != "rea" or repo != dep.REA_REPOSITORY or files != list(dep.REA_FILES):
                        _fail(f"{name}: upstream reference must use the three REA data files")
                else:
                    if not isinstance(files, dict) or not files:
                        _fail(f"{name}: files must pin at least one archive member")
                    for path, digest in files.items():
                        _relative(path, f"{name} member")
                        _digest(digest, f"{name} member digest")
        elif family == "composite":
            if not isinstance(c["requires"], list) or not c["requires"]:
                _fail(f"{name}: requires must name components")
        elif family == "operator-asset":
            _relative(c["path"], f"{name} path")
            _digest(c["sha256"], f"{name} digest")
        elif family == "ci-actions":
            _relative(c["workflows"], f"{name} workflows")
            actions = c["actions"]
            if not isinstance(actions, dict) or not actions:
                _fail(f"{name}: actions must be listed")
            for repo, pin in actions.items():
                _text(repo, f"{name} action", GITHUB_REPO, 120)
                if isinstance(pin, dict) and set(pin) == {"ref"}:
                    _text(pin["ref"], f"{name} action channel", re.compile(r"v[1-9][0-9]*"))
                    continue
                if not isinstance(pin, dict) or set(pin) != {"sha", "version"}:
                    _fail(f"{name}: every action needs sha and version")
                _text(pin["sha"], f"{name} action sha", COMMIT, 40)
                _text(pin["version"], f"{name} action version")
    by_name = {c["name"]: c for c in components}
    for c in components:
        reference = c.get("image")
        if reference is not None:
            target = by_name.get(reference)
            if target is None or target["family"] != "oci-image" or target["optional"]:
                _fail(f"{c['name']}: image must name a required oci-image component")
        for required in c.get("requires", []):
            target = by_name.get(required)
            if target is None or required == c["name"] or target["family"] == "composite":
                _fail(f"{c['name']}: requires {required!r}, which is not a component")
    missing = [key for key in CORE_IMAGE_KEYS if not any(
        c["family"] == "oci-image" and c["key"] == key and not c["optional"] for c in components)]
    if missing:
        _fail("manifest must pin the required images: " + ", ".join(missing))
    return Manifest(raw, data)


def manifest_from_bytes(data: bytes) -> Manifest:
    data = _lf(data)
    return validate_manifest(_loads(data, "dependency manifest"), data)


def load_manifest(path: Path) -> Manifest:
    return manifest_from_bytes(_read_limited(path, MANIFEST_LIMIT, "dependency manifest"))


# ------------------------------------------------------------------------------------------------ side effects

def _https_fetch(url: str, destination: Path, limit: int) -> str:
    """Download a manifest-derived URL over https from a fixed host list; returns the SHA-256."""
    def require_host(target: str) -> None:
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme != "https" or parsed.hostname not in FETCH_HOSTS or parsed.username or parsed.port:
            raise MaintenanceError("a download left the allowed hosts")

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            require_host(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    require_host(url)
    request = urllib.request.Request(url, headers={"User-Agent": "workspace-maintenance"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.build_opener(Redirect()).open(request, timeout=60) as response, destination.open("wb") as out:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise MaintenanceError("a download exceeded its size limit")
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NotFound("the publisher has no such file") from error
        raise MaintenanceError(f"download failed (HTTP {error.code})") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise MaintenanceError(f"download failed ({type(error).__name__}); check the network") from error
    return digest.hexdigest()


def _subprocess_runner(argv: list, timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise PrerequisiteError("Docker is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise MaintenanceError("a Docker command timed out") from error
    return done.returncode, done.stdout.decode("utf-8", "replace")[-65536:]


class Docker:
    """The only host command the updater runs. Every call is a fixed argument list; images run by immutable ID."""

    def __init__(self, executable: str | None, runner: Runner):
        self.executable = executable
        self.runner = runner
        if os.name == "posix" and os.getuid() != 0:
            self.user = f"{os.getuid()}:{os.getgid()}"
        else:
            self.user = "65534:65534"

    def _call(self, args: list, timeout: int) -> str:
        if not self.executable:
            raise PrerequisiteError("Docker is not installed or not on PATH")
        code, output = self.runner([self.executable, *args], timeout)
        if code != 0:
            raise MaintenanceError(f"docker {args[0]} failed: {_clip(output, 240)}")
        return output

    def ready(self) -> None:
        try:
            output = self._call(["version", "--format", "{{.Server.Os}}"], 60)
        except PrerequisiteError:
            raise
        except MaintenanceError as error:
            raise PrerequisiteError("The Docker engine is not reachable; start Docker and retry") from error
        if output.strip() != "linux":
            raise PrerequisiteError("Docker must run Linux containers")

    def pull(self, reference: str) -> None:
        self._call(["pull", "--platform", PLATFORM, reference], 1800)

    def inspect(self, reference: str) -> tuple[str, list, str]:
        output = self._call(["image", "inspect", "--format",
                             "{{.Id}}\t{{json .RepoDigests}}\t{{.Os}}/{{.Architecture}}", reference], 60)
        fields = output.strip().split("\t")
        if len(fields) != 3 or not DIGEST.fullmatch(fields[0]):
            raise MaintenanceError("docker returned an unexpected image description")
        try:
            digests = json.loads(fields[1]) or []
        except ValueError as error:
            raise MaintenanceError("docker returned an unexpected image description") from error
        return fields[0], digests, fields[2]

    def present(self, image_id: str) -> bool:
        if not self.executable:
            raise PrerequisiteError("Docker is not installed or not on PATH")
        code, _ = self.runner([self.executable, "image", "inspect", "--format", "{{.Id}}", image_id], 60)
        return code == 0

    def build(self, tag: str, context: Path) -> None:
        self._call(["build", "--platform", PLATFORM, "--pull=false", "--tag", tag,
                    "--file", str(context / "Dockerfile"), str(context)], 3600)

    def run(self, image_id: str, argv: list, *, mounts: tuple = (), network: bool = False,
            env: dict | None = None, workdir: str | None = None, timeout: int = 600) -> str:
        if not DIGEST.fullmatch(image_id):
            raise MaintenanceError("containers run only by immutable image ID")
        args = ["run", "--rm", "--pull=never", "--network", "bridge" if network else "none", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--read-only", "--pids-limit=512", "--memory=2g",
                "--user", self.user, "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "--entrypoint", argv[0]]
        for source, target, read_only in mounts:
            if "," in str(source):
                raise MaintenanceError("a mount path contains a comma")
            args += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if read_only else "")]
        for name, value in sorted((env or {}).items()):
            args += ["--env", f"{name}={value}"]
        if workdir:
            args += ["--workdir", workdir]
        return self._call([*args, image_id, *argv[1:]], timeout)


@contextlib.contextmanager
def _process_lock(path: Path):
    """Exclusive, non-blocking, process-scoped lock; the OS drops it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise MaintenanceError("Another process is already updating dependencies") from error
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()  # closing also releases a POSIX flock


def _check_npm_lock(data: bytes) -> dict:
    lock = _loads(data, "npm lock")
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if lock.get("lockfileVersion") != 3 or not isinstance(packages, dict) or "" not in packages:
        raise MaintenanceError("npm lock is not a version 3 lockfile")
    for key, entry in packages.items():
        if not key:
            continue
        if not isinstance(entry, dict) or entry.get("link"):
            raise MaintenanceError("npm lock contains a link or a malformed entry")
        if entry.get("inBundle"):
            continue
        parsed = urllib.parse.urlsplit(str(entry.get("resolved", "")))
        integrity = str(entry.get("integrity", ""))
        if (parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org"
                or not re.fullmatch(r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", integrity)):
            raise MaintenanceError("npm lock contains an unverified or non-registry dependency")
    return lock


def _check_installed_npm(directory: Path, lock: dict) -> int:
    installed_path = directory / "node_modules" / ".package-lock.json"
    installed = _loads(_read_limited(installed_path, 256 << 20, "installed npm tree"), "installed npm tree")
    have = installed.get("packages", {})
    want = lock["packages"]
    for key, entry in have.items():
        if key not in want or entry.get("integrity") != want[key].get("integrity") or entry.get("version") != want[key].get("version"):
            raise MaintenanceError(f"npm installed {key}, which differs from the lock")
    for key, entry in want.items():
        if key and not entry.get("optional") and "os" not in entry and "cpu" not in entry and key not in have:
            raise MaintenanceError(f"npm did not install locked package {key}")
    return len(have)


def _check_cargo_lock(text: str) -> list:
    lock = tomllib.loads(text)
    if lock.get("version") not in (3, 4):
        raise MaintenanceError("Cargo.lock version is not supported")
    external = []
    for package in lock.get("package", []):
        source = package.get("source")
        if source is None:
            continue
        if source not in CRATES_SOURCES or not SHA256.fullmatch(str(package.get("checksum", ""))):
            raise MaintenanceError("Cargo.lock has a source that is not a checksummed crates.io package")
        external.append(package["checksum"])
    return external


# ------------------------------------------------------------------------------------------------ the service

class Maintenance:
    """Status, one-click update and image/asset lookup for one workspace data root.

    ``upstream`` is the only repository discovery may contact (``None`` disables discovery, which every status and
    update then says). ``runner`` and ``fetch`` exist so tests can replace the two side-effect seams.
    """

    def __init__(self, root: Path, checkout: Path, *, runner: Runner | None = None, fetch: Fetcher | None = None,
                 docker: str | None = None, upstream: tuple | None = FIRST_PARTY):
        self.root = Path(root)
        self.checkout = Path(checkout)
        self.state = self.root / STATE_DIR
        self.upstream = upstream
        self.docker = Docker(docker if docker is not None else shutil.which("docker"), runner or _subprocess_runner)
        self._fetch = fetch or _https_fetch
        self._guard = threading.Lock()
        self._worker: threading.Thread | None = None
        self._running = False
        self._progress = ""
        self._result: dict | None = None
        self._hashes: dict = {}
        self._loaded = self._loaded_receipt()

    # -------------------------------------------------------------------------------- selection

    def _loaded_receipt(self) -> dict | None:
        try:
            return self._read_active()[1]
        except MaintenanceError:
            return None

    def _set_dir(self, set_id: str) -> Path:
        return self.state / "sets" / set_id

    def _read_active(self) -> tuple:
        path = self.state / "active.json"
        if not os.path.lexists(path):
            return None, None
        selection = _loads(_read_limited(path, 1 << 16, "active selection"), "active selection")
        keys = {"schema", "set", "previous", "revision", "manifest_sha256", "receipt_sha256", "switched_at"}
        if (not isinstance(selection, dict) or set(selection) != keys or selection["schema"] != SCHEMA
                or not SET_ID.fullmatch(str(selection["set"]))
                or not (selection["previous"] is None or SET_ID.fullmatch(str(selection["previous"])))
                or not SHA256.fullmatch(str(selection["receipt_sha256"]))
                or not SHA256.fullmatch(str(selection["manifest_sha256"]))):
            raise Damaged("The active selection is malformed")
        base = self._set_dir(selection["set"])
        data = _read_limited(base / "receipt.json", RECEIPT_LIMIT, "active receipt")
        if _sha(data) != selection["receipt_sha256"]:
            raise Damaged("The active set no longer matches its receipt")
        if _sha(_read_limited(base / "manifest.json", MANIFEST_LIMIT, "active manifest")) != selection["manifest_sha256"]:
            raise Damaged("The active set no longer matches its reviewed manifest")
        receipt = _loads(data, "active receipt")
        if not isinstance(receipt, dict) or receipt.get("set") != selection["set"] or not isinstance(receipt.get("components"), dict):
            raise Damaged("The active receipt is malformed")
        if "resolution_sha256" in receipt:
            if _sha(_read_limited(base / "resolution.json", MANIFEST_LIMIT, "dependency resolution")) != receipt["resolution_sha256"]:
                raise Damaged("The active set no longer matches its dependency resolution")
        return selection, receipt

    def _active_manifest(self, selection: dict) -> Manifest:
        try:
            return load_manifest(self._set_dir(selection["set"]) / "manifest.json")
        except ManifestError as error:
            raise Damaged(f"The active manifest is not valid: {error}") from error

    def verify(self, *, deep: bool = True, images: bool = False) -> list:
        """Problems in the active set: missing parts, changed trees (deep), absent images (images)."""
        try:
            selection, receipt = self._read_active()
        except MaintenanceError as error:
            return [str(error)]
        if selection is None:
            return ["Nothing is installed"]
        return self._verify_components(self._set_dir(selection["set"]), receipt["components"], deep=deep, images=images)

    def _verify_components(self, base: Path, records: dict, *, deep: bool, images: bool) -> list:
        problems = []
        for name, record in sorted(records.items()):
            if record.get("lock_file"):
                path = base / record["lock_file"]
                if not path.is_file() or _file_sha(path, text=True) != record.get("lock_sha256"):
                    problems.append(f"{name}: installed dependency lock changed after install")
            relative = record.get("asset") or record.get("tree_root")
            if relative:
                target = base / relative
                if not target.is_dir():
                    problems.append(f"{name}: installed files are missing")
                    continue
                if deep and record.get("tree_sha256"):
                    try:
                        digest, _ = tree_digest(target)
                    except (MaintenanceError, OSError) as error:
                        problems.append(f"{name}: {_clip(error, 120)}")
                        continue
                    if digest != record["tree_sha256"]:
                        problems.append(f"{name}: installed files changed after install")
            if images and record.get("image_id"):
                try:
                    if not self.docker.present(record["image_id"]):
                        problems.append(f"{name}: image is not present in Docker")
                except PrerequisiteError:
                    problems.append("Docker is unavailable, so images were not checked")
                    break
        return problems

    def _build_application(self, base: Path, components: dict) -> Path:
        from .workspace_build import build_workspace

        self._note("Checking the workspace shell and both interfaces against the selected dependencies")
        return build_workspace(self.docker, self.checkout, base, components, self.state / "builds")

    def installed_application(self) -> Path:
        """Open the application for one exact selection; never refresh publisher channels."""
        selection, receipt = self._read_active()
        if not selection:
            raise MaintenanceError("Nothing is installed")
        base = self._set_dir(selection["set"])
        problems = self._verify_components(base, receipt["components"], deep=True, images=True)
        if problems:
            raise Damaged("Installed dependency set is incomplete: " + problems[0])
        return self._build_application(base, receipt["components"])

    def image_for(self, key: str) -> str:
        """Immutable image for ``key``: the active set's local image ID, else the committed ``repo@sha256`` pin."""
        if not isinstance(key, str) or not NAME.fullmatch(key):
            raise Unavailable("unknown image key")
        try:
            _, receipt = self._read_active()
        except Damaged:
            receipt = None  # a damaged selection falls back to the reviewed committed pin, never to a guess
        if receipt:
            for record in receipt["components"].values():
                if record.get("key") == key and DIGEST.fullmatch(str(record.get("image_id", ""))):
                    return record["image_id"]
        try:
            manifest = load_manifest(self.checkout / MANIFEST_NAME)
        except ManifestError as error:
            raise Unavailable(f"no reviewed pin is available: {error}") from error
        component = manifest.images.get(key)
        if component is None:
            raise Unavailable(f"image {key!r} is not declared")
        if component.get("track"):
            raise Unavailable(f"image {key!r} has no installed resolution; run Update all dependencies")
        if component["family"] != "oci-image":
            raise Unavailable(f"image {key!r} is built locally and is not installed yet; run the update")
        return f"{component['repository']}@{component['digest']}"

    def asset_path(self, name: str) -> Path | None:
        """Directory of an installed component in the active set, or ``None`` (nothing installed or no files)."""
        try:
            selection, receipt = self._read_active()
        except Damaged:
            return None
        record = (receipt or {}).get("components", {}).get(name)
        if not record or not record.get("asset"):
            return None
        path = self._set_dir(selection["set"]) / record["asset"]
        return path if path.is_dir() else None

    # -------------------------------------------------------------------------------- pins in the checkout and root

    def _hash(self, path: Path, *, text: bool) -> str:
        info = os.lstat(path)
        key = (str(path), info.st_size, info.st_mtime_ns, text)
        if key not in self._hashes:
            self._hashes[key] = _file_sha(path, text=text)
        return self._hashes[key]

    def _pinned_file(self, base: Path, relative: str, expected: str, *, text: bool = True) -> tuple:
        """(None, "") when the file matches its pin; ("absent"|"drift", reason) otherwise."""
        path = base.joinpath(*relative.split("/"))
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return "absent", f"not present: {relative}"
        except OSError:
            return "drift", f"{relative} cannot be read"
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            return "drift", f"{relative} is not a regular file"
        if self._hash(path, text=text) != expected:
            return "drift", f"{relative} differs from the reviewed pin"
        return None, ""

    def _source(self, c: dict) -> tuple:
        """Source-side check of one component: (None|'absent'|'drift'|'verified', detail)."""
        family = c["family"]
        if family == "oci-build":
            checks = [(c["recipe"], c["recipe_sha256"])] + sorted(c.get("source_files", {}).items())
            for relative, expected in checks:
                kind, detail = self._pinned_file(self.checkout, relative, expected)
                if kind:
                    return kind, detail
            recipe = _lf(self.checkout.joinpath(*c["recipe"].split("/")).read_bytes()).decode("utf-8", "replace")
            match = re.search(r"^FROM\s+(\S+)\s*$", recipe, re.MULTILINE)
            base = c.get("base")
            allowed = ({f"{base['repository']}:{base['tag']}"} if c.get("track") else
                       {f"{short}@{base['digest']}" for short in _short_repository(base["repository"])} if base else {"scratch"})
            if not match or match[1] not in allowed:
                return "drift", "recipe base image differs from the reviewed pin"
            return None, ""
        if family == "oci-image":
            pin = c.get("checkout_pin")
            if pin:
                path = self.checkout.joinpath(*pin["path"].split("/"))
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    return None, ""  # no inspector in this checkout, so there is no file to disagree with
                except (OSError, UnicodeDecodeError):
                    return "drift", f"{pin['path']} cannot be read"
                if text != pin["text"]:
                    return "drift", f"{pin['path']} differs from the reviewed image pin"
            return None, ""
        if family == "cargo-lock":
            if c.get("track"):
                return self._pinned_file(self.checkout, c["manifest"], c["manifest_sha256"])
            return self._pinned_file(self.checkout, c["lock"], c["lock_sha256"])
        if family == "npm-lock":
            inputs = [(c["manifest"], c["manifest_sha256"])]
            if not c.get("track"):
                inputs.append((c["lock"], c["lock_sha256"]))
            for relative, expected in inputs:
                kind, detail = self._pinned_file(self.checkout, relative, expected)
                if kind:
                    return kind, detail
            return None, ""
        if family == "operator-asset":
            kind, detail = self._pinned_file(self.root, c["path"], c["sha256"], text=False)
            if kind == "absent":
                return "absent", "operator-supplied and not present; an update never downloads it"
            return ("drift", detail) if kind else ("verified", "operator-supplied file matches its pin")
        if family == "ci-actions":
            return self._ci_check(c)
        return None, ""

    def _ci_check(self, c: dict) -> tuple:
        directory = self.checkout.joinpath(*c["workflows"].split("/"))
        if not directory.is_dir():
            return "absent", f"not present: {c['workflows']}"
        used: dict = {}
        for path in sorted(directory.glob("*.y*ml")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"\s*-?\s*uses:\s*([^\s@]+)@(\S+)", line)
                if match and not match[1].startswith((".", "docker:")):
                    used.setdefault(match[1], set()).add(match[2])
        for repo, refs in sorted(used.items()):
            pin = c["actions"].get(repo)
            if pin is None:
                return "drift", f"{repo} is used by a workflow but not pinned in the manifest"
            if refs != {pin.get("ref", pin.get("sha"))}:
                return "drift", f"{repo} does not match its declared action source"
        unused = sorted(set(c["actions"]) - set(used))
        if unused:
            return "drift", f"{unused[0]} is pinned but no workflow uses it"
        return "verified", "workflow references match the declared sources; CI tooling is never installed here"

    def _compatible(self, manifest: Manifest) -> str | None:
        """Why a reviewed manifest cannot be installed against this checkout, or ``None``.

        Locks, recipes and checkout pins ride with the code. A revision that changes them needs a code update first.
        """
        for c in manifest.components:
            if c["family"] not in SOURCE_BOUND:
                continue
            kind, detail = self._source(c)
            if kind == "drift":
                return f"{c['name']}: {detail}"
            if kind == "absent" and not c["optional"]:
                return f"{c['name']}: required but {detail}"
        return None

    # -------------------------------------------------------------------------------- status

    def _component_states(self, manifest: Manifest, receipt: dict | None) -> list:
        records = (receipt or {}).get("components", {})
        skipped = {item["name"] for item in (receipt or {}).get("skipped", [])}
        states: dict = {}
        for c in manifest.components:
            if c["family"] == "composite":
                continue
            kind, detail = self._source(c)
            record = records.get(c["name"])
            if c["family"] in ("operator-asset", "ci-actions"):
                state = kind or "verified"
            elif kind == "drift":
                state = "drift"
            elif record is not None and record.get("policy_identity", record.get("identity")) == manifest.identity(c["name"]):
                state, detail = ("tracked", "release metadata only") if c["family"] == "release-metadata" else ("installed", "")
            elif record is not None:
                state, detail = "outdated", "a newer reviewed version is ready to install"
            elif kind == "absent":
                state = "absent"
                detail = detail + ("; skipped at the last update" if c["name"] in skipped else "")
            else:
                state, detail = "missing", "not installed yet"
            states[c["name"]] = (state, detail)
        rank = {"drift": 0, "outdated": 1, "missing": 2, "absent": 3}
        for c in manifest.components:
            if c["family"] != "composite":
                continue
            worst = min((rank.get(states[r][0], 9), r, states[r]) for r in c["requires"])
            if worst[0] == 9:
                states[c["name"]] = ("installed", "")
            else:
                state, detail = worst[2]
                states[c["name"]] = (state, f"{worst[1]}: {detail}" if detail else worst[1])
        result = []
        for c in manifest.components:
            state, detail = states[c["name"]]
            version = records.get(c["name"], {}).get("version", c["version"]) if c.get("track") else c["version"]
            result.append({"name": c["name"], "version": version, "state": state, "family": c["family"],
                           "scope": c["scope"], "optional": c["optional"], "activation": c["activation"],
                           "detail": detail, **({"note": c["note"]} if "note" in c else {})})
        return result

    def _restart_names(self, receipt: dict | None) -> list:
        if not receipt or self._loaded is None:  # a process that loaded nothing has nothing to restart
            return []
        loaded = self._loaded.get("components", {})
        return sorted(name for name, record in receipt["components"].items()
                      if record.get("activation") == "restart"
                      and loaded.get(name, {}).get("identity") != record.get("identity"))

    def _read_record(self, name: str, label: str) -> dict | None:
        path = self.state / name
        if not os.path.lexists(path):
            return None
        try:
            data = _loads(_read_limited(path, MANIFEST_LIMIT + 4096, label), label)
        except MaintenanceError:
            return None  # a damaged note about a past attempt is advisory; status recomputes from the real selection
        return data if isinstance(data, dict) else None

    def _candidate(self, record: dict | None) -> Manifest | None:
        """A discovered newer manifest, re-validated and re-checked against this checkout."""
        if not record or record.get("state") != "newer" or not isinstance(record.get("manifest"), str):
            return None
        try:
            manifest = manifest_from_bytes(record["manifest"].encode("utf-8"))
        except MaintenanceError:
            return None  # a stale or edited cache entry is simply not a candidate
        if manifest.sha256 != record.get("manifest_sha256") or self._compatible(manifest):
            return None
        return manifest

    def status(self) -> dict:
        with self._guard:
            running, progress, result = self._running, self._progress, self._result
        docker_path = self.docker.executable
        prerequisites = [{"name": "docker", "state": "found" if docker_path else "missing",
                          "detail": "Docker client on PATH; the engine must run Linux containers"
                          if docker_path else "Install Docker Engine or Docker Desktop, then retry"},
                         {"name": "network", "state": "checked-on-update",
                          "detail": "registry-1.docker.io, registry.npmjs.org, crates.io, codeload.github.com, "
                                    "api.github.com and raw.githubusercontent.com"}]
        try:
            local = load_manifest(self.checkout / MANIFEST_NAME)
        except MaintenanceError as error:
            return {"state": "running" if running else "failed",
                    "message": _clip(f"Reviewed manifest rejected: {error}"), "components": [],
                    "prerequisites": prerequisites, "restart_required": False, "restart": [],
                    "update_available": False, "discovery": {"state": "not-checked", "message": ""}}
        damaged = ""
        selection = receipt = active = None
        try:
            selection, receipt = self._read_active()
            if selection:
                active = self._active_manifest(selection)
        except Damaged as error:
            selection = receipt = active = None
            damaged = str(error)
        record = self._read_record("discovery.json", "discovery record")
        candidates = [(local.revision, 1, local)]
        if active:
            candidates.append((active.revision, 2, active))
        newer = self._candidate(record)
        if newer:
            candidates.append((newer.revision, 0, newer))
        desired = max(candidates, key=lambda item: (item[0], item[1]))[2]
        components = self._component_states(desired, receipt)
        restart = self._restart_names(receipt)
        pending = [c for c in components if c["state"] in ("outdated", "missing")]
        drift = [c for c in components if c["state"] == "drift"]
        last = self._read_record("last.json", "last update")
        discovery = ({"state": record.get("state", "not-checked"), "message": record.get("message", ""),
                      "checked_at": record.get("at"), "revision": record.get("revision"), "commit": record.get("commit")}
                     if record else {"state": "not-checked", "message": "Newer reviewed revisions have not been checked yet."})
        if running:
            state, message = "running", progress or "Updating dependencies"
        elif damaged:
            state, message = "failed", damaged + ". Run the update to reinstall."
        elif drift:
            state, message = "failed", f"Checkout does not match the reviewed pins: {drift[0]['name']} ({drift[0]['detail']})"
        elif last and last.get("state") == "failed" and last.get("manifest_sha256") == desired.sha256:
            state, message = "failed", last.get("message", "The last update failed")
            if last.get("kind") == "prerequisite" and docker_path:
                prerequisites[0]["state"] = "unreachable"
        elif result and result["state"] == "updated" and not pending:
            state, message = "updated", result["message"]
        elif pending or selection is None or selection["manifest_sha256"] != desired.sha256:
            state = "available"
            message = (f"Reviewed revision {desired.revision} is ready to install." if selection
                       else "Nothing is installed yet. Run the update.")
        else:
            state, message = "current", f"Installed set matches reviewed revision {desired.revision}."
            if discovery["state"] not in QUIET_DISCOVERY:
                message += " " + discovery["message"]
        if restart and state in ("current", "updated"):
            message += " Restart required for: " + ", ".join(restart) + "."
        return {"state": state, "message": message, "revision": desired.revision,
                "installed_revision": selection["revision"] if selection else None,
                "set": selection["set"] if selection else None,
                "origin": (receipt or {}).get("origin"), "components": components,
                "prerequisites": prerequisites, "restart_required": bool(restart), "restart": restart,
                "update_available": state == "available", "discovery": discovery,
                "distribution": desired.raw.get("distribution", {}),
                "optional_absent": [c["name"] for c in components if c["state"] == "absent" and c["optional"]]}

    # -------------------------------------------------------------------------------- update

    def update(self, wait: bool = False) -> dict:
        """Look for a newer reviewed revision, then install and activate the best reviewed set.

        Returns status at once (``running``); ``wait=True`` returns the terminal status. A lookup or network error
        is reported as a failure, never as "current", unless nothing is installed yet (bootstrap) or the checkout
        itself carries a newer reviewed manifest than the installed set.
        """
        return self._start(False, wait)

    def bootstrap(self, wait: bool = False) -> dict:
        """Initial install from this checkout's reviewed manifest only; refused once a set is installed."""
        return self._start(True, wait)

    def _start(self, bootstrap: bool, wait: bool) -> dict:
        with self._guard:
            if not self._running:
                self._running, self._progress, self._result = True, "Starting update", None
                self._worker = threading.Thread(target=self._work, args=(bootstrap,), name="dependency-update", daemon=True)
                self._worker.start()
            worker = self._worker
        if wait and worker is not None:
            worker.join()
        return self.status()

    def _note(self, message: str) -> None:
        with self._guard:
            self._progress = message

    def _work(self, bootstrap: bool) -> None:
        try:
            with _process_lock(self.state / "update.lock"):
                outcome = self._perform(bootstrap)
        except MaintenanceError as error:
            kind = "prerequisite" if isinstance(error, PrerequisiteError) else getattr(error, "kind", "update")
            outcome = {"state": "failed", "message": _clip(error), "kind": kind}
        except Exception as error:  # an unexpected defect must still end as a visible failure
            outcome = {"state": "failed", "message": _clip(f"Update stopped unexpectedly ({type(error).__name__})"),
                       "kind": "update"}
        self._save_last(outcome)
        with self._guard:
            self._result = outcome
            self._running = False

    def _save_last(self, outcome: dict) -> None:
        try:
            manifest_sha = outcome.get("manifest_sha256") or load_manifest(self.checkout / MANIFEST_NAME).sha256
        except MaintenanceError:
            manifest_sha = None
        record = {"state": "failed" if outcome["state"] == "failed" else "ok", "message": outcome["message"],
                  "kind": outcome.get("kind"), "manifest_sha256": manifest_sha, "at": int(time.time())}
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.state / "last.json", _canonical(record))
        except OSError:  # the outcome is still returned to the caller; only the cross-restart note is lost
            outcome["message"] += " (The result could not be saved for the next start.)"

    def _save_discovery(self, record: dict) -> None:
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.state / "discovery.json", _canonical(record))
        except OSError:  # a missing cache only means the next status says "not checked"
            record["message"] += " (The check could not be saved.)"

    def _discover(self, base_revision: int) -> dict:
        """Ask the fixed first-party repository for a newer reviewed manifest at an immutable commit."""
        now = int(time.time())
        if self.upstream is None:
            return {"state": "disabled", "at": now, "message": "Checking for newer reviewed revisions is disabled; "
                    "only this checkout's manifest was used."}
        repository, branch = self.upstream
        staging = self.state / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="discovery-", dir=staging) as scratch:
            scratch = Path(scratch)
            try:
                self._fetch(f"https://api.github.com/repos/{repository}/git/ref/heads/{branch}", scratch / "ref.json", 1 << 20)
                target = _loads(_read_limited(scratch / "ref.json", 1 << 20, "repository metadata"), "repository metadata")
                commit = target["object"]["sha"] if target["object"]["type"] == "commit" else ""
                if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
                    raise ValueError("the branch does not identify a commit")
                self._fetch(f"https://raw.githubusercontent.com/{repository}/{commit}/{MANIFEST_NAME}",
                            scratch / "manifest.json", MANIFEST_LIMIT)
                data = _lf(_read_limited(scratch / "manifest.json", MANIFEST_LIMIT, "published manifest"))
            except NotFound:
                return {"state": "unpublished", "at": now, "message": "The first-party repository publishes no portable "
                        "manifest yet; this checkout's reviewed manifest is the source."}
            except MaintenanceError as error:
                return {"state": "unavailable", "at": now,
                        "message": _clip(f"Could not check for newer reviewed revisions: {error}")}
            except (KeyError, TypeError, ValueError):
                return {"state": "rejected", "at": now,
                        "message": "The repository metadata was not in the expected form; nothing was installed from it."}
        try:
            remote = manifest_from_bytes(data)
        except MaintenanceError as error:
            return {"state": "rejected", "at": now, "commit": commit,
                    "message": _clip(f"The newest published manifest was refused ({error}). If it needs a newer "
                                     "workspace build, update the application code explicitly.")}
        base = {"at": now, "commit": commit, "revision": remote.revision, "manifest_sha256": remote.sha256}
        if remote.revision < base_revision or (remote.revision == base_revision and remote.sha256 in self._known(remote)):
            return {**base, "state": "current", "message": f"No newer reviewed revision (published: {remote.revision})."}
        if remote.revision == base_revision:
            return {**base, "state": "diverged", "message": f"Published revision {remote.revision} differs from the local "
                    "one with the same number; it is ignored until the revision is raised."}
        # A remote revision cannot turn an approved metadata source into downloaded code or change an install channel.
        local = load_manifest(self.checkout / MANIFEST_NAME)
        if any(c.get("track") for c in local.components + remote.components) and remote.sha256 != local.sha256:
            return {**base, "state": "needs-code-update",
                    "message": "The Kit has a new dependency policy. Update the checkout to adopt it; local channels still refresh."}
        reason = self._compatible(remote)
        if reason:
            return {**base, "state": "needs-code-update",
                    "message": _clip(f"Reviewed revision {remote.revision} needs a workspace code update first ({reason}).")}
        return {**base, "state": "newer", "manifest": data.decode("utf-8"),
                "message": f"Reviewed revision {remote.revision} found at commit {commit[:12]}."}

    def _known(self, remote: Manifest) -> set:
        """Digests of the manifests this machine already holds (checkout and active set)."""
        known = set()
        try:
            known.add(load_manifest(self.checkout / MANIFEST_NAME).sha256)
            selection, _ = self._read_active()
            if selection:
                known.add(selection["manifest_sha256"])
        except MaintenanceError:
            return known  # whatever could be read is still a valid "already held" set
        return known

    def _plan(self, manifest: Manifest) -> tuple:
        plan, skipped, skipped_names = [], [], set()
        for c in manifest.ordered():
            kind, detail = self._source(c)
            if kind == "drift":
                raise MaintenanceError(f"{c['name']}: {detail}; the checkout does not match the reviewed pins")
            reason = detail if kind == "absent" else None
            if reason is None and c.get("image") in skipped_names:
                reason = f"needs {c['image']}"
            if reason is None:
                plan.append(c)
            elif c["optional"]:
                skipped.append({"name": c["name"], "reason": reason})
                skipped_names.add(c["name"])
            else:
                raise MaintenanceError(f"{c['name']} is required but {reason}")
        for c in manifest.components:
            if c["family"] in ("ci-actions", "operator-asset"):
                kind, detail = self._source(c)
                if kind == "drift":
                    raise MaintenanceError(f"{c['name']}: {detail}")
        return plan, skipped

    def _current(self, manifest: Manifest, resolved: Manifest, plan: list, selection: dict | None, receipt: dict | None) -> bool:
        if not selection or selection["manifest_sha256"] != manifest.sha256:
            return False
        records = receipt["components"]
        if any(records.get(c["name"], {}).get("identity") != resolved.identity(c["name"]) for c in plan):
            return False
        return not self.verify(deep=True)

    def _choose(self, local: Manifest, selection: dict | None, receipt: dict | None, discovery: dict) -> tuple:
        """The reviewed manifest to install and where it came from."""
        if discovery["state"] == "newer":
            remote = manifest_from_bytes(discovery["manifest"].encode("utf-8"))
            return remote, {"kind": "remote", "repository": self.upstream[0], "commit": discovery["commit"]}
        if selection:
            active = self._active_manifest(selection)
            if active.revision >= local.revision:
                return active, (receipt or {}).get("origin") or {"kind": "checkout"}
        return local, {"kind": "checkout"}

    def _perform(self, bootstrap: bool) -> dict:
        self.state.mkdir(parents=True, exist_ok=True)
        self._note("Reading the reviewed manifest")
        local = load_manifest(self.checkout / MANIFEST_NAME)
        try:
            selection, receipt = self._read_active()
            if selection:
                self._active_manifest(selection)
        except Damaged:
            selection = receipt = None  # a damaged selection is reinstalled from a reviewed manifest
        if bootstrap:
            if selection:
                raise MaintenanceError("Dependencies are already installed; use the update, not the bootstrap")
            discovery = {"state": "bootstrap", "at": int(time.time()),
                         "message": "Initial install from this checkout's reviewed manifest."}
        else:
            self._note("Checking the first-party repository for a newer reviewed revision")
            discovery = self._discover(max(local.revision, selection["revision"] if selection else 0))
        self._save_discovery(discovery)
        target, origin = self._choose(local, selection, receipt, discovery)
        if (discovery["state"] == "unavailable" and selection and target.sha256 == selection["manifest_sha256"]
                and not any(c.get("track") for c in target.components)):
            error = MaintenanceError(discovery["message"] + " Nothing was changed.")
            error.kind = "discovery"
            raise error
        plan, skipped = self._plan(target)
        note = "" if discovery["state"] in QUIET_DISCOVERY else " " + discovery["message"]
        from .upstream import resolve
        with tempfile.TemporaryDirectory(prefix="resolve-", dir=self.state) as scratch:
            resolved, roots, ready = resolve(self, target, plan, Path(scratch), receipt)
            portable = any(m.raw.get("distribution", {}).get("portable") is True for m in (local, target))
            return self._activate(target, resolved, plan, skipped, selection, receipt, note, origin, roots, ready, portable)

    def _activate(self, target, resolved, plan, skipped, selection, receipt, note, origin, roots, ready, portable):
        if self._current(target, resolved, plan, selection, receipt):
            try:
                self.docker.ready()
                missing = [n for n, r in receipt["components"].items()
                           if r.get("image_id") and not self.docker.present(r["image_id"])]
            except PrerequisiteError:
                if portable:
                    raise
                return {"state": "current", "manifest_sha256": target.sha256,
                        "message": "Dependencies are current. Docker is not reachable, so images were not rechecked." + note}
            if not missing:
                if portable:
                    self._build_application(self._set_dir(selection["set"]), receipt["components"])
                return {"state": "current", "manifest_sha256": target.sha256,
                        "message": f"Dependencies are current at reviewed revision {target.revision}." + note}
        self.docker.ready()
        shutil.rmtree(self.state / "staging", ignore_errors=True)  # leftovers of a crashed run; the process lock is ours
        nonce = uuid.uuid4().hex[:12]
        stage = self.state / "staging" / nonce
        (stage / "components").mkdir(parents=True)
        try:
            images: dict = {}
            records: dict = {}
            for index, c in enumerate(plan, 1):
                self._note(f"Installing {c['name']} ({index} of {len(plan)})")
                try:
                    concrete = resolved.by_name[c["name"]]
                    if c["name"] in ready:
                        facts = ready[c["name"]]
                        images[c["name"]] = facts["image_id"]
                    else:
                        facts = self._install(concrete, stage / "components" / c["name"], images, roots.get(c["name"], self.checkout))
                except MaintenanceError as error:
                    raise MaintenanceError(f"{c['name']}: {error}") from error
                records[c["name"]] = {"identity": resolved.identity(c["name"]), "policy_identity": target.identity(c["name"]),
                                      "family": c["family"], "version": concrete["version"], "activation": c["activation"], **facts}
            if portable:
                self._build_application(stage, records)
            self._note("Checking the installed set")
            new_id = f"r{target.revision}-{resolved.sha256[:12]}"
            if os.path.lexists(self._set_dir(new_id)):
                new_id += "-" + nonce[:6]
            data = _canonical({"schema": SCHEMA, "set": new_id, "revision": target.revision,
                               "manifest_sha256": target.sha256, "created": int(time.time()), "origin": origin,
                               "components": records, "skipped": skipped, "resolution_sha256": resolved.sha256})
            _fsync_write(stage / "resolution.json", resolved.data)
            _fsync_write(stage / "receipt.json", data)
            _fsync_write(stage / "manifest.json", target.data)
            (self.state / "sets").mkdir(exist_ok=True)
            os.rename(stage, self._set_dir(new_id))
            problems = self.verify_set(new_id)
            if problems:
                _rmtree(self._set_dir(new_id))
                raise MaintenanceError("the installed set failed its own check: " + problems[0])
        except BaseException:
            _rmtree(stage)
            raise
        previous = selection["set"] if selection else None
        pointer = {"schema": SCHEMA, "set": new_id, "previous": previous, "revision": target.revision,
                   "manifest_sha256": target.sha256, "receipt_sha256": _sha(data), "switched_at": int(time.time())}
        try:
            _atomic_write(self.state / "active.json", _canonical(pointer))
        except OSError as error:
            _rmtree(self._set_dir(new_id))
            raise MaintenanceError("the new selection could not be written; the previous one is unchanged") from error
        self._collect({new_id, previous})
        restart = self._restart_names(_loads(data, "receipt"))
        message = f"Updated to reviewed revision {target.revision}"
        message += (f" from the first-party repository (commit {origin['commit'][:12]})." if origin["kind"] == "remote"
                    else " from this checkout.")
        if restart:
            message += " Restart required for: " + ", ".join(restart) + "."
        if skipped:
            message += " Not installed (absent in this checkout): " + ", ".join(s["name"] for s in skipped) + "."
        return {"state": "updated", "message": message + note, "manifest_sha256": target.sha256}

    def verify_set(self, set_id: str) -> list:
        base = self._set_dir(set_id)
        try:
            receipt = _loads(_read_limited(base / "receipt.json", RECEIPT_LIMIT, "receipt"), "receipt")
        except MaintenanceError as error:
            return [str(error)]
        problems = []
        for name, record in receipt["components"].items():
            relative = record.get("asset") or record.get("tree_root")
            if relative and not (base / relative).is_dir():
                problems.append(f"{name}: installed files are missing")
        return problems

    def _collect(self, keep: set) -> None:
        directory = self.state / "sets"
        if directory.is_dir():
            for child in directory.iterdir():
                if child.name not in keep:
                    _rmtree(child)

    # -------------------------------------------------------------------------------- installers

    def _install(self, c: dict, destination: Path, images: dict, checkout: Path) -> dict:
        family = c["family"]
        if family == "oci-image":
            return self._install_image(c, images)
        if family == "oci-build":
            return self._install_build(c, destination, images, checkout)
        if family == "cargo-lock":
            return self._install_cargo(c, destination, images[c["image"]], checkout)
        if family == "npm-lock":
            return self._install_npm(c, destination, images[c["image"]], checkout)
        if family == "release-metadata":
            return {"role": "metadata-only", "repository": c["repository"], "tag": c["tag"], "commit": c["commit"]}
        if family == "reference-archive" and checkout != self.checkout:
            for name, expected in c["files"].items():
                content = _read_limited(checkout / name, REFERENCE_FILE_LIMIT, name)
                if _sha(content) != expected:
                    raise MaintenanceError(f"{name} changed after resolution")
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                _fsync_write(path, content)
            digest, count = tree_digest(destination)
            return {"role": "reference", "repository": c["repository"], "tag": c["tag"], "commit": c["commit"],
                    "asset": f"components/{c['name']}", "tree_sha256": digest, "files": count}
        return self._install_reference(c, destination)

    def _check_image(self, reference: str, repository: str, digest: str) -> tuple:
        image_id, digests, platform = self.docker.inspect(reference)
        if not platform.startswith("linux/"):
            raise MaintenanceError("image is not a Linux image")
        if not any(f"{short}@{digest}" in digests for short in _short_repository(repository)):
            raise MaintenanceError("Docker does not report the reviewed digest for this image")
        return image_id, platform

    def _run_checks(self, image_id: str, checks: list) -> list:
        results = []
        for check in checks:
            output = self.docker.run(image_id, check["argv"], timeout=120).strip()
            if not re.search(check["expect"], output, re.MULTILINE):
                raise MaintenanceError(f"{' '.join(check['argv'])} reported {_clip(output, 80)!r}, not the reviewed version")
            results.append({"argv": check["argv"], "output": _clip(output.splitlines()[0] if output else "", 100)})
        return results

    def _install_image(self, c: dict, images: dict) -> dict:
        reference = f"{c['repository']}@{c['digest']}"
        self.docker.pull(reference)
        image_id, platform = self._check_image(reference, c["repository"], c["digest"])
        checks = self._run_checks(image_id, c["verify"])
        images[c["name"]] = image_id
        return {"key": c["key"], "image_id": image_id, "repository": c["repository"], "digest": c["digest"],
                "platform": platform, "checks": checks}

    def _install_build(self, c: dict, destination: Path, images: dict, checkout: Path) -> dict:
        base = c.get("base")
        if base:
            reference = f"{base['repository']}@{base['digest']}"
            self.docker.pull(reference)
            self._check_image(reference, base["repository"], base["digest"])
        kind, detail = self._pinned_file(checkout, c["recipe"], c["recipe_sha256"])
        if kind:
            raise MaintenanceError(detail)
        context = destination.parent.parent / "build-context" / c["name"]
        context.mkdir(parents=True)
        _fsync_write(context / "Dockerfile", _lf(checkout.joinpath(*c["recipe"].split("/")).read_bytes()))
        tag = f"workspace-{c['key']}:{c['recipe_sha256'][:12]}"
        self.docker.build(tag, context)
        _rmtree(context.parent)
        image_id, _, platform = self.docker.inspect(tag)
        if not platform.startswith("linux/"):
            raise MaintenanceError("built image is not a Linux image")
        checks = self._run_checks(image_id, c["verify"]) if c.get("verify") else []
        images[c["name"]] = image_id
        return {"key": c["key"], "image_id": image_id, "base": f"{base['repository']}@{base['digest']}" if base else "scratch",
                "recipe_sha256": c["recipe_sha256"], "platform": platform,
                "reproducible": bool(c.get("reproducible", False)), "checks": checks}

    def _writable(self, path: Path) -> None:
        if os.name == "posix" and os.getuid() == 0:
            path.chmod(0o777)

    def _install_cargo(self, c: dict, destination: Path, image_id: str, checkout: Path) -> dict:
        kind, detail = self._pinned_file(checkout, c["lock"], c["lock_sha256"])
        if kind:
            raise MaintenanceError(detail)
        lock = checkout.joinpath(*c["lock"].split("/"))
        checksums = _check_cargo_lock(_lf(lock.read_bytes()).decode("utf-8"))
        destination.mkdir(parents=True)
        _fsync_write(destination / "Cargo.lock", _lf(lock.read_bytes()))
        facts = {"crates": len(checksums), "lock_sha256": c["lock_sha256"], "lock_file": f"components/{c['name']}/Cargo.lock"}
        if not checksums:
            return {**facts, "asset": None, "tree_sha256": None}
        context = checkout.joinpath(*c["context"].split("/")).resolve()
        manifest = checkout.joinpath(*c["manifest"].split("/")).resolve()
        try:
            inside = manifest.relative_to(context).as_posix()
        except ValueError as error:
            raise MaintenanceError("the Cargo manifest is outside its context") from error
        self._writable(destination)
        self.docker.run(
            image_id, ["cargo", "vendor", "--locked", "--manifest-path", f"/src/{inside}", "/out/vendor"],
            mounts=((context, "/src", True), (destination, "/out", False)), network=True,
            env={"HOME": "/tmp", "CARGO_HOME": "/tmp/cargo", "CARGO_TERM_COLOR": "never"}, workdir="/src", timeout=1800)
        vendor = destination / "vendor"
        packed = []
        for crate in sorted(vendor.iterdir()) if vendor.is_dir() else []:
            marker = crate / ".cargo-checksum.json"
            if not crate.is_dir() or not marker.is_file():
                raise MaintenanceError("vendored output has an entry that is not a crate")
            packed.append(_loads(marker.read_bytes(), "crate checksum").get("package"))
        if sorted(map(str, packed)) != sorted(checksums):
            raise MaintenanceError("vendored crates do not match the checksums in Cargo.lock")
        digest, count = tree_digest(vendor)
        return {**facts, "asset": f"components/{c['name']}/vendor", "tree_sha256": digest, "files": count}

    def _install_npm(self, c: dict, destination: Path, image_id: str, checkout: Path) -> dict:
        for relative, expected in ((c["manifest"], c["manifest_sha256"]), (c["lock"], c["lock_sha256"])):
            kind, detail = self._pinned_file(checkout, relative, expected)
            if kind:
                raise MaintenanceError(detail)
        raw_lock = _lf(checkout.joinpath(*c["lock"].split("/")).read_bytes())
        lock = _check_npm_lock(raw_lock)
        destination.mkdir(parents=True)
        _fsync_write(destination / "package.json", _lf(checkout.joinpath(*c["manifest"].split("/")).read_bytes()))
        _fsync_write(destination / "package-lock.json", raw_lock)
        self._writable(destination)
        self.docker.run(
            image_id, ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/"],
            mounts=((destination, "/work", False),), network=True, workdir="/work", timeout=1800,
            env={"HOME": "/tmp", "npm_config_cache": "/tmp/npm-cache", "npm_config_update_notifier": "false", "CI": "1"})
        installed = _check_installed_npm(destination, lock)
        digest, count = tree_digest(destination)
        return {"asset": f"components/{c['name']}", "tree_sha256": digest, "files": count, "packages": installed,
                "lock_sha256": c["lock_sha256"]}

    def _install_reference(self, c: dict, destination: Path) -> dict:
        """Fetch a release archive by commit and keep only its pinned files, as data. Nothing is run or unpacked."""
        repository, commit = c["repository"], c["commit"]
        root = f"{repository.split('/')[1]}-{commit}"
        archive = destination.parent.parent / f"{c['name']}.tgz"
        archive_sha = self._fetch(f"https://codeload.github.com/{repository}/tar.gz/{commit}", archive, ARCHIVE_LIMIT)
        wanted = {f"{root}/{relative}": relative for relative in c["files"]}
        found: dict = {}
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    relative = wanted.get(member.name)
                    if relative is None:
                        continue
                    if relative in found or not member.isfile() or not 0 < member.size <= REFERENCE_FILE_LIMIT:
                        raise MaintenanceError(f"{relative} is not a single regular file in the archive")
                    with tar.extractfile(member) as stream:
                        found[relative] = stream.read()
        except (tarfile.TarError, OSError, EOFError) as error:
            raise MaintenanceError(f"the release archive could not be read ({type(error).__name__})") from error
        finally:
            try:
                _remove_file(archive)
            except OSError:  # the archive is scratch inside staging, which the caller removes on failure
                pass
        for relative, expected in sorted(c["files"].items()):
            if relative not in found or _sha(found[relative]) != expected:
                raise MaintenanceError(f"{relative} does not match the reviewed digest")
        package = found.get("package.json")
        if package is not None:
            version = _loads(package, "package.json").get("version")
            if not isinstance(version, str) or not c["tag"].endswith(version):
                raise MaintenanceError("the package version does not match the reviewed tag")
        destination.mkdir(parents=True)
        for relative, content in found.items():
            target = destination.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            _fsync_write(target, content)
        digest, count = tree_digest(destination)
        return {"role": "reference", "commit": commit, "tag": c["tag"], "archive_sha256": archive_sha,
                "asset": f"components/{c['name']}", "tree_sha256": digest, "files": count}
