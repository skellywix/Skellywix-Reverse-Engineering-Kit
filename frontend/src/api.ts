import type {
  ApiErrorBody,
  ApiErrorCode,
  CommandName,
  CommandReceipt,
  CommandRequest,
  EngineHealth,
  EngineState,
  Job,
  JobOutcome,
  JobStatus,
  JsonObject,
  JsonValue,
  Project,
  ProjectKind,
  WorkspaceEvent,
  WorkspaceEventType,
  WorkspaceSnapshot,
} from './types';

const PROJECT_KINDS = new Set<ProjectKind>(['reconstruct', 'inspect', 'convert']);
const JOB_STATUSES = new Set<JobStatus>([
  'queued',
  'starting',
  'running',
  'awaiting_input',
  'blocked',
  'cancelling',
  'cancelled',
  'failed',
  'succeeded',
  'interrupted',
]);
const JOB_OUTCOMES = new Set<Exclude<JobOutcome, null>>(['verified', 'unverified', 'failed']);
const ENGINE_STATES = new Set<EngineState>(['up', 'down', 'unknown']);
const COMMAND_NAMES = new Set<CommandName>([
  'create_project',
  'start_job',
  'cancel_job',
  'retry_job',
  'resume_job',
  'resolve_decision',
]);
const EVENT_TYPES = new Set<WorkspaceEventType>([
  'project.created',
  'job.queued',
  'job.status_changed',
  'job.phase_changed',
  'job.decision_requested',
  'job.decision_resolved',
  'artifact.published',
  'engine.state_changed',
  'release.changed',
]);
const ERROR_CODES = new Set<ApiErrorCode>([
  'bad_request',
  'forbidden_origin',
  'not_found',
  'stale_revision',
  'illegal_transition',
  'idempotency_conflict',
  'payload_too_large',
  'engine_unavailable',
  'internal',
]);

export class ValidationError extends Error {
  constructor(path: string, expected: string) {
    super(`${path} must be ${expected}`);
    this.name = 'ValidationError';
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.error;
  }
}

export class StaleRevisionError extends ApiError {
  readonly currentRevision: number;

  constructor(status: number, body: ApiErrorBody & { current_revision: number }) {
    super(status, body);
    this.name = 'StaleRevisionError';
    this.currentRevision = body.current_revision;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function expectRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new ValidationError(path, 'an object');
  }
  return value;
}

function expectArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new ValidationError(path, 'an array');
  }
  return value;
}

function expectString(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ValidationError(path, 'a non-empty string');
  }
  return value;
}

function expectNullableString(value: unknown, path: string): string | null {
  return value === null ? null : expectString(value, path);
}

function expectBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ValidationError(path, 'a boolean');
  }
  return value;
}

function expectUnsignedInteger(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new ValidationError(path, 'a non-negative safe integer');
  }
  return value;
}

function expectPositiveInteger(value: unknown, path: string): number {
  const parsed = expectUnsignedInteger(value, path);
  if (parsed === 0) {
    throw new ValidationError(path, 'a positive safe integer');
  }
  return parsed;
}

function expectRfc3339Utc(value: unknown, path: string): string {
  const parsed = expectString(value, path);
  if (!parsed.endsWith('Z') || Number.isNaN(Date.parse(parsed))) {
    throw new ValidationError(path, 'an RFC 3339 UTC timestamp');
  }
  return parsed;
}

function expectEnum<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  path: string,
): T {
  const parsed = expectString(value, path);
  if (!allowed.has(parsed as T)) {
    throw new ValidationError(path, `one of ${[...allowed].join(', ')}`);
  }
  return parsed as T;
}

function isJsonValue(value: unknown): value is JsonValue {
  if (
    value === null ||
    typeof value === 'string' ||
    typeof value === 'boolean' ||
    (typeof value === 'number' && Number.isFinite(value))
  ) {
    return true;
  }
  if (Array.isArray(value)) {
    return value.every(isJsonValue);
  }
  return isRecord(value) && Object.values(value).every(isJsonValue);
}

function isJsonObject(value: unknown): value is JsonObject {
  return isRecord(value) && Object.values(value).every(isJsonValue);
}

