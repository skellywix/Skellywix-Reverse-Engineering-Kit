#!/usr/bin/env bash
# Launch one offline, disposable inspection desk. stdout: container ID for docker stop.
set -eu
if (( $# < 1 || $# > 2 || EUID == 0 )) || [[ $# == 2 && $2 != --paired ]]; then
  printf 'Run as a non-root Docker user: bash run.sh <empty-private-directory> [--paired]\n' >&2
  exit 2
fi
root=$(cd -- "$(dirname -- "$0")" && pwd -P)
ipc=$(cd -- "$1" && pwd -P)
shift
if [[ $(stat -c '%u:%a' "$ipc") != "$(id -u):700" || -n $(ls -A -- "$ipc") || $ipc == *,* || $root == *,* ]]; then
  printf 'Use an empty, owner-private (0700) directory and paths without commas.\n' >&2
  exit 2
fi
if [[ ! -f $root/ui/dist/index.html || ! -r $root/ui/dist/skelly.webp || ! -r $root/ui/dist/skellywix-mark.svg ]]; then
  printf 'Build the UI first: npm ci --ignore-scripts and npm run build in workbench/ui.\n' >&2
  exit 2
fi
image=$(bash "$root/worker-image.sh")
mounts=(--mount "type=bind,src=$ipc,dst=/ipc" --mount "type=bind,src=$root/ui/dist,dst=/desk,readonly")
for file in server.py protocol.py case_state.py evidence.py; do
  mounts+=(--mount "type=bind,src=$root/$file,dst=/app/$file,readonly")
done
exec docker run --detach --rm --init --pull=never --network=none --read-only \
  --user "$(id -u):$(id -g)" --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=32 --cpus=0.5 --memory=128m --memory-swap=128m \
  "${mounts[@]}" "$image" python3 -IB /app/server.py --ipc /ipc --web /desk "$@"
