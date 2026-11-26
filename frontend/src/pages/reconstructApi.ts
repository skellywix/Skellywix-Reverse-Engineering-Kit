const API_ROOT = '/api/v1/reconstruct';
const RUN_ID_PATTERN = /^[0-9a-f]{32}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const STAGE_NAMES = ['intake', 'analyze', 'rebuild', 'verify', 'package'] as const;

export type ReconstructStageName = (typeof STAGE_NAMES)[number];

export interface ReconstructEngine {
  state: 'up' | 'down';
  detail: string;
}

export interface ReconstructProfile {
  id: 'fourmilab_base64_1_5';
  name: string;
  fixed: true;
  requiredCases: 2557;
}

export interface ReconstructRunSummary {
  id: string;
  attempt: number;
  status: string;
  stage: string;
}

export interface ReconstructStage {
  name: ReconstructStageName;
  status: string;
  completed: number | null;
  required: number | null;
  detail: Readonly<Record<string, unknown>>;
}

export interface ReconstructSelectedRun extends ReconstructRunSummary {
  stages: readonly ReconstructStage[];
  proof: {
    matched: number | null;
    required: number;
    verified: boolean;
  };
  model: {
    requested: string;
    actual: string | null;
  };
  package: {
    available: boolean;
    sha256: string | null;
  };
}

export interface ReconstructReleases {
  active: string | null;
  previous: string | null;
  staged: string | null;
}

export interface ReconstructState {
  schemaVersion: 1;
  engine: ReconstructEngine;
  profile: ReconstructProfile | null;
  runs: readonly ReconstructRunSummary[];
  selectedRun: ReconstructSelectedRun | null;
  releases: ReconstructReleases | null;
  busy: boolean;
  running: string | null;
  updateError: string | null;
  builderConnected: boolean;
}

export interface ReconstructActionReceipt {
  ok: true;
  run: string | null;
}

export interface ReconstructApi {
  readState(runId?: string, signal?: AbortSignal): Promise<ReconstructState>;
  start(): Promise<ReconstructActionReceipt>;
  cancel(runId: string): Promise<ReconstructActionReceipt>;
  retry(runId: string): Promise<ReconstructActionReceipt>;
  stageRelease(): Promise<ReconstructActionReceipt>;
  promoteRelease(targetDigest: string): Promise<ReconstructActionReceipt>;
  rollbackRelease(targetDigest: string): Promise<ReconstructActionReceipt>;
  packageUrl(runId: string): string;
}

export interface ReconstructApiOptions {
  fetchImpl?: typeof fetch;
}

export class ReconstructApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly outcomeKnown: boolean;

  constructor(
    message: string,
    status: number,
    code: string | null = null,
    outcomeKnown = false,
  ) {
    super(message);
    this.name = 'ReconstructApiError';
    this.status = status;
    this.code = code;
    this.outcomeKnown = outcomeKnown;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new ReconstructApiError(`${path} must be an object`, 502);
  }
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== 'string') {
    throw new ReconstructApiError(`${path} must be a string`, 502);
  }
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  return value === null ? null : string(value, path);
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ReconstructApiError(`${path} must be a boolean`, 502);
  }
  return value;
}

function unsignedInteger(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new ReconstructApiError(`${path} must be a non-negative integer`, 502);
  }
  return value as number;
}

function positiveInteger(value: unknown, path: string): number {
  const decoded = unsignedInteger(value, path);
  if (decoded === 0) {
    throw new ReconstructApiError(`${path} must be a positive integer`, 502);
  }
  return decoded;
}

function nullableUnsignedInteger(value: unknown, path: string): number | null {
  return value === null ? null : unsignedInteger(value, path);
}

function runId(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!RUN_ID_PATTERN.test(decoded)) {
    throw new ReconstructApiError(`${path} must be 32 lowercase hexadecimal characters`, 502);
  }
  return decoded;
}

function nullableRunId(value: unknown, path: string): string | null {
  return value === null ? null : runId(value, path);
}

function digest(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!DIGEST_PATTERN.test(decoded)) {
    throw new ReconstructApiError(`${path} must be 64 lowercase hexadecimal characters`, 502);
  }
  return decoded;
}

function nullableDigest(value: unknown, path: string): string | null {
  return value === null ? null : digest(value, path);
}

