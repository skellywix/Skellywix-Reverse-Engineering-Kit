-- An attempt is immutable. Only its comparison intent may change, once.
CREATE TABLE source_port_attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision > 0),
    target_json TEXT NOT NULL,
    compare_requested INTEGER NOT NULL DEFAULT 0 CHECK (compare_requested IN (0, 1)),
    UNIQUE (project_id, proposal_revision)
);
CREATE TRIGGER source_port_freeze_identity BEFORE UPDATE ON source_port_attempts
WHEN OLD.id != NEW.id OR OLD.project_id != NEW.project_id OR OLD.proposal_revision != NEW.proposal_revision
  OR OLD.target_json != NEW.target_json OR OLD.compare_requested > NEW.compare_requested
BEGIN
    SELECT RAISE(ABORT, 'source port attempt identity is immutable');
END;
