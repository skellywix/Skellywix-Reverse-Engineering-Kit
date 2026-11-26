#!/usr/bin/env bash
# Print the installed standalone worker ID. Only bootstrap or --refresh contacts the publisher.
set -eu
fail() { printf '%s\n' "$*" >&2; exit 1; }
if (( EUID == 0 || $# > 1 )) || [[ $# == 1 && $1 != --refresh ]]; then
  fail 'Run as a non-root Docker user: bash worker-image.sh [--refresh]'
fi
umask 077
root=$(cd -- "$(dirname -- "$0")" && pwd -P)
state=${XDG_STATE_HOME:-"$HOME/.local/state"}/re-kit/standalone-inspector
mkdir -p -- "$state"
[[ ! -L $state && $(stat -c '%u:%a' -- "$state") == "$(id -u):700" ]] || fail 'The standalone image state directory must be owner-private (0700).'
exec 9>"$state/update.lock"
flock -x 9
selection=$state/image-ids
old=
if [[ -e $selection || -L $selection ]]; then
  [[ -f $selection && ! -L $selection ]] || fail 'Invalid standalone image selection.'
  IFS= read -r old < "$selection" && [[ $old =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Invalid standalone image ID.'
  if (( $# == 0 )); then
    installed=$(docker image inspect --format '{{.Id}}' "$old") || fail 'The installed image is missing. Run worker-image.sh --refresh.'
    [[ $installed == "$old" ]] || fail 'Docker returned a different installed image.'
    printf '%s\n' "$old"
    exit 0
  fi
fi
IFS= read -r channel < "$root/worker-image.txt"
[[ $channel =~ ^python:[A-Za-z0-9_.-]+$ ]] || fail 'Expected an official Python image channel.'
docker pull --platform linux/amd64 "$channel" >&2
candidate=$(docker image inspect --format '{{.Id}}' "$channel")
[[ $candidate =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Docker did not return an immutable worker ID.'
# No case, IPC, credentials, or host-writable mounts in the candidate check.
files=()
for file in server.py protocol.py case_state.py evidence.py; do
  files+=(--mount "type=bind,src=$root/$file,dst=/app/$file,readonly")
done
docker run --rm --pull=never --network=none --read-only --user "$(id -u):$(id -g)" \
  --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=32 --cpus=0.5 --memory=128m --memory-swap=128m \
  "${files[@]}" --entrypoint python3 "$candidate" -IB -c '
import sys
sys.path.insert(0, "/app")
from server import summarize
expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
if sys.version_info.major != 3 or summarize(b"abc")["sha256"] != expected:
    raise SystemExit("The candidate failed the inspector compatibility check.")' >&2
if [[ $candidate != "$old" ]]; then
  stage=$(mktemp -- "$state/.selection.XXXXXXXX")
  trap 'rm -f -- "$stage"' EXIT
  printf '%s\n' "$candidate" > "$stage"
  if [[ -n $old ]]; then printf '%s\n' "$old" >> "$stage"; fi
  mv -fT -- "$stage" "$selection"
fi
printf '%s\n' "$candidate"
