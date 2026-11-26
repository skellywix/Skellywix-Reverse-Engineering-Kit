#!/usr/bin/env bash
# One local, disposable desk. Never run specimens or publish a Docker port.
set -eu
fail() { printf '%s\n' "$*" >&2; exit 1; }
port=${1-48730}
if (( $# > 1 )) || ! [[ $port =~ ^[1-9][0-9]{3,4}$ ]] || (( port < 1024 || port > 65534 )); then
  fail 'Usage: bash start.sh [first-loopback-port (1024–65534)]'
fi
(( EUID != 0 )) || fail 'Use a non-root account on a single-user Linux workstation.'
for cmd in docker systemd-socket-activate curl; do
  command -v "$cmd" >/dev/null || fail "Missing prerequisite: $cmd"
done
proxy=$(command -v systemd-socket-proxyd || :)
for path in /usr/lib/systemd/systemd-socket-proxyd /lib/systemd/systemd-socket-proxyd; do
  [[ -x $proxy ]] && break
  [[ ! -x $path ]] || proxy=$path
done
[[ -x $proxy ]] || fail 'Missing prerequisite: systemd-socket-proxyd'
root=$(cd -- "$(dirname -- "$0")/workbench" && pwd -P)
docker info --format '{{.ServerVersion}}' >/dev/null

umask 077
ipc=$(mktemp -d)
worker=''
children=()
cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  for child in "${children[@]}"; do kill "$child" 2>/dev/null || :; done
  for child in "${children[@]}"; do wait "$child" 2>/dev/null || :; done
  if [[ -n $worker ]] && ! docker stop -t 2 "$worker" >/dev/null; then
    printf 'Could not confirm worker shutdown. Inspect container %s and private directory %s.\n' "$worker" "$ipc" >&2
    exit 1
  fi
  rm -f -- "$ipc/desk.sock" "$ipc/agent.sock"
  rmdir -- "$ipc"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
worker=$(bash "$root/run.sh" "$ipc")
for name in desk agent; do
  listen=$port; [[ $name != agent ]] || listen=$((port + 1))
  systemd-socket-activate -l "127.0.0.1:$listen" "$proxy" "$ipc/$name.sock" &
  children+=("$!")
done
docker wait "$worker" >/dev/null &
children+=("$!")
status_at() { curl --noproxy '*' --silent --max-time 1 -o /dev/null -w '%{http_code}' "$1"; }
desk=http://127.0.0.1:$port
agent=http://127.0.0.1:$((port + 1))/mcp
for ((i=0; i<100; i++)); do
  for child in "${children[@]}"; do kill -0 "$child" 2>/dev/null || fail 'The worker or a listener stopped. Check both ports.'; done
  if [[ -S $ipc/desk.sock && -S $ipc/agent.sock ]] && [[ $(status_at "$desk/api/state") == 200 && $(status_at "$agent") == 401 ]]; then
    for child in "${children[@]}"; do kill -0 "$child" 2>/dev/null || fail 'A listener or the worker exited during startup.'; done
    printf '\nOpen %s/\nPress Control-C here to stop the desk and discard the case.\n' "$desk"
    wait -n || :
    fail 'The worker or a listener stopped unexpectedly.'
  fi
  sleep 0.1
done
fail 'The local desk did not become ready.'