function expectJsonValue(value: unknown, path: string): JsonValue {
  if (!isJsonValue(value)) {
    throw new ValidationError(path, 'valid JSON');
  }
  return value;
}

function expectJsonObject(value: unknown, path: string): JsonObject {
  if (!isJsonObject(value)) {
    throw new ValidationError(path, 'a JSON object');
  }
  return value;
}

function decodeProject(value: unknown, path: string): Project {
  const object = expectRecord(value, path);
  return {
    id: expectString(object.id, `${path}.id`),
    kind: expectEnum(object.kind, PROJECT_KINDS, `${path}.kind`),
    name: expectString(object.name, `${path}.name`),
    revision: expectUnsignedInteger(object.revision, `${path}.revision`),
    created_at: expectRfc3339Utc(object.created_at, `${path}.created_at`),
  };
}

function decodeJob(value: unknown, path: string): Job {
  const object = expectRecord(value, path);
  const outcome =
    object.outcome === null
      ? null
      : expectEnum(object.outcome, JOB_OUTCOMES, `${path}.outcome`);
  return {
    id: expectString(object.id, `${path}.id`),
    project_id: expectString(object.project_id, `${path}.project_id`),
    attempt_id: expectString(object.attempt_id, `${path}.attempt_id`),
    attempt: expectPositiveInteger(object.attempt, `${path}.attempt`),
    status: expectEnum(object.status, JOB_STATUSES, `${path}.status`),
    phase: expectString(object.phase, `${path}.phase`),
    outcome,
    revision: expectUnsignedInteger(object.revision, `${path}.revision`),
    created_at: expectRfc3339Utc(object.created_at, `${path}.created_at`),
    updated_at: expectRfc3339Utc(object.updated_at, `${path}.updated_at`),
  };
}

function decodeEngine(value: unknown, path: string): EngineHealth {
  const object = expectRecord(value, path);
  return {
    state: expectEnum(object.state, ENGINE_STATES, `${path}.state`),
    detail: expectString(object.detail, `${path}.detail`),
  };
}

export function decodeSnapshot(value: unknown): WorkspaceSnapshot {
  const object = expectRecord(value, 'snapshot');
  if (object.schema_version !== 1) {
    throw new ValidationError('snapshot.schema_version', '1');
  }
  const engines = expectRecord(object.engines, 'snapshot.engines');
  const releases = expectRecord(object.releases, 'snapshot.releases');
  return {
    schema_version: 1,
    cursor: expectUnsignedInteger(object.cursor, 'snapshot.cursor'),
    generated_at: expectRfc3339Utc(object.generated_at, 'snapshot.generated_at'),
    projects: expectArray(object.projects, 'snapshot.projects').map((project, index) =>
      decodeProject(project, `snapshot.projects[${index}]`),
    ),
    jobs: expectArray(object.jobs, 'snapshot.jobs').map((job, index) =>
      decodeJob(job, `snapshot.jobs[${index}]`),
    ),
    engines: {
      fourmilab: decodeEngine(engines.fourmilab, 'snapshot.engines.fourmilab'),
      inspector: decodeEngine(engines.inspector, 'snapshot.engines.inspector'),
    },
    releases: {
      active: expectJsonValue(releases.active, 'snapshot.releases.active'),
      previous: expectJsonValue(releases.previous, 'snapshot.releases.previous'),
      staged: expectJsonValue(releases.staged, 'snapshot.releases.staged'),
    },
  };
}

export function decodeCommandReceipt(value: unknown): CommandReceipt {
  const object = expectRecord(value, 'receipt');
  return {
    project_id: expectString(object.project_id, 'receipt.project_id'),
    job_id: expectNullableString(object.job_id, 'receipt.job_id'),
    revision: expectUnsignedInteger(object.revision, 'receipt.revision'),
    cursor: expectUnsignedInteger(object.cursor, 'receipt.cursor'),
    replayed: expectBoolean(object.replayed, 'receipt.replayed'),
  };
}

