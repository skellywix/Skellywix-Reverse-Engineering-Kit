export const MAX_INSPECT_FILE_SIZE = 16 * 1024 * 1024;

export type InspectSlot = 'a' | 'b';
export type InspectTool = 'identify_file' | 'compare_builds' | 'verify_checksum';
export type InspectEventTool = InspectTool | 'export_report';
export type InspectActor = 'operator' | 'agent' | 'skellywix';
export type InspectConnection = 'off' | 'ready' | 'connected' | 'expired' | 'spent';
export type CriticStatus =
  | 'idle'
  | 'not_requested'
  | 'pending'
  | 'reviewing'
  | 'ready'
  | 'failed'
  | 'expired'
  | 'cancelled'
  | 'stale';

export interface SpecimenMetadata {
  size: number;
  sha256: string;
  format: string;
  architecture: string;
}

export interface BuildComparisonResult {
  identical: boolean;
  size_delta: number;
  format_changed: boolean;
  architecture_changed: boolean;
}

export interface ChecksumResult {
  matches: boolean;
  expected: string;
  actual: string;
}

export interface ExportResult {
  files: number;
}

export type InspectResult =
  | SpecimenMetadata
  | BuildComparisonResult
  | ChecksumResult
  | ExportResult;

export interface InspectEvent {
  n: number;
  actor: InspectActor;
  tool: InspectEventTool;
  slot?: InspectSlot;
  result: InspectResult;
}

export interface InspectCritique {
  status: CriticStatus;
  end: number;
  text?: string;
  cites?: number[];
}

export interface InspectAdvisor {
  total: number;
  mismatches: number;
  actors: Record<InspectActor, number>;
  skipped: InspectTool[];
  critique: InspectCritique;
}

export interface InspectReport {
  schema: 'yuzu.report/1';
  revision: number;
  slots: Partial<Record<InspectSlot, SpecimenMetadata>>;
  events: InspectEvent[];
  earlier_events_omitted: number;
}

export interface InspectState extends InspectReport {
  connection: InspectConnection;
  remaining: number;
  advisor: InspectAdvisor;
}

export interface InspectStateEnvelope {
  schema_version: 1;
  engine: {
    state: 'up' | 'down';
    detail: string;
  };
  state: InspectState | null;
  legacy_evidence: Record<string, unknown> | null;
}

export type InspectRunRequest =
  | { revision: number; tool: 'identify_file'; arguments: { slot: InspectSlot } }
  | { revision: number; tool: 'compare_builds'; arguments: Record<string, never> }
  | {
    revision: number;
    tool: 'verify_checksum';
    arguments: { slot: InspectSlot; sha256: string };
  };

export interface InspectRunResponse {
  result: InspectResult;
  state: InspectState;
}

export interface InspectPairResponse {
  token: string;
  state: InspectState;
}

export interface InspectReviewRequest {
  revision: number;
  reference: string | null;
  critique: boolean;
}

export interface InspectApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

export interface InspectApiClient {
  getState(signal?: AbortSignal): Promise<InspectStateEnvelope>;
  upload(
    slot: InspectSlot,
    revision: number,
    file: File,
    signal?: AbortSignal,
  ): Promise<InspectState>;
  run(request: InspectRunRequest, signal?: AbortSignal): Promise<InspectRunResponse>;
  reset(revision: number, signal?: AbortSignal): Promise<InspectState>;
  getReport(signal?: AbortSignal): Promise<Blob>;
  review(request: InspectReviewRequest, signal?: AbortSignal): Promise<InspectState>;
  pair(revision: number, signal?: AbortSignal): Promise<InspectPairResponse>;
  disconnect(revision: number, signal?: AbortSignal): Promise<InspectState>;
  cancelCritic(revision: number, end: number, signal?: AbortSignal): Promise<InspectState>;
}

export class InspectValidationError extends Error {
  constructor(path: string, expected: string) {
    super(`${path} must be ${expected}`);
    this.name = 'InspectValidationError';
  }
}

export class InspectApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'InspectApiError';
    this.status = status;
    this.code = code;
  }
}

export class InspectStaleRevisionError extends InspectApiError {
  readonly currentRevision: number;

  constructor(status: number, message: string, currentRevision: number) {
    super(status, 'stale_revision', message);
    this.name = 'InspectStaleRevisionError';
    this.currentRevision = currentRevision;
  }
}

