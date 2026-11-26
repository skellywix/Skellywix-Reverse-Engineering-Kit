export type JsonPrimitive = boolean | number | string | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export type ProjectKind = 'reconstruct' | 'inspect' | 'convert';
export type JobStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'awaiting_input'
  | 'blocked'
  | 'cancelling'
  | 'cancelled'
  | 'failed'
  | 'succeeded'
  | 'interrupted';
export type JobOutcome = 'verified' | 'unverified' | 'failed' | null;
export type EngineState = 'up' | 'down' | 'unknown';

export interface Project {
  id: string;
  kind: ProjectKind;
  name: string;
  revision: number;
  created_at: string;
}

export interface Job {
  id: string;
  project_id: string;
  attempt_id: string;
  attempt: number;
  status: JobStatus;
  phase: string;
  outcome: JobOutcome;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface EngineHealth {
  state: EngineState;
  detail: string;
}

export interface WorkspaceSnapshot {
  schema_version: 1;
  cursor: number;
  generated_at: string;
  projects: Project[];
  jobs: Job[];
  engines: {
    fourmilab: EngineHealth;
    inspector: EngineHealth;
  };
  releases: {
    active: JsonValue;
    previous: JsonValue;
    staged: JsonValue;
  };
}

export type CommandName =
  | 'create_project'
  | 'start_job'
  | 'cancel_job'
  | 'retry_job'
  | 'resume_job'
  | 'resolve_decision';

export interface CommandRequest {
  command: CommandName;
  project_id: string | null;
  job_id: string | null;
  expected_revision: number;
  payload: JsonObject;
}

export interface CommandReceipt {
  project_id: string;
  job_id: string | null;
  revision: number;
  cursor: number;
  replayed: boolean;
}

export type WorkspaceEventType =
  | 'project.created'
  | 'job.queued'
  | 'job.status_changed'
  | 'job.phase_changed'
  | 'job.decision_requested'
  | 'job.decision_resolved'
  | 'artifact.published'
  | 'engine.state_changed'
  | 'release.changed';

export interface WorkspaceEvent {
  schema_version: 1;
  event_id: number;
  project_id: string | null;
  job_id: string | null;
  attempt_id: string | null;
  revision: number;
  phase: string | null;
  type: WorkspaceEventType;
  at: string;
  payload: JsonObject;
}

export type ApiErrorCode =
  | 'bad_request'
  | 'forbidden_origin'
  | 'not_found'
  | 'stale_revision'
  | 'illegal_transition'
  | 'idempotency_conflict'
  | 'payload_too_large'
  | 'engine_unavailable'
  | 'internal';

export interface ApiErrorBody {
  error: ApiErrorCode;
  message: string;
  current_revision?: number;
}
