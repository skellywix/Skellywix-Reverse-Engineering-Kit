"""Digest and Target rules from source-port-v1. Digests are over exact bytes."""
import hashlib
import json
import re
from dataclasses import dataclass

from .errors import Refused

PROJECT = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
ATTEMPT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
CASE = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
SHA256 = re.compile(r'[0-9a-f]{64}')
TARGET_FIELDS = ('project_id', 'attempt_id', 'source_sha256', 'candidate_sha256',
                 'corpus_sha256', 'recipe_sha256', 'toolchain_sha256')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    """Deterministic JSON bytes: sorted keys, no whitespace, ASCII, one trailing LF."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'


def parse_json(data, what):
    try:
        return json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise Refused(f'{what} is not valid UTF-8 JSON.') from error


def exact_keys(value, keys, what):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Refused(f'{what} must be an object with exactly these fields: {", ".join(sorted(keys))}.')
    return value


def valid_attempt(attempt_id):
    return isinstance(attempt_id, str) and ATTEMPT.fullmatch(attempt_id) is not None


@dataclass(frozen=True)
class Target:
    project_id: str
    attempt_id: str
    source_sha256: str
    candidate_sha256: str
    corpus_sha256: str
    recipe_sha256: str
    toolchain_sha256: str

    def __post_init__(self):
        if not (isinstance(self.project_id, str) and PROJECT.fullmatch(self.project_id)):
            raise Refused('Target project_id is not a valid project ID.')
        if not valid_attempt(self.attempt_id):
            raise Refused('Target attempt_id is not a valid attempt ID.')
        for name in TARGET_FIELDS[2:]:
            value = getattr(self, name)
            if not (isinstance(value, str) and SHA256.fullmatch(value)):
                raise Refused(f'Target {name} is not 64 lowercase hexadecimal characters.')

    def as_dict(self):
        return {name: getattr(self, name) for name in TARGET_FIELDS}

    @classmethod
    def from_wire(cls, value):
        return cls(**exact_keys(value, TARGET_FIELDS, 'Target'))

    def differences(self, other):
        return [name for name in TARGET_FIELDS if getattr(self, name) != getattr(other, name)]