const INSPECT_TOOLS = new Set<InspectTool>([
  'identify_file',
  'compare_builds',
  'verify_checksum',
]);
const EVENT_TOOLS = new Set<InspectEventTool>([
  ...INSPECT_TOOLS,
  'export_report',
]);
const ACTORS = new Set<InspectActor>(['operator', 'agent', 'skellywix']);
const CONNECTIONS = new Set<InspectConnection>([
  'off',
  'ready',
  'connected',
  'expired',
  'spent',
]);
const CRITIC_STATUSES = new Set<CriticStatus>([
  'idle',
  'not_requested',
  'pending',
  'reviewing',
  'ready',
  'failed',
  'expired',
  'cancelled',
  'stale',
]);
const ENGINE_STATES = new Set(['up', 'down'] as const);
const DIGEST_PATTERN = /^[0-9a-f]{64}$/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function expectRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new InspectValidationError(path, 'an object');
  return value;
}

function expectArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new InspectValidationError(path, 'an array');
  return value;
}

function expectString(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new InspectValidationError(path, 'a non-empty string');
  }
  return value;
}

function expectBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') throw new InspectValidationError(path, 'a boolean');
  return value;
}

function expectUnsignedInteger(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new InspectValidationError(path, 'a non-negative safe integer');
  }
  return value;
}

function expectInteger(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
    throw new InspectValidationError(path, 'a safe integer');
  }
  return value;
}

function expectEnum<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  path: string,
): T {
  const parsed = expectString(value, path) as T;
  if (!allowed.has(parsed)) {
    throw new InspectValidationError(path, `one of ${[...allowed].join(', ')}`);
  }
  return parsed;
}

function expectDigest(value: unknown, path: string): string {
  const digest = expectString(value, path);
  if (!DIGEST_PATTERN.test(digest)) {
    throw new InspectValidationError(path, '64 hexadecimal characters');
  }
  return digest.toLowerCase();
}

function decodeSpecimen(value: unknown, path: string): SpecimenMetadata {
  const object = expectRecord(value, path);
  return {
    size: expectUnsignedInteger(object.size, `${path}.size`),
    sha256: expectDigest(object.sha256, `${path}.sha256`),
    format: expectString(object.format, `${path}.format`),
    architecture: expectString(object.architecture, `${path}.architecture`),
  };
}

function decodeComparison(value: unknown, path: string): BuildComparisonResult {
  const object = expectRecord(value, path);
  return {
    identical: expectBoolean(object.identical, `${path}.identical`),
    size_delta: expectInteger(object.size_delta, `${path}.size_delta`),
    format_changed: expectBoolean(object.format_changed, `${path}.format_changed`),
    architecture_changed: expectBoolean(
      object.architecture_changed,
      `${path}.architecture_changed`,
    ),
  };
}

function decodeChecksum(value: unknown, path: string): ChecksumResult {
  const object = expectRecord(value, path);
  return {
    matches: expectBoolean(object.matches, `${path}.matches`),
    expected: expectDigest(object.expected, `${path}.expected`),
    actual: expectDigest(object.actual, `${path}.actual`),
  };
}

function decodeExport(value: unknown, path: string): ExportResult {
  const object = expectRecord(value, path);
  return { files: expectUnsignedInteger(object.files, `${path}.files`) };
}

function decodeResult(
  tool: InspectEventTool,
  value: unknown,
  path: string,
): InspectResult {
  if (tool === 'identify_file') return decodeSpecimen(value, path);
  if (tool === 'compare_builds') return decodeComparison(value, path);
  if (tool === 'verify_checksum') return decodeChecksum(value, path);
  return decodeExport(value, path);
}

function decodeEvent(value: unknown, path: string): InspectEvent {
  const object = expectRecord(value, path);
  const tool = expectEnum(object.tool, EVENT_TOOLS, `${path}.tool`);
  const event: InspectEvent = {
    n: expectUnsignedInteger(object.n, `${path}.n`),
    actor: expectEnum(object.actor, ACTORS, `${path}.actor`),
    tool,
    result: decodeResult(tool, object.result, `${path}.result`),
  };
  if (event.n === 0) throw new InspectValidationError(`${path}.n`, 'a positive integer');
  if (object.slot !== undefined) {
    event.slot = expectEnum(object.slot, new Set<InspectSlot>(['a', 'b']), `${path}.slot`);
  }
  return event;
}