function decodeEngine(value: unknown): ReconstructEngine {
  const source = record(value, 'reconstruct.engine');
  const state = string(source.state, 'reconstruct.engine.state');
  if (state !== 'up' && state !== 'down') {
    throw new ReconstructApiError('reconstruct.engine.state must be up or down', 502);
  }
  return { state, detail: string(source.detail, 'reconstruct.engine.detail') };
}

function decodeRunSummary(value: unknown, path: string): ReconstructRunSummary {
  const source = record(value, path);
  return {
    id: runId(source.id, `${path}.id`),
    attempt: positiveInteger(source.attempt, `${path}.attempt`),
    status: string(source.status, `${path}.status`),
    stage: string(source.stage, `${path}.stage`),
  };
}

function decodeStage(value: unknown, index: number): ReconstructStage {
  const path = `reconstruct.selected_run.stages[${index}]`;
  const source = record(value, path);
  const name = string(source.name, `${path}.name`);
  if (name !== STAGE_NAMES[index]) {
    throw new ReconstructApiError(`${path}.name must be ${STAGE_NAMES[index]}`, 502);
  }
  return {
    name,
    status: string(source.status, `${path}.status`),
    completed: nullableUnsignedInteger(source.completed, `${path}.completed`),
    required: nullableUnsignedInteger(source.required, `${path}.required`),
    detail: record(source.detail, `${path}.detail`),
  };
}

function decodeSelectedRun(value: unknown): ReconstructSelectedRun | null {
  if (value === null) {
    return null;
  }
  const source = record(value, 'reconstruct.selected_run');
  if (!Array.isArray(source.stages) || source.stages.length !== STAGE_NAMES.length) {
    throw new ReconstructApiError('reconstruct.selected_run.stages must contain five stages', 502);
  }
  const proof = record(source.proof, 'reconstruct.selected_run.proof');
  const model = record(source.model, 'reconstruct.selected_run.model');
  const packageState = record(source.package, 'reconstruct.selected_run.package');
  return {
    ...decodeRunSummary(source, 'reconstruct.selected_run'),
    stages: source.stages.map(decodeStage),
    proof: {
      matched: nullableUnsignedInteger(proof.matched, 'reconstruct.selected_run.proof.matched'),
      required: unsignedInteger(proof.required, 'reconstruct.selected_run.proof.required'),
      verified: boolean(proof.verified, 'reconstruct.selected_run.proof.verified'),
    },
    model: {
      requested: string(model.requested, 'reconstruct.selected_run.model.requested'),
      actual: nullableString(model.actual, 'reconstruct.selected_run.model.actual'),
    },
    package: {
      available: boolean(packageState.available, 'reconstruct.selected_run.package.available'),
      sha256: nullableDigest(packageState.sha256, 'reconstruct.selected_run.package.sha256'),
    },
  };
}

function decodeProfile(value: unknown): ReconstructProfile {
  const source = record(value, 'reconstruct.profile');
  if (source.id !== 'fourmilab_base64_1_5') {
    throw new ReconstructApiError('reconstruct.profile.id is not the fixed Fourmilab profile', 502);
  }
  if (source.fixed !== true || source.required_cases !== 2557) {
    throw new ReconstructApiError('reconstruct.profile must be fixed at 2,557 cases', 502);
  }
  return {
    id: source.id,
    name: string(source.name, 'reconstruct.profile.name'),
    fixed: true,
    requiredCases: 2557,
  };
}

function decodeReleases(value: unknown): ReconstructReleases {
  const source = record(value, 'reconstruct.releases');
  if (!Array.isArray(source.items)) {
    throw new ReconstructApiError('reconstruct.releases.items must be an array', 502);
  }
  return {
    active: nullableDigest(source.active, 'reconstruct.releases.active'),
    previous: nullableDigest(source.previous, 'reconstruct.releases.previous'),
    staged: nullableDigest(source.staged, 'reconstruct.releases.staged'),
  };
}