export function decodeWorkspaceEvent(value: unknown): WorkspaceEvent {
  const object = expectRecord(value, 'event');
  if (object.schema_version !== 1) {
    throw new ValidationError('event.schema_version', '1');
  }
  const type = expectEnum(object.type, EVENT_TYPES, 'event.type');
  const phase = object.phase === null ? null : expectString(object.phase, 'event.phase');
  if (phase !== null && !/^[a-z][a-z0-9_]*$/.test(phase)) {
    throw new ValidationError('event.phase', 'a lowercase workflow token');
  }
  return {
    schema_version: 1,
    event_id: expectPositiveInteger(object.event_id, 'event.event_id'),
    project_id: type === 'engine.state_changed'
      ? expectNullableString(object.project_id, 'event.project_id')
      : expectString(object.project_id, 'event.project_id'),
    job_id: expectNullableString(object.job_id, 'event.job_id'),
    attempt_id: expectNullableString(object.attempt_id, 'event.attempt_id'),
    revision: expectUnsignedInteger(object.revision, 'event.revision'),
    phase,
    type,
    at: expectRfc3339Utc(object.at, 'event.at'),
    payload: expectJsonObject(object.payload, 'event.payload'),
  };
}

function decodeApiError(value: unknown): ApiErrorBody {
  const object = expectRecord(value, 'error');
  const code = expectEnum(object.error, ERROR_CODES, 'error.error');
  const body: ApiErrorBody = {
    error: code,
    message: expectString(object.message, 'error.message'),
  };
  if (object.current_revision !== undefined) {
    body.current_revision = expectUnsignedInteger(
      object.current_revision,
      'error.current_revision',
    );
  }
  return body;
}

function validateCommandRequest(request: CommandRequest): void {
  expectEnum(request.command, COMMAND_NAMES, 'command.command');
  if (request.project_id !== null) {
    expectString(request.project_id, 'command.project_id');
  }
  if (request.job_id !== null) {
    expectString(request.job_id, 'command.job_id');
  }
  expectUnsignedInteger(request.expected_revision, 'command.expected_revision');
  expectJsonObject(request.payload, 'command.payload');
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ValidationError('response', 'a JSON body');
  }
}

function generateIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID !== 'function') {
    throw new Error('Secure random UUID generation is unavailable');
  }
  return globalThis.crypto.randomUUID();
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  createIdempotencyKey?: () => string;
}

export interface CommandOptions {
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export interface ApiClient {
  getSnapshot(signal?: AbortSignal): Promise<WorkspaceSnapshot>;
  command(request: CommandRequest, options?: CommandOptions): Promise<CommandReceipt>;
}

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = normalizedBaseUrl(options.baseUrl ?? '/api/v1');
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const createIdempotencyKey = options.createIdempotencyKey ?? generateIdempotencyKey;

  return {
    async getSnapshot(signal?: AbortSignal): Promise<WorkspaceSnapshot> {
      const response = await fetchImpl(`${baseUrl}/snapshot`, {
        headers: { Accept: 'application/json' },
        signal: signal ?? null,
      });
      const body = await parseJson(response);
      if (!response.ok) {
        throw new ApiError(response.status, decodeApiError(body));
      }
      return decodeSnapshot(body);
    },

    async command(
      request: CommandRequest,
      commandOptions: CommandOptions = {},
    ): Promise<CommandReceipt> {
      validateCommandRequest(request);
      const idempotencyKey = commandOptions.idempotencyKey ?? createIdempotencyKey();
      if (!/^[\x21-\x7E]{1,128}$/.test(idempotencyKey)) {
        throw new ValidationError(
          'command.idempotencyKey',
          '1-128 visible ASCII characters',
        );
      }
      const response = await fetchImpl(`${baseUrl}/commands`, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(request),
        signal: commandOptions.signal ?? null,
      });
      const body = await parseJson(response);
      if (!response.ok) {
        const error = decodeApiError(body);
        if (response.status === 409 && error.error === 'stale_revision') {
          if (error.current_revision === undefined) {
            throw new ValidationError('error.current_revision', 'a non-negative safe integer');
          }
          throw new StaleRevisionError(response.status, {
            ...error,
            current_revision: error.current_revision,
          });
        }
        throw new ApiError(response.status, error);
      }
      return decodeCommandReceipt(body);
    },
  };
}