function decodeCritique(value: unknown, path: string): InspectCritique {
  const object = expectRecord(value, path);
  const status = expectEnum(object.status, CRITIC_STATUSES, `${path}.status`);
  const critique: InspectCritique = {
    status,
    end: expectUnsignedInteger(object.end, `${path}.end`),
  };
  if (status === 'ready') {
    critique.text = expectString(object.text, `${path}.text`);
    critique.cites = expectArray(object.cites, `${path}.cites`).map((citation, index) => {
      const parsed = expectUnsignedInteger(citation, `${path}.cites[${index}]`);
      if (parsed === 0) {
        throw new InspectValidationError(`${path}.cites[${index}]`, 'a positive integer');
      }
      return parsed;
    });
  }
  return critique;
}

function decodeAdvisor(value: unknown, path: string): InspectAdvisor {
  const object = expectRecord(value, path);
  const actors = expectRecord(object.actors, `${path}.actors`);
  return {
    total: expectUnsignedInteger(object.total, `${path}.total`),
    mismatches: expectUnsignedInteger(object.mismatches, `${path}.mismatches`),
    actors: {
      operator: expectUnsignedInteger(actors.operator, `${path}.actors.operator`),
      agent: expectUnsignedInteger(actors.agent, `${path}.actors.agent`),
      skellywix: expectUnsignedInteger(actors.skellywix, `${path}.actors.skellywix`),
    },
    skipped: expectArray(object.skipped, `${path}.skipped`).map((tool, index) =>
      expectEnum(tool, INSPECT_TOOLS, `${path}.skipped[${index}]`),
    ),
    critique: decodeCritique(object.critique, `${path}.critique`),
  };
}

function decodeInspectReportObject(
  object: Record<string, unknown>,
  path: string,
): InspectReport {
  if (object.schema !== 'yuzu.report/1') {
    throw new InspectValidationError(`${path}.schema`, 'yuzu.report/1');
  }
  const slotObject = expectRecord(object.slots, `${path}.slots`);
  const unexpectedSlot = Object.keys(slotObject).find((slot) => slot !== 'a' && slot !== 'b');
  if (unexpectedSlot !== undefined) {
    throw new InspectValidationError(`${path}.slots.${unexpectedSlot}`, 'slot a or b');
  }
  const slots: Partial<Record<InspectSlot, SpecimenMetadata>> = {};
  if (slotObject.a !== undefined) slots.a = decodeSpecimen(slotObject.a, `${path}.slots.a`);
  if (slotObject.b !== undefined) slots.b = decodeSpecimen(slotObject.b, `${path}.slots.b`);
  return {
    schema: 'yuzu.report/1',
    revision: expectUnsignedInteger(object.revision, `${path}.revision`),
    slots,
    events: expectArray(object.events, `${path}.events`).map((event, index) =>
      decodeEvent(event, `${path}.events[${index}]`),
    ),
    earlier_events_omitted: expectUnsignedInteger(
      object.earlier_events_omitted,
      `${path}.earlier_events_omitted`,
    ),
  };
}

export function decodeInspectReport(value: unknown): InspectReport {
  return decodeInspectReportObject(expectRecord(value, 'inspectReport'), 'inspectReport');
}

export function decodeInspectState(value: unknown): InspectState {
  const object = expectRecord(value, 'inspectState');
  return {
    ...decodeInspectReportObject(object, 'inspectState'),
    connection: expectEnum(object.connection, CONNECTIONS, 'inspectState.connection'),
    remaining: expectUnsignedInteger(object.remaining, 'inspectState.remaining'),
    advisor: decodeAdvisor(object.advisor, 'inspectState.advisor'),
  };
}

