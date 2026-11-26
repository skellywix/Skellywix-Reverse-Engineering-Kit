"""Operator entry point: `python -B -m verifier --root DIR run PROJECT ATTEMPT` (run from source-port/).

It takes no command, approval, profile, or environment input. `describe` only prints digests of a profile directory.
"""
import argparse
import json
import sys

from .errors import Refused, ResultLost, Unavailable
from .profile import describe
from .run import verify


def main(argv=None):
    parser = argparse.ArgumentParser(prog='verifier', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Verify one queued, operator-approved attempt.')
    run.add_argument('--root', required=True, help='Protected root laid out as LAYOUT.md describes.')
    run.add_argument('project')
    run.add_argument('attempt')
    show = commands.add_parser('describe', help='Print identities and the manifest for a profile directory.')
    show.add_argument('project')
    show.add_argument('directory')
    args = parser.parse_args(argv)
    try:
        if args.command == 'describe':
            print(json.dumps(describe(args.project, args.directory), indent=2, sort_keys=True))
            return 0
        outcome = verify(args.root, args.project, args.attempt)
    except (Refused, Unavailable, ResultLost) as error:
        print(f'verifier: {error}', file=sys.stderr)
        return 1
    print(f'verifier: {outcome["state"]}: {outcome["detail"]}')
    return 0 if outcome['state'] == 'exercised' else 1


if __name__ == '__main__':
    sys.exit(main())
