"""Prepare the candidate-only runtime image and print what a human must review before it is pinned.

Nothing here pins, trusts, or reviews an image. The archive is generated from constants (verifier/candidate_image.py);
the image ID Docker reports for the loaded archive is read at the real boundary and only a reviewer can pin it in
verifier/containers.py (CANDIDATE_IMAGE and REVIEWED_CANDIDATE_ONLY_IMAGES), which also changes every recipe digest.
"""
import re
from pathlib import Path

from verifier import candidate_image, containers
from verifier.errors import Refused
from verifier.identity import sha256
from verifier.store import read_regular

IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
ARCHIVE_LIMIT = 1 << 20
NOTICE = ('NOT REVIEWED. These are facts for a human reviewer. Nothing was pinned, and the verifier still refuses to '
          'issue a receipt until a reviewed image is pinned in containers.py.')


def write_archive(output):
    """Write the generated archive to a path that must not exist. Returns (sha256, docker load command)."""
    data = candidate_image.archive()
    digest = sha256(data)
    if digest != candidate_image.ARCHIVE_SHA256:
        raise Refused('The archive generator no longer matches its pinned digest; the change needs review first.')
    path = Path(output)
    try:
        with path.open('xb') as stream:
            stream.write(data)
    except OSError as error:
        raise Refused(f'Could not create the archive: {error.strerror or error}') from error
    return digest, f'docker load -i {path}'


def review(archive=None, image=None):
    """Facts for the human review of the runtime image. Reads only; the one Docker call is `image inspect`."""
    generated = candidate_image.describe(candidate_image.archive())
    expected_id = f'sha256:{generated["configuration_sha256"]}'
    report = {
        'notice': NOTICE,
        'generator': {**generated, 'expected_classic_image_id': expected_id,
                      'expected_classic_image_id_note': (
                          'Derived offline: the classic Docker image store names an image by the SHA-256 of its '
                          'configuration. A containerd image store names it differently. Docker is the authority: '
                          'compare, then pin what Docker reports, never this value.')},
        'pinned': {'candidate_image': containers.CANDIDATE_IMAGE, 'observer_image': containers.OBSERVER_IMAGE,
                   'compiler_image': containers.COMPILER_IMAGE,
                   'reviewed_candidate_only_images': sorted(containers.REVIEWED_CANDIDATE_ONLY_IMAGES),
                   'runtime_blocker': containers.candidate_runtime_blocker()},
    }
    if archive is not None:
        data = read_regular(archive, ARCHIVE_LIMIT)
        report['archive_file'] = {'sha256': sha256(data), 'identical_to_generator': data == candidate_image.archive()}
    if image is not None:
        if not IMAGE_ID.fullmatch(image):
            raise Refused('The image must be given as an ID: sha256: followed by 64 lowercase hexadecimal characters.')
        facts = containers.inspect_image(image)
        problem = candidate_image.shape_problem(facts['layers'], facts['entrypoint'], facts['command'], facts['env'],
                                                facts['platform'])
        report['image'] = {**facts, 'id_equals_requested': facts['id'] == image,
                           'id_equals_expected_classic_id': facts['id'] == expected_id, 'shape_problem': problem}
    report['to_pin_after_review'] = [
        'Compare the archive members, the configuration, and the Docker-reported image facts above by hand.',
        'Edit verifier/containers.py: CANDIDATE_IMAGE = the Docker-reported ID; REVIEWED_CANDIDATE_ONLY_IMAGES = '
        'frozenset({that ID}).',
        'Regenerate both profiles with `python -B -m verifier describe <id> profiles/<id>` and update profile.json, '
        'manifest.json, and PINNED_MANIFEST_SHA256 (the recipe digest changes), then re-approve every attempt.']
    return report
