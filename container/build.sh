#!/bin/sh
# Resolve REA before building; this command never switches deployed image IDs.
set -eu
cd -- "$(dirname -- "$0")/.."

case "${1-}:$#" in
    :0) python3 tools/dependency_candidates.py --apply-rea ;;
    --pinned:1) ;;
    *) printf 'Usage: sh container/build.sh [--pinned]\n' >&2; exit 2 ;;
esac

exec docker compose -f container/compose.yaml --profile build-only build re-agent re-exec re-egress