export function decodeReconstructState(value: unknown): ReconstructState {
  const source = record(value, 'reconstruct');
  if (source.schema_version !== 1) {
    throw new ReconstructApiError('reconstruct.schema_version must be 1', 502);
  }
  const engine = decodeEngine(source.engine);
  if (engine.state === 'down') {
    return {
      schemaVersion: 1,
      engine,
      profile: null,
      runs: [],
      selectedRun: null,
      releases: null,
      busy: false,
      running: null,
      updateError: nullableString(source.update_error, 'reconstruct.update_error'),
      builderConnected: false,
    };
  }
  if (!Array.isArray(source.runs) || !Array.isArray(source.catalog)) {
    throw new ReconstructApiError('reconstruct runs and catalog must be arrays', 502);
  }
  return {
    schemaVersion: 1,
    engine,
    profile: decodeProfile(source.profile),
    runs: source.runs.map((run, index) => decodeRunSummary(run, `reconstruct.runs[${index}]`)),
    selectedRun: decodeSelectedRun(source.selected_run),
    releases: decodeReleases(source.releases),
    busy: boolean(source.busy, 'reconstruct.busy'),
    running: nullableRunId(source.running, 'reconstruct.running'),
    updateError: nullableString(source.update_error, 'reconstruct.update_error'),
    builderConnected: boolean(source.builder_connected, 'reconstruct.builder_connected'),
  };
}

function decodeReceipt(value: unknown): ReconstructActionReceipt {
  const source = record(value, 'reconstruct.action');
  if (source.ok !== true) {
    throw new ReconstructApiError('reconstruct action did not return ok', 502);
  }
  return {
    ok: true,
    run: source.run === undefined ? null : nullableRunId(source.run, 'reconstruct.action.run'),
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ReconstructApiError('The reconstruction service returned invalid JSON', response.status);
  }
}

async function expectSuccess(response: Response): Promise<unknown> {
  const payload = await readJson(response);
  if (response.ok) {
    return payload;
  }
  const source = isRecord(payload) ? payload : {};
  const message =
    typeof source.message === 'string'
      ? source.message
      : typeof source.error === 'string'
        ? source.error
        : `Reconstruction request failed with status ${response.status}`;
  throw new ReconstructApiError(
    message,
    response.status,
    typeof source.error === 'string' ? source.error : null,
    true,
  );
}

function assertRunId(value: string): void {
  if (!RUN_ID_PATTERN.test(value)) {
    throw new ReconstructApiError(
      'Run id must be 32 lowercase hexadecimal characters',
      400,
      null,
      true,
    );
  }
}

function assertDigest(value: string): void {
  if (!DIGEST_PATTERN.test(value)) {
    throw new ReconstructApiError(
      'Release digest must be 64 lowercase hexadecimal characters',
      400,
      null,
      true,
    );
  }
}

export function createReconstructApi(options: ReconstructApiOptions = {}): ReconstructApi {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;

  const post = async (path: string, body: Readonly<Record<string, unknown>>) => {
    const response = await fetchImpl(`${API_ROOT}${path}`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    return decodeReceipt(await expectSuccess(response));
  };

  return {
    async readState(selectedRunId, signal) {
      if (selectedRunId !== undefined) {
        assertRunId(selectedRunId);
      }
      const query = selectedRunId === undefined ? '' : `?run=${selectedRunId}`;
      const response = await fetchImpl(`${API_ROOT}/state${query}`, {
        headers: { Accept: 'application/json' },
        ...(signal === undefined ? {} : { signal }),
      });
      return decodeReconstructState(await expectSuccess(response));
    },
    start: () => post('/start', {}),
    cancel: (selectedRunId) => {
      assertRunId(selectedRunId);
      return post(`/runs/${selectedRunId}/cancel`, {});
    },
    retry: (selectedRunId) => {
      assertRunId(selectedRunId);
      return post(`/runs/${selectedRunId}/retry`, {});
    },
    stageRelease: () => post('/releases/stage', {}),
    promoteRelease: (targetDigest) => {
      assertDigest(targetDigest);
      return post('/releases/promote', {
        target_digest: targetDigest,
        confirm_target: targetDigest,
      });
    },
    rollbackRelease: (targetDigest) => {
      assertDigest(targetDigest);
      return post('/releases/rollback', {
        target_digest: targetDigest,
        confirm_target: targetDigest,
      });
    },
    packageUrl: (selectedRunId) => {
      assertRunId(selectedRunId);
      return `${API_ROOT}/runs/${selectedRunId}/package`;
    },
  };
}
