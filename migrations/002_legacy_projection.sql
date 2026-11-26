CREATE TABLE legacy_engine_projections (
    engine TEXT PRIMARY KEY CHECK (engine IN ('fourmilab', 'inspector')),
    availability TEXT NOT NULL CHECK (availability IN ('up', 'down')),
    detail TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    evidence_json TEXT,
    evidence_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE legacy_job_bindings (
    engine TEXT NOT NULL CHECK (engine IN ('fourmilab', 'inspector')),
    legacy_key TEXT NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (engine, legacy_key)
);
