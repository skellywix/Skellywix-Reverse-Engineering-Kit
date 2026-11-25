"""Stage published dependency candidates for the active Skellywix RE Kit inputs."""

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

NPM = "https://registry.npmjs.org"
GITHUB = "https://api.github.com"
REA_REPOSITORY = "morluto/rea"
REA_SOURCE_URL = f"https://codeload.github.com/{REA_REPOSITORY}/tar.gz/"
REA_TAG = re.compile(r"rea-agents-([0-9]+\.[0-9]+\.[0-9]+)")
REA_SKILL = ".agents/skills/reverse-engineer-anything/SKILL.md"
ROOT = Path(__file__).resolve().parents[1]
METADATA_LIMIT = 8 * 1024**2
REA_BASELINE_VERSION = (6, 10, 1)
ARCHIVE_LIMIT = 2 * 1024**3
HOSTS = frozenset({
    "api.github.com", "raw.githubusercontent.com", "github.com",
    "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "registry.npmjs.org", "static.crates.io", "crates.io", "nodejs.org",
    "codeload.github.com",
    "static.rust-lang.org",
    "pypi.org", "files.pythonhosted.org", "snapshot.ubuntu.com",
})


def https_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.username is not None or parsed.port is not None or parsed.fragment):
        raise ValueError("unexpected download origin")
    return parsed


class PublisherRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, path, limit):
    https_url(url)
    request = Request(url, headers={"User-Agent": "re-kit-dependency-candidates"})
    digest = hashlib.sha256()
    size = 0
    opener = build_opener(PublisherRedirect())
    with opener.open(request, timeout=30) as response, path.open("wb") as out:
        while chunk := response.read(1024**2):
            size += len(chunk)
            if size > limit:
                raise ValueError("download exceeded size limit")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def read_json(url, scratch, fetch):
    path = scratch / "metadata.json"
    fetch(url, path, METADATA_LIMIT)
    return json.loads(path.read_bytes())


