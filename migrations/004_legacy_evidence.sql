CREATE TABLE legacy_evidence_records (
    engine TEXT NOT NULL CHECK (engine IN ('fourmilab', 'inspector')),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    evidence_json TEXT NOT NULL CHECK (
        json_valid(evidence_json)
        AND length(evidence_json) <= 1048576
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (engine, evidence_sha256)
);

INSERT INTO legacy_evidence_records (
    engine,
    evidence_sha256,
    evidence_json,
    created_at
)
SELECT
    engine,
    evidence_sha256,
    COALESCE(evidence_json, 'null'),
    updated_at
FROM legacy_engine_projections;