export function decodeInspectEnvelope(value: unknown): InspectStateEnvelope {
  const object = expectRecord(value, 'inspectEnvelope');
  if (object.schema_version !== 1) {
    throw new InspectValidationError('inspectEnvelope.schema_version', '1');
  }
  const engineObject = expectRecord(object.engine, 'inspectEnvelope.engine');
  const engineState = expectEnum(
    engineObject.state,
    ENGINE_STATES,
    'inspectEnvelope.engine.state',
  );
  const state = object.state === null ? null : decodeInspectState(object.state);
  if (engineState === 'up' && state === null) {
    throw new InspectValidationError('inspectEnvelope.state', 'an inspector state while up');
  }
  if (engineState !== 'up' && state !== null) {
    throw new InspectValidationError('inspectEnvelope.state', 'null while the engine is not up');
  }
  const legacyEvidence =
    object.legacy_evidence === null
      ? null
      : expectRecord(object.legacy_evidence, 'inspectEnvelope.legacy_evidence');
  if (engineState === 'down' && legacyEvidence !== null) {
    throw new InspectValidationError(
      'inspectEnvelope.legacy_evidence',
      'null while the engine is down',
    );
  }
  return {
    schema_version: 1,
    engine: {
      state: engineState,
      detail: expectString(engineObject.detail, 'inspectEnvelope.engine.detail'),
    },
    state,
    legacy_evidence: legacyEvidence,
  };
}

export function isSpecimenMetadata(result: InspectResult): result is SpecimenMetadata {
  return 'sha256' in result && 'format' in result && 'architecture' in result;
}

export function isBuildComparison(
  result: InspectResult,
): result is BuildComparisonResult {
  return 'identical' in result && 'size_delta' in result;
}

export function isChecksumResult(result: InspectResult): result is ChecksumResult {
  return 'matches' in result && 'expected' in result && 'actual' in result;
}

export interface ReplacementLoss {
  /** Ledger entries that loading a file clears, including count-only earlier entries. */
  entries: number;
  /** Checksum mismatches in the case history that loading a file clears. */
  mismatches: number;
  /** Earlier entries that are counted but not retained, so an export cannot include them. */
  notExportable: number;
  /** Loading A also clears the loaded B. */
  clearsSpecimenB: boolean;
  /** The read-only agent key is revoked. */
  revokesKey: boolean;
  /** A ready or in-flight model critique is discarded. */
  discardsCritique: boolean;
}

/**
 * Measures what the inspector will actually discard when this slot is loaded.
 * The inspector clears the ledger, review state, and key on every load, and
 * clears B whenever A is replaced.
 */
export function measureReplacementLoss(
  state: InspectState,
  slot: InspectSlot,
): ReplacementLoss {
  const status = state.advisor.critique.status;
  return {
    entries: state.advisor.total,
    mismatches: state.advisor.mismatches,
    notExportable: state.earlier_events_omitted,
    clearsSpecimenB: slot === 'a' && state.slots.b !== undefined,
    revokesKey: state.connection === 'ready' || state.connection === 'connected',
    discardsCritique:
      status === 'ready' || status === 'pending' || status === 'reviewing',
  };
}

/** A populated ledger is the only loss that interrupts loading. An empty ledger loads directly. */
export function needsReplacementConfirmation(loss: ReplacementLoss): boolean {
  return loss.entries > 0;
}

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new InspectValidationError('response', 'a JSON body');
  }
}

async function throwResponseError(response: Response): Promise<never> {
  let body: Record<string, unknown> = {};
  try {
    body = expectRecord(await response.json(), 'error');
  } catch (error) {
    if (error instanceof InspectValidationError) throw error;
    throw new InspectValidationError('error', 'a JSON object');
  }
  const code = typeof body.error === 'string' ? body.error : 'internal';
  const message = typeof body.message === 'string' ? body.message : `Request declined (${response.status}).`;
  if (response.status === 409 && code === 'stale_revision') {
    const currentRevision = expectUnsignedInteger(body.current_revision, 'error.current_revision');
    throw new InspectStaleRevisionError(response.status, message, currentRevision);
  }
  throw new InspectApiError(response.status, code, message);
}

function validateRevision(revision: number): void {
  expectUnsignedInteger(revision, 'revision');
}

function validateRunRequest(request: InspectRunRequest): void {
  validateRevision(request.revision);
  if (request.tool === 'compare_builds') {
    if (Object.keys(request.arguments).length !== 0) {
      throw new InspectValidationError('arguments', 'an empty object');
    }
    return;
  }
  if (request.arguments.slot !== 'a' && request.arguments.slot !== 'b') {
    throw new InspectValidationError('arguments.slot', 'a or b');
  }
  if (request.tool === 'verify_checksum') {
    expectDigest(request.arguments.sha256, 'arguments.sha256');
  }
}