def stable(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", version, re.ASCII):
        raise ValueError("expected a numeric stable version")
    return tuple(map(int, version.split(".")))


def npm_tarball(package, metadata, scratch, fetch):
    version = metadata["version"]
    stable(version)
    dist = metadata["dist"]
    url = f"{NPM}/{package}/-/{package}-{version}.tgz"
    if metadata.get("name") != package or dist["tarball"] != url:
        raise ValueError("npm package identity or tarball origin mismatch")
    path = scratch / "package.tgz"
    digest = fetch(url, path, ARCHIVE_LIMIT)
    with path.open("rb") as content:
        integrity = base64.b64encode(hashlib.file_digest(content, "sha512").digest()).decode()
    if dist["integrity"] != "sha512-" + integrity:
        raise ValueError("npm tarball integrity mismatch")
    return {"url": url, "sha256": digest}


def tar_members(path, names, *, source_root=None):
    """Read only named regular files; do not unpack arbitrary tar members."""
    result = {}
    seen = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if source_root is not None:
                parts = member.name.split("/")
                if (parts[0] != source_root or any(part in ("", ".", "..") for part in parts)
                        or "\\" in member.name or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                        or member.mode & 0o7000 or member.name in seen
                        or parts[1:] == ["npm-shrinkwrap.json"]):
                    raise ValueError("unsafe member in the REA source archive")
                seen.add(member.name)
            if member.name not in names:
                continue
            if member.name in result or not member.isfile() or not 0 < member.size <= METADATA_LIMIT:
                raise ValueError("invalid or duplicate required archive member")
            with archive.extractfile(member) as stream:
                result[member.name] = stream.read()
    if names != result.keys():
        raise ValueError("package is missing a required manifest, entry point, or skill")
    return result


def validate_npm_lock(lock, manifest):
    packages = lock["packages"]
    root = packages[""]
    keys = ("name", "version", "engines", "dependencies", "devDependencies",
            "optionalDependencies", "peerDependencies")
    if (lock["lockfileVersion"] != 3 or lock.get("name") != manifest.get("name")
            or lock.get("version") != manifest.get("version")
            or any(root.get(key) != manifest.get(key) for key in keys)):
        raise ValueError("npm lock does not match the REA manifest")
    for name, entry in packages.items():
        if not name:
            continue
        url = urlsplit(entry["resolved"])
        if (url.scheme != "https" or url.netloc != "registry.npmjs.org"
                or entry.get("link") or not entry.get("integrity")):
            raise ValueError("npm lock contains an unverified or non-registry dependency")


def release_commit(repository, tag, scratch, fetch):
    prefix = f"{GITHUB}/repos/{repository}/git/"
    ref = read_json(prefix + "ref/tags/" + quote(tag, safe=""), scratch, fetch)["object"]
    if not re.fullmatch(r"[0-9a-f]{40}", ref["sha"]):
        raise ValueError("invalid release ref SHA")
    if ref["type"] == "tag":
        ref = read_json(prefix + "tags/" + ref["sha"], scratch, fetch)["object"]
    if ref["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", ref["sha"]):
        raise ValueError("release tag does not identify a commit")
    return ref["sha"]


def stage_rea(scratch, current, fetch=download):
    release = read_json(f"{GITHUB}/repos/{REA_REPOSITORY}/releases/latest", scratch, fetch)
    tag = release.get("tag_name", "")
    match = REA_TAG.fullmatch(tag)
    if not match or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("expected a stable GitHub release for rea-agents")
    version = match[1]
    if stable(version) <= current:
        return None

    commit = release_commit(REA_REPOSITORY, tag, scratch, fetch)
    root = f"rea-{commit}"
    url = REA_SOURCE_URL + commit
    archive = scratch / "rea-source.tgz"
    digest = fetch(url, archive, ARCHIVE_LIMIT)
    manifest_member = f"{root}/package.json"
    lock_member = f"{root}/package-lock.json"
    skill_member = f"{root}/{REA_SKILL}"
    contents = tar_members(
        archive, {manifest_member, lock_member, skill_member, f"{root}/scripts/rea.mjs",
                  f"{root}/scripts/generate-skill-metadata.mjs"}, source_root=root,
    )
    manifest = json.loads(contents[manifest_member])
    if manifest["name"] != "rea-agents" or manifest["version"] != version:
        raise ValueError("REA source manifest does not match the release")
    validate_npm_lock(json.loads(contents[lock_member]), manifest)
    return {
        "version": version,
        "commit": commit,
        "source": {"url": url, "sha256": digest},
        "lock": {"path": "package-lock.json", "sha256": hashlib.sha256(contents[lock_member]).hexdigest()},
        "skill": {"path": REA_SKILL, "sha256": hashlib.sha256(contents[skill_member]).hexdigest()},
        "node": manifest["engines"]["node"],
    }


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=900, check=False)
    if result.returncode:
        raise ValueError(f"{command[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


def stage_ui(scratch):
    ui = ROOT / "static-inspection/workbench/ui"
    manifest = json.loads((ui / "package.json").read_bytes())
    candidates = {}
    for group in ("dependencies", "devDependencies"):
        for package, current in manifest[group].items():
            metadata = read_json(f"{NPM}/{package}/latest", scratch, download)
            version = metadata["version"]
            if stable(version) > stable(current):
                candidates[package] = {"version": version, **npm_tarball(package, metadata, scratch, download)}
                manifest[group][package] = version
    if manifest["dependencies"]["react"] != manifest["dependencies"]["react-dom"]:
        raise ValueError("React and React DOM must be staged at the same version")
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm is required to stage an updated workbench lock")
    previous_lock = json.loads((ui / "package-lock.json").read_bytes())
    validate_npm_lock(previous_lock, json.loads((ui / "package.json").read_bytes()))
    (scratch / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(ui / "package-lock.json", scratch / "package-lock.json")
    flags = ["--ignore-scripts", "--no-audit", "--no-fund", f"--registry={NPM}"]
    run([npm, "update", "--package-lock-only", *flags], cwd=scratch)
    lock = json.loads((scratch / "package-lock.json").read_bytes())
    validate_npm_lock(lock, manifest)
    if lock["packages"][""].get("devDependencies") != manifest["devDependencies"]:
        raise ValueError("workbench lock is missing its proposed development pins")
    if not candidates and lock["packages"] == previous_lock["packages"]:
        return None
    run([npm, "ci", *flags], cwd=scratch)
    return {"downloads": candidates, "manifest": manifest, "lock": lock}


def stage_image(_scratch, image, candidate_tag=None):
    tag = candidate_tag or image.split("@", 1)[0]
    resolved = json.loads(run(["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", tag]))
    digest = resolved["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("invalid registry manifest digest")
    if f"{tag}@{digest}" == image:
        return None
    run(["docker", "pull", "--platform=linux/amd64", f"{tag}@{digest}"])
    return {"previous": image, "candidate": f"{tag}@{digest}"}


def stage_action(scratch, repository, pinned):
    # Some actions publish major-version aliases which move independently of releases.
    release = read_json(f"{GITHUB}/repos/{repository}/releases/latest", scratch, download)
    tag = release["tag_name"]
    stable(tag.removeprefix("v"))
    commit = release_commit(repository, tag, scratch, download)
    if commit == pinned:
        return None
    url = f"https://codeload.github.com/{repository}/tar.gz/{commit}"
    return {"tag": tag, "previous": pinned, "commit": commit,
            "source": {"url": url, "sha256": download(url, scratch / "source.tgz", ARCHIVE_LIMIT)}}


def sources():
    yield "rea-agents", stage_rea, (REA_BASELINE_VERSION,)
    yield "workbench-ui", stage_ui, ()

    worker_image = (ROOT / "static-inspection/workbench/worker-image.txt").read_text(encoding="utf-8").strip()
    yield worker_image.split("@", 1)[0], stage_image, (worker_image,)

    actions = {}
    for file in sorted((ROOT / ".github/workflows").glob("*.yml")):
        contents = file.read_text(encoding="utf-8")
        for repository, pin in re.findall(
                r"^\s+uses: ([^/]+/[^@]+)@([0-9a-f]{40})(?:\s|$)", contents, re.M):
            actions[repository, pin] = None
    for repository, pin in actions:
        yield f"action:{repository}@{pin[:8]}", stage_action, (repository, pin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="stage only this source (repeatable)")
    parser.add_argument("--output", type=Path, required=True, help="path for the JSON receipt")
    args = parser.parse_args()

    available = {name: (stage, inputs) for name, stage, inputs in sources()}
    selected = args.source or list(available)
    if unknown := set(selected) - available.keys():
        parser.error("unknown sources: " + ", ".join(sorted(unknown)))

    report = {}
    failed = False
    for name in dict.fromkeys(selected):
        stage, inputs = available[name]
        result = None
        try:
            with TemporaryDirectory(prefix="re-kit-candidate-") as directory:
                scratch = Path(directory)
                result = stage(scratch, *inputs)
            status = "staged" if result is not None else "no-new-candidate"
            report[name] = {"status": status, "candidate": result} if result else {"status": status}
        except HTTPError as error:
            report[name] = {"status": "failed", "http_status": error.code}
            failed = True
        except (OSError, EOFError, ValueError, KeyError, TypeError, StopIteration,
                tarfile.TarError, subprocess.TimeoutExpired) as error:
            # Don't print upstream response bodies, redirected location headers, or subprocess output.
            report[name] = {"status": "failed", "error_type": type(error).__name__}
            failed = True
        print(f"{name}: {report[name]['status']}", flush=True)

    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
