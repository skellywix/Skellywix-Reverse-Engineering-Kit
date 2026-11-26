PRAGMA foreign_keys = ON;

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('reconstruct', 'inspect', 'convert')),
    name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'starting', 'running', 'awaiting_input', 'blocked',
        'cancelling', 'cancelled', 'failed', 'succeeded', 'interrupted'
    )),
    phase TEXT NOT NULL,
    outcome TEXT CHECK (outcome IN ('verified', 'unverified', 'failed')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX jobs_project_id_idx ON jobs(project_id);
CREATE UNIQUE INDEX jobs_attempt_id_idx ON jobs(attempt_id);

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    number INTEGER NOT NULL CHECK (number >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'starting', 'running', 'awaiting_input', 'blocked',
        'cancelling', 'cancelled', 'failed', 'succeeded', 'interrupted'
    )),
    phase TEXT NOT NULL,
    outcome TEXT CHECK (outcome IN ('verified', 'unverified', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, number)
);

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    state_json TEXT NOT NULL,
    artifact_manifest_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX checkpoints_attempt_id_idx ON checkpoints(attempt_id, created_at);

CREATE TABLE artifact_manifests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
    attempt_id TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, sha256, relative_path)
);

CREATE TABLE release_targets (
    id TEXT PRIMARY KEY,
    slot TEXT NOT NULL CHECK (slot IN ('active', 'previous', 'staged')),
    artifact_manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id) ON DELETE RESTRICT,
    selector_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (slot)
);

CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE event_cursor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    high_water INTEGER NOT NULL CHECK (high_water >= 0)
);

INSERT INTO event_cursor(singleton, high_water) VALUES (1, 0);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY CHECK (event_id >= 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT,
    job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
    attempt_id TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    phase TEXT,
    event_type TEXT NOT NULL,
    at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX events_project_id_idx ON events(project_id, event_id);
CREATE INDEX events_job_id_idx ON events(job_id, event_id);

CREATE TRIGGER events_are_immutable
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;