export function createInspectApiClient(
  options: InspectApiClientOptions = {},
): InspectApiClient {
  const baseUrl = normalizedBaseUrl(options.baseUrl ?? '/api/v1/inspect');
  const fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));

  async function jsonRequest(
    path: string,
    init: RequestInit,
  ): Promise<unknown> {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      credentials: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      ...init,
    });
    if (!response.ok) await throwResponseError(response);
    return parseJson(response);
  }

  async function postState(
    path: string,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<InspectState> {
    const value = await jsonRequest(path, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: signal ?? null,
    });
    return decodeInspectState(value);
  }

  return {
    async getState(signal?: AbortSignal): Promise<InspectStateEnvelope> {
      const value = await jsonRequest('/state', {
        headers: { Accept: 'application/json' },
        signal: signal ?? null,
      });
      return decodeInspectEnvelope(value);
    },

    async upload(
      slot: InspectSlot,
      revision: number,
      file: File,
      signal?: AbortSignal,
    ): Promise<InspectState> {
      validateRevision(revision);
      if (slot !== 'a' && slot !== 'b') {
        throw new InspectValidationError('slot', 'a or b');
      }
      if (file.size > MAX_INSPECT_FILE_SIZE) {
        throw new InspectValidationError('file', 'no larger than 16 MiB');
      }
      const value = await jsonRequest(`/samples/${slot}`, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/octet-stream',
          'X-Workspace-Revision': String(revision),
        },
        body: file,
        signal: signal ?? null,
      });
      return decodeInspectState(value);
    },

    async run(
      request: InspectRunRequest,
      signal?: AbortSignal,
    ): Promise<InspectRunResponse> {
      validateRunRequest(request);
      const closedRequest: InspectRunRequest =
        request.tool === 'compare_builds'
          ? { revision: request.revision, tool: request.tool, arguments: {} }
          : request.tool === 'verify_checksum'
            ? {
              revision: request.revision,
              tool: request.tool,
              arguments: {
                slot: request.arguments.slot,
                sha256: request.arguments.sha256,
              },
            }
            : {
              revision: request.revision,
              tool: request.tool,
              arguments: { slot: request.arguments.slot },
            };
      const value = await jsonRequest('/run', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(closedRequest),
        signal: signal ?? null,
      });
      const object = expectRecord(value, 'runResponse');
      return {
        result: decodeResult(request.tool, object.result, 'runResponse.result'),
        state: decodeInspectState(object.state),
      };
    },

    async reset(revision: number, signal?: AbortSignal): Promise<InspectState> {
      validateRevision(revision);
      return postState('/reset', { revision }, signal);
    },

    async getReport(signal?: AbortSignal): Promise<Blob> {
      const response = await fetchImpl(`${baseUrl}/report`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        redirect: 'error',
        cache: 'no-store',
        signal: signal ?? null,
      });
      if (!response.ok) await throwResponseError(response);
      const report = decodeInspectReport(await parseJson(response));
      return new Blob([`${JSON.stringify(report, null, 2)}\n`], {
        type: 'application/json',
      });
    },

    async review(
      request: InspectReviewRequest,
      signal?: AbortSignal,
    ): Promise<InspectState> {
      validateRevision(request.revision);
      if (request.reference !== null) expectDigest(request.reference, 'reference');
      if (typeof request.critique !== 'boolean') {
        throw new InspectValidationError('critique', 'a boolean');
      }
      return postState(
        '/review',
        {
          revision: request.revision,
          reference: request.reference,
          critique: request.critique,
        },
        signal,
      );
    },

    async pair(revision: number, signal?: AbortSignal): Promise<InspectPairResponse> {
      validateRevision(revision);
      const value = await jsonRequest('/pair', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision }),
        signal: signal ?? null,
      });
      const object = expectRecord(value, 'pairResponse');
      return {
        token: expectString(object.token, 'pairResponse.token'),
        state: decodeInspectState(object.state),
      };
    },

    async disconnect(revision: number, signal?: AbortSignal): Promise<InspectState> {
      validateRevision(revision);
      return postState('/disconnect', { revision }, signal);
    },

    async cancelCritic(
      revision: number,
      end: number,
      signal?: AbortSignal,
    ): Promise<InspectState> {
      validateRevision(revision);
      const snapshotEnd = expectUnsignedInteger(end, 'end');
      if (snapshotEnd === 0) throw new InspectValidationError('end', 'a positive integer');
      return postState('/critic/cancel', { revision, end: snapshotEnd }, signal);
    },
  };
}
