"""Operator entry point: `python -B -m portsetup COMMAND` (run from source-port/). No command approves anything."""
import argparse
import json
import sys

from verifier.errors import Refused, Unavailable

from . import install, runtime


def build_parser():
    parser = argparse.ArgumentParser(prog='portsetup', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('install', help='Create the protected root and install both fixed profiles.')
    setup.add_argument('--root', required=True)
    verify = commands.add_parser('check', help='Verify an installed root; writes nothing.')
    verify.add_argument('--root', required=True)
    archive = commands.add_parser('runtime-archive', help='Write the deterministic candidate-only image archive.')
    archive.add_argument('--output', required=True, help='A path that must not exist.')
    review = commands.add_parser('runtime-review', help='Print facts for the human review of the runtime image.')
    review.add_argument('--archive', help='An archive file to compare byte for byte with the generator.')
    review.add_argument('--image', help='A loaded image ID (sha256:...) to inspect through Docker.')
    approval = commands.add_parser('approval-review', help='Print what to read before approving an attempt by hand.')
    approval.add_argument('--root', required=True)
    approval.add_argument('project')
    approval.add_argument('attempt')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'install':
            print('\n'.join(install.install(args.root)))
        elif args.command == 'check':
            ok, lines = install.check(args.root)
            print('\n'.join(lines))
            return 0 if ok else 1
        elif args.command == 'runtime-archive':
            digest, command = runtime.write_archive(args.output)
            print(f'archive sha256 {digest}\nLoad it on the Linux Docker host with: {command}\n'
                  'Then run `python -B -m portsetup runtime-review --image <ID Docker reports>`.')
        elif args.command == 'runtime-review':
            print(json.dumps(runtime.review(args.archive, args.image), indent=2, sort_keys=True, ensure_ascii=True))
        else:
            print(install.render_review(install.approval_review(args.root, args.project, args.attempt)))
    except (Refused, Unavailable) as error:
        print(f'portsetup: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
