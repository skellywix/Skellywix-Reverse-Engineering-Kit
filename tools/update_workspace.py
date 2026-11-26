"""Command-line front end for the workspace dependency maintenance.

    python tools/update_workspace.py --root DATA_ROOT status
    python tools/update_workspace.py --root DATA_ROOT update      # resolve upstream and activate any changes
    python tools/update_workspace.py --root DATA_ROOT bootstrap   # first install from the checkout's channels
    python tools/update_workspace.py --root DATA_ROOT verify      # re-check installed files (and --images)

Prints one JSON document. Exit status is 1 when the state is ``failed`` (or ``verify`` finds problems), else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT))

from build_desk.maintenance import Maintenance, MaintenanceError  # noqa: E402 - import needs the sys.path entry above


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="workspace data directory (dependencies/ lives under it)")
    parser.add_argument("--checkout", type=Path, default=CHECKOUT, help="checkout holding workspace-dependencies.json")
    parser.add_argument("--no-discovery", action="store_true",
                        help="skip the first-party Kit policy check; still resolve the checkout's upstream channels")
    parser.add_argument("--images", action="store_true", help="verify: also check the images exist in Docker")
    parser.add_argument("command", choices=("status", "update", "bootstrap", "verify"))
    args = parser.parse_args(argv)
    kwargs = {"upstream": None} if args.no_discovery else {}
    maintenance = Maintenance(args.root, args.checkout, **kwargs)
    try:
        if args.command == "verify":
            problems = maintenance.verify(deep=True, images=args.images)
            print(json.dumps({"state": "failed" if problems else "verified", "problems": problems}, indent=2))
            return 1 if problems else 0
        if args.command == "status":
            result = maintenance.status()
        elif args.command == "update":
            result = maintenance.update(wait=True)
        else:
            result = maintenance.bootstrap(wait=True)
    except MaintenanceError as error:
        print(json.dumps({"state": "failed", "message": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result["state"] == "failed" else 0


if __name__ == "__main__":
    os.umask(0o077)  # The CLI and the launcher share a private operator data directory.
    sys.exit(main())
