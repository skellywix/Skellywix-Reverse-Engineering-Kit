// Page-private client, validation and state rules for the reviewed-source Convert and Runtime views.
// Contract: docs/superpowers/contracts/source-port-v1.md. Reads are side-effect free; the only
// mutations are a proposal submission and a comparison request, each with an expected revision and
// an idempotency key. Nothing here approves code, writes verifier evidence or reaches a provider.
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, StaleRevisionError, ValidationError } from '../api';
import type { ApiErrorCode } from '../types';

const BASE_URL = '/api/v1';
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const IDEMPOTENCY_KEY_PATTERN = /^[\x21-\x7E]{1,128}$/;
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

export const PHASES = [
  'registered',
  'awaiting_review',
  'approved',
  'queued',
  'comparing',
  'mismatch',
  'blocked',
  'verified',
] as const;
export type Phase = (typeof PHASES)[number];

export const RECIPE_STATES = ['configured', 'exercised', 'unavailable', 'failed'] as const;
export type RecipeState = (typeof RECIPE_STATES)[number];

const ERROR_CODES: ReadonlySet<string> = new Set<ApiErrorCode>([
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

export interface ProjectSummary {
  id: string;
  name: string;
  source_sha256: string;
}

export interface Attempt {
  id: string;
  candidate_sha256: string;
  rust_source: string;
  approved: boolean;
}

export interface ProjectDetail {
  project: ProjectSummary;
  revision: number;
  phase: Phase;
  attempt: Attempt | null;
  matched: number | null;
  required: number;
  blocker: string | null;
  export_available: boolean;
}

export interface ProposalPacket {
  project_id: string;
  source_sha256: string;
  language: string;
  source: string;
  brief: string;
}

export interface RuntimeRecipe {
  project_id: string;
  name: string;
  recipe_sha256: string;
  toolchain_sha256: string;
  state: RecipeState;
  detail: string;
}

export interface RuntimeState {
  recipes: RuntimeRecipe[];
}

export interface ProposalRequest {
  expected_revision: number;
  source_sha256: string;
  rust_source: string;
}

export interface ComparisonRequest {
  expected_revision: number;
  attempt_id: string;
  candidate_sha256: string;
}

export interface MutationOptions {
  idempotencyKey: string;
  signal?: AbortSignal;
}

export interface DownloadedPacket {
  packet: ProposalPacket;
  bytes: Uint8Array<ArrayBuffer>;
}

/** The request never produced an HTTP response: the server is unreachable or the device is offline. */
export class NetworkError extends Error {
  constructor() {
    super('The workspace server could not be reached.');
    this.name = 'NetworkError';
  }
}

/** SHA-256 needs a secure browsing context; without it nothing can be verified. */
export class DigestUnavailableError extends Error {
  constructor() {
    super('SHA-256 is unavailable in this browser context.');
    this.name = 'DigestUnavailableError';
  }
}

// ---------------------------------------------------------------------------------------------
// Digests and text
// ---------------------------------------------------------------------------------------------

export function isSha256(value: unknown): value is string {
  return typeof value === 'string' && SHA256_PATTERN.test(value);
}

export function isWellFormedText(text: string): boolean {
  return !LONE_SURROGATE.test(text);
}

export async function sha256Hex(data: string | Uint8Array<ArrayBuffer>): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (subtle === undefined) {
    throw new DigestUnavailableError();
  }
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  const digest = new Uint8Array(await subtle.digest('SHA-256', bytes));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export interface LineEndingCounts {
  crlf: number;
  lf: number;
  cr: number;
}

export function countLineEndings(text: string): LineEndingCounts {
  const counts: LineEndingCounts = { crlf: 0, lf: 0, cr: 0 };
  for (const match of text.matchAll(/\r\n|\n|\r/g)) {
    if (match[0] === '\r\n') {
      counts.crlf += 1;
    } else if (match[0] === '\n') {
      counts.lf += 1;
    } else {
      counts.cr += 1;
    }
  }
  return counts;
}

/** The number of bytes `sha256Hex` hashes for this text. */
export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

const INVISIBLE_OR_UNASSIGNED = /^[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Zs}\p{Cn}\p{Co}\p{Cs}]$/u;

/**
 * An unambiguous view of exact text: backslash, CR, LF and tab are written as escapes, and every
 * other control, format, separator, unassigned or surrogate code point as `\u{hex}`. A real line
 * break follows each `\n` marker only so long text stays readable; it is not part of the text.
 */
export function escapeExactText(text: string): string {
  let out = '';
  for (const char of text) {
    if (char === '\\') {
      out += '\\\\';
    } else if (char === '\n') {
      out += '\\n\n';
    } else if (char === '\r') {
      out += '\\r';
    } else if (char === '\t') {
      out += '\\t';
    } else if (char !== ' ' && INVISIBLE_OR_UNASSIGNED.test(char)) {
      out += `\\u{${(char.codePointAt(0) ?? 0).toString(16)}}`;
    } else {
      out += char;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isErrorCode(value: unknown): value is ApiErrorCode {
  return typeof value === 'string' && ERROR_CODES.has(value);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new ValidationError(path, 'an object');
  }
  return value;
}

function text(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ValidationError(path, 'a non-empty string');
  }
  return value;
}

function anyString(value: unknown, path: string): string {
  if (typeof value !== 'string') {
    throw new ValidationError(path, 'a string');
  }
  return value;
}

function digest(value: unknown, path: string): string {
  if (!isSha256(value)) {
    throw new ValidationError(path, '64 lowercase hexadecimal characters');
  }
  return value;
}

function unsigned(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new ValidationError(path, 'a non-negative safe integer');
  }
  return value;
}

function bool(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ValidationError(path, 'a boolean');
  }
  return value;
}

function schemaVersion(object: Record<string, unknown>, path: string): void {
  if (object.schema_version !== 1) {
    throw new ValidationError(`${path}.schema_version`, '1');
  }
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  const found = allowed.find((candidate) => candidate === value);
  if (found === undefined) {
    throw new ValidationError(path, `one of ${allowed.join(', ')}`);
  }
  return found;
}

function decodeSummary(value: unknown, path: string): ProjectSummary {
  const object = record(value, path);
  return {
    id: text(object.id, `${path}.id`),
    name: text(object.name, `${path}.name`),
    source_sha256: digest(object.source_sha256, `${path}.source_sha256`),
  };
}

export function decodeProjectList(value: unknown): ProjectSummary[] {
  const object = record(value, 'projects');
  schemaVersion(object, 'projects');
  if (!Array.isArray(object.projects)) {
    throw new ValidationError('projects.projects', 'an array');
  }
  const seen = new Set<string>();
  return object.projects.map((row: unknown, index: number) => {
    const summary = decodeSummary(row, `projects.projects[${index}]`);
    if (seen.has(summary.id)) {
      throw new ValidationError('projects.projects', 'free of duplicate project ids');
    }
    seen.add(summary.id);
    return summary;
  });
}

function decodeAttempt(value: unknown, path: string): Attempt {
  const object = record(value, path);
  return {
    id: text(object.id, `${path}.id`),
    candidate_sha256: digest(object.candidate_sha256, `${path}.candidate_sha256`),
    rust_source: text(object.rust_source, `${path}.rust_source`),
    approved: bool(object.approved, `${path}.approved`),
  };
}

export function decodeProjectDetail(value: unknown): ProjectDetail {
  const object = record(value, 'project');
  schemaVersion(object, 'project');
  const required = unsigned(object.required, 'project.required');
  if (required === 0) {
    throw new ValidationError('project.required', 'at least 1 registered case');
  }
  let matched: number | null = null;
  if (object.matched !== null) {
    matched = unsigned(object.matched, 'project.matched');
    if (matched > required) {
      throw new ValidationError('project.matched', 'no greater than project.required');
    }
  }
  const phase = enumValue(object.phase, PHASES, 'project.phase');
  const attempt = object.attempt === null ? null : decodeAttempt(object.attempt, 'project.attempt');
  const exportAvailable = bool(object.export_available, 'project.export_available');
  if (
    exportAvailable &&
    (phase !== 'verified' || attempt === null || matched === null || matched !== required)
  ) {
    throw new ValidationError(
      'project.export_available',
      'true only for a verified attempt with every required case matched',
    );
  }
  return {
    project: decodeSummary(object.project, 'project.project'),
    revision: unsigned(object.revision, 'project.revision'),
    phase,
    attempt,
    matched,
    required,
    blocker: object.blocker === null ? null : text(object.blocker, 'project.blocker'),
    export_available: exportAvailable,
  };
}

export function decodePacket(value: unknown): ProposalPacket {
  const object = record(value, 'packet');
  schemaVersion(object, 'packet');
  return {
    project_id: text(object.project_id, 'packet.project_id'),
    source_sha256: digest(object.source_sha256, 'packet.source_sha256'),
    language: text(object.language, 'packet.language'),
    source: anyString(object.source, 'packet.source'),
    brief: anyString(object.brief, 'packet.brief'),
  };
}

export function decodeRuntimeState(value: unknown): RuntimeState {
  const object = record(value, 'runtime');
  schemaVersion(object, 'runtime');
  if (!Array.isArray(object.recipes)) {
    throw new ValidationError('runtime.recipes', 'an array');
  }
  const recipes = object.recipes.map((row: unknown, index: number): RuntimeRecipe => {
    const path = `runtime.recipes[${index}]`;
    const item = record(row, path);
    return {
      project_id: text(item.project_id, `${path}.project_id`),
      name: text(item.name, `${path}.name`),
      recipe_sha256: digest(item.recipe_sha256, `${path}.recipe_sha256`),
      toolchain_sha256: digest(item.toolchain_sha256, `${path}.toolchain_sha256`),
      state: enumValue(item.state, RECIPE_STATES, `${path}.state`),
      detail: anyString(item.detail, `${path}.detail`),
    };
  });
  return { recipes };
}

// ---------------------------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------------------------

export interface SourcePortClient {
  listProjects(signal?: AbortSignal): Promise<ProjectSummary[]>;
  getProject(projectId: string, signal?: AbortSignal): Promise<ProjectDetail>;
  getPacket(projectId: string, signal?: AbortSignal): Promise<DownloadedPacket>;
  submitProposal(
    projectId: string,
    request: ProposalRequest,
    options: MutationOptions,
  ): Promise<ProjectDetail>;
  requestComparison(
    projectId: string,
    request: ComparisonRequest,
    options: MutationOptions,
  ): Promise<ProjectDetail>;
  downloadSource(
    projectId: string,
    attemptId: string,
    signal?: AbortSignal,
  ): Promise<Uint8Array<ArrayBuffer>>;
  getRuntimeState(signal?: AbortSignal): Promise<RuntimeState>;
}

export interface SourcePortClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

function isAbort(error: unknown): boolean {
  return isRecord(error) ? error.name === 'AbortError' : error instanceof Error && error.name === 'AbortError';
}

async function failureFrom(response: Response): Promise<Error> {
  let body: unknown = null;
  try {
    body = JSON.parse(await response.text());
  } catch {
    body = null;
  }
  if (isRecord(body) && isErrorCode(body.error) && typeof body.message === 'string') {
    const code = body.error;
    const message = body.message;
    const currentRevision = body.current_revision;
    if (
      response.status === 409 &&
      code === 'stale_revision' &&
      typeof currentRevision === 'number' &&
      Number.isSafeInteger(currentRevision) &&
      currentRevision >= 0
    ) {
      return new StaleRevisionError(response.status, {
        error: code,
        message,
        current_revision: currentRevision,
      });
    }
    return new ApiError(response.status, { error: code, message });
  }
  return new ApiError(response.status, {
    error: 'internal',
    message: `The request failed with status ${response.status}.`,
  });
}

export function createSourcePortClient(options: SourcePortClientOptions = {}): SourcePortClient {
  const baseUrl = (options.baseUrl ?? BASE_URL).replace(/\/$/, '');
  const fetchImpl: typeof fetch =
    options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));

  async function send(url: string, init: RequestInit): Promise<Response> {
    try {
      return await fetchImpl(url, init);
    } catch (error) {
      if (isAbort(error)) {
        throw error;
      }
      throw new NetworkError();
    }
  }

  async function readJson(url: string, signal?: AbortSignal): Promise<unknown> {
    const response = await send(url, {
      headers: { Accept: 'application/json' },
      signal: signal ?? null,
    });
    if (!response.ok) {
      throw await failureFrom(response);
    }
    try {
      return await response.json();
    } catch {
      throw new ValidationError('response', 'a JSON body');
    }
  }

  function projectUrl(projectId: string): string {
    return `${baseUrl}/convert/projects/${encodeURIComponent(text(projectId, 'projectId'))}`;
  }

  function decodeForProject(projectId: string, body: unknown): ProjectDetail {
    const detail = decodeProjectDetail(body);
    if (detail.project.id !== projectId) {
      throw new ValidationError('project.project.id', `the requested project ${projectId}`);
    }
    return detail;
  }

  async function mutate(
    projectId: string,
    route: string,
    body: Record<string, unknown>,
    mutation: MutationOptions,
  ): Promise<ProjectDetail> {
    if (!IDEMPOTENCY_KEY_PATTERN.test(mutation.idempotencyKey)) {
      throw new ValidationError('idempotencyKey', '1-128 visible ASCII characters');
    }
    const response = await send(`${projectUrl(projectId)}/${route}`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Idempotency-Key': mutation.idempotencyKey,
      },
      body: JSON.stringify(body),
      signal: mutation.signal ?? null,
    });
    if (!response.ok) {
      throw await failureFrom(response);
    }
    let parsed: unknown;
    try {
      parsed = await response.json();
    } catch {
      throw new ValidationError('response', 'a JSON body');
    }
    return decodeForProject(projectId, parsed);
  }

  return {
    async listProjects(signal) {
      return decodeProjectList(await readJson(`${baseUrl}/convert/projects`, signal));
    },

    async getProject(projectId, signal) {
      return decodeForProject(projectId, await readJson(projectUrl(projectId), signal));
    },

    async getPacket(projectId, signal) {
      const response = await send(`${projectUrl(projectId)}/packet`, {
        headers: { Accept: 'application/json' },
        signal: signal ?? null,
      });
      if (!response.ok) {
        throw await failureFrom(response);
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      let parsed: unknown;
      try {
        parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      } catch {
        throw new ValidationError('response', 'a UTF-8 JSON body');
      }
      const packet = decodePacket(parsed);
      if (packet.project_id !== projectId) {
        throw new ValidationError('packet.project_id', `the requested project ${projectId}`);
      }
      return { packet, bytes };
    },

    async submitProposal(projectId, request, mutation) {
      unsigned(request.expected_revision, 'expected_revision');
      digest(request.source_sha256, 'source_sha256');
      if (request.rust_source.length === 0 || !isWellFormedText(request.rust_source)) {
        throw new ValidationError('rust_source', 'non-empty well-formed UTF-8 text');
      }
      return mutate(
        projectId,
        'proposals',
        {
          expected_revision: request.expected_revision,
          source_sha256: request.source_sha256,
          rust_source: request.rust_source,
        },
        mutation,
      );
    },

    async requestComparison(projectId, request, mutation) {
      unsigned(request.expected_revision, 'expected_revision');
      text(request.attempt_id, 'attempt_id');
      digest(request.candidate_sha256, 'candidate_sha256');
      return mutate(
        projectId,
        'compare',
        {
          expected_revision: request.expected_revision,
          attempt_id: request.attempt_id,
          candidate_sha256: request.candidate_sha256,
        },
        mutation,
      );
    },

    async downloadSource(projectId, attemptId, signal) {
      const url = `${projectUrl(projectId)}/attempts/${encodeURIComponent(text(attemptId, 'attemptId'))}/source`;
      const response = await send(url, { signal: signal ?? null });
      if (!response.ok) {
        throw await failureFrom(response);
      }
      const contentType = response.headers.get('Content-Type') ?? '';
      if (!/^text\/plain\b/i.test(contentType)) {
        throw new ValidationError('response.content-type', 'text/plain');
      }
      return new Uint8Array(await response.arrayBuffer());
    },

    async getRuntimeState(signal) {
      return decodeRuntimeState(await readJson(`${baseUrl}/runtime/state`, signal));
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Idempotency and drafts
// ---------------------------------------------------------------------------------------------

/**
 * One key per unfinished intent. A retry after an unknown outcome (network failure, 5xx) must reuse
 * the key so the server can replay the original receipt instead of repeating the action.
 */
export class IntentKeys {
  readonly #create: () => string;
  readonly #keys = new Map<string, string>();

  constructor(create: () => string = defaultKey) {
    this.#create = create;
  }

  keyFor(fingerprint: string): string {
    let key = this.#keys.get(fingerprint);
    if (key === undefined) {
      key = this.#create();
      this.#keys.set(fingerprint, key);
    }
    return key;
  }

  settle(fingerprint: string): void {
    this.#keys.delete(fingerprint);
  }
}

function defaultKey(): string {
  if (typeof globalThis.crypto?.randomUUID !== 'function') {
    throw new DigestUnavailableError();
  }
  return globalThis.crypto.randomUUID();
}

export interface DraftStore {
  get(projectId: string): string | undefined;
  set(projectId: string, draft: string): void;
  discard(projectId: string): void;
}

/** Unsaved proposal text, per project, in memory only. It is never written to web storage. */
export function createDraftStore(): DraftStore {
  const drafts = new Map<string, string>();
  return {
    get: (projectId) => drafts.get(projectId),
    set: (projectId, draft) => {
      drafts.set(projectId, draft);
    },
    discard: (projectId) => {
      drafts.delete(projectId);
    },
  };
}

export const draftStore: DraftStore = createDraftStore();

// ---------------------------------------------------------------------------------------------
// Convert view rules
// ---------------------------------------------------------------------------------------------

export type Integrity = 'checking' | 'ok' | 'mismatch' | 'unavailable';
export type ReviewState = 'none' | 'pending' | 'approved' | 'conflict';
export type PrimaryKind = 'submit' | 'compare' | 'refresh' | 'download';

export interface ConvertViewInput {
  detail: ProjectDetail;
  /** Whether the shown candidate text hashes to the attempt's reported digest. */
  integrity: Integrity;
  /** The registry row for this project, or null while the list is unknown. */
  listed: ProjectSummary | null;
  draft: string;
  isBusy: boolean;
}

export interface ConvertAction {
  label: string;
  disabledReason: string | null;
}

export interface ConvertView {
  review: ReviewState;
  identityConflict: boolean;
  editable: boolean;
  canDownload: boolean;
  primary: ConvertAction & { kind: PrimaryKind };
  submit: ConvertAction & { available: boolean };
}

const EDITABLE_PHASES: ReadonlySet<Phase> = new Set<Phase>([
  'registered',
  'awaiting_review',
  'approved',
  'mismatch',
  'blocked',
]);

function reviewOf(phase: Phase, attempt: Attempt | null): ReviewState {
  if (attempt === null) {
    return 'none';
  }
  switch (phase) {
    case 'awaiting_review':
      return attempt.approved ? 'conflict' : 'pending';
    case 'approved':
    case 'queued':
    case 'comparing':
    case 'mismatch':
    case 'verified':
      return attempt.approved ? 'approved' : 'conflict';
    case 'registered':
    case 'blocked':
      return attempt.approved ? 'approved' : 'pending';
  }
}

function integrityReason(integrity: Integrity): string | null {
  switch (integrity) {
    case 'ok':
      return null;
    case 'checking':
      return 'Checking the candidate SHA-256 digest.';
    case 'mismatch':
      return 'The candidate text does not match its recorded SHA-256 digest.';
    case 'unavailable':
      return 'SHA-256 is unavailable in this browser context, so the candidate cannot be checked.';
  }
}

function submitLabel(phase: Phase): string {
  switch (phase) {
    case 'registered':
      return 'Submit proposal';
    case 'mismatch':
    case 'blocked':
      return 'Submit revised proposal';
    default:
      return 'Replace proposal';
  }
}

/**
 * The full export gate on one project detail: a verified phase, a server-side export flag, an
 * externally approved attempt, and every required case matched. Applied again to a fresh read
 * before bytes are saved, because approval can be revoked while a download is in flight.
 */
export function isExportable(detail: ProjectDetail): boolean {
  return (
    detail.phase === 'verified' &&
    detail.export_available &&
    detail.attempt !== null &&
    detail.attempt.approved &&
    detail.matched !== null &&
    detail.matched === detail.required
  );
}

export function deriveConvertView(input: ConvertViewInput): ConvertView {
  const { detail, integrity, listed, draft, isBusy } = input;
  const { phase, attempt } = detail;
  const review = reviewOf(phase, attempt);
  const identityConflict =
    listed !== null &&
    (listed.id !== detail.project.id ||
      listed.name !== detail.project.name ||
      listed.source_sha256 !== detail.project.source_sha256);
  const editable = EDITABLE_PHASES.has(phase);

  const blockReason = isBusy
    ? 'A request is in progress.'
    : identityConflict
      ? 'The project identity differs between the registry list and its detail; reload before continuing.'
      : null;

  let submitReason = blockReason;
  if (submitReason === null) {
    if (!editable) {
      submitReason = 'This proposal can no longer be replaced.';
    } else if (draft.trim().length === 0) {
      submitReason = 'Paste the Rust proposal text first.';
    } else if (!isWellFormedText(draft)) {
      submitReason = 'The text cannot be encoded as UTF-8 (it contains an unpaired surrogate).';
    } else if (attempt !== null && draft === attempt.rust_source) {
      submitReason = 'Change the text to submit a revised proposal.';
    }
  }
  const submit = {
    label: submitLabel(phase),
    disabledReason: submitReason,
    available: submitReason === null,
  };

  const canDownload = blockReason === null && isExportable(detail) && integrity === 'ok';

  let primary: ConvertView['primary'];
  switch (phase) {
    case 'registered':
    case 'mismatch':
    case 'blocked':
      primary = { kind: 'submit', ...submit };
      break;
    case 'approved':
      primary =
        review === 'approved'
          ? {
            kind: 'compare',
            label: 'Run fixed comparison',
            disabledReason: blockReason ?? integrityReason(integrity),
          }
          : { kind: 'refresh', label: 'Refresh status', disabledReason: blockReason };
      break;
    case 'verified':
      primary = canDownload
        ? { kind: 'download', label: 'Download port.rs', disabledReason: null }
        : { kind: 'refresh', label: 'Refresh status', disabledReason: blockReason };
      break;
    case 'awaiting_review':
    case 'queued':
    case 'comparing':
      primary = { kind: 'refresh', label: 'Refresh status', disabledReason: blockReason };
      break;
  }

  return { review, identityConflict, editable, canDownload, primary, submit };
}

export type Reconciliation = 'accept' | 'ignore' | 'conflict';

const STALE_MESSAGE =
  'The server returned an older revision than one already accepted. Showing no results until it is read again.';

const CONFLICT_MESSAGE =
  'The server returned a different frozen project identity for the same revision. Showing no results until it is read again.';

/**
 * The control revision moves only on a proposal or compare intent. Approval, revocation and
 * verifier evidence arrive on their own, so phase, approval, measurements, blocker and
 * eligibility may change at one revision. What a revision freezes may not: the project, its
 * source digest, and the attempt id, candidate digest and candidate bytes.
 */
function sameFrozenIdentity(a: ProjectDetail, b: ProjectDetail): boolean {
  const x = a.attempt;
  const y = b.attempt;
  const sameAttempt =
    x === null || y === null
      ? x === y
      : x.id === y.id && x.candidate_sha256 === y.candidate_sha256 && x.rust_source === y.rust_source;
  return (
    a.project.id === b.project.id &&
    a.project.source_sha256 === b.project.source_sha256 &&
    sameAttempt
  );
}

/**
 * A late or reordered response never replaces a newer revision of the same project. At an equal
 * revision a fresh response replaces the state unless it contradicts the frozen identity, which is
 * a conflict, never a silent overwrite. The hook drops responses superseded by a later read.
 */
export function reconcileDetail(current: ProjectDetail, incoming: ProjectDetail): Reconciliation {
  if (incoming.project.id !== current.project.id || incoming.revision < current.revision) {
    return 'ignore';
  }
  if (incoming.revision > current.revision) {
    return 'accept';
  }
  return sameFrozenIdentity(current, incoming) ? 'accept' : 'conflict';
}

// ---------------------------------------------------------------------------------------------
// Read-only resources
// ---------------------------------------------------------------------------------------------

export interface ResourceState<T> {
  phase: 'loading' | 'ready' | 'offline' | 'error';
  /** Only non-null in the `ready` phase: failures and offline states never keep a stale result. */
  value: T | null;
  message: string | null;
  isRefreshing: boolean;
}

export interface ReadResource<T> {
  state: ResourceState<T>;
  reload(): void;
  /** Install a mutation result. Returns false when a newer value is already held. */
  replace(value: T): boolean;
}

const OFFLINE_MESSAGE = 'The workspace server cannot be reached. Showing no results until it responds.';

export function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return OFFLINE_MESSAGE;
  }
  if (error instanceof StaleRevisionError) {
    return `The project changed (now at revision ${error.currentRevision}). Review the latest state and try again.`;
  }
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof ValidationError) {
    return `The server response did not match the contract: ${error.message}.`;
  }
  if (error instanceof DigestUnavailableError) {
    return error.message;
  }
  return 'The request failed.';
}

/**
 * Loads a resource with GET semantics only: on mount, on manual reload, and when the browser
 * reports that it is back online. Never sends a command. The caller keys the owning component by
 * the resource identity, so a selection change always starts from a clean state.
 */
export function useReadResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  reconcile?: (current: T, incoming: T) => Reconciliation,
): ReadResource<T> {
  const [state, setState] = useState<ResourceState<T>>({
    phase: 'loading',
    value: null,
    message: null,
    isRefreshing: false,
  });
  const stateRef = useRef(state);
  const anchorRef = useRef<T | null>(null);
  const sequenceRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  const commit = useCallback((next: ResourceState<T>): void => {
    stateRef.current = next;
    setState(next);
  }, []);

  /**
   * Installs a response against the last accepted value, which outlives errors and offline gaps:
   * the shown value is cleared on failure, the identity it froze is not.
   */
  const settle = useCallback(
    (incoming: T): boolean => {
      const anchor = anchorRef.current;
      const verdict =
        anchor !== null && reconcile !== undefined ? reconcile(anchor, incoming) : 'accept';
      if (verdict === 'accept') {
        anchorRef.current = incoming;
        commit({ phase: 'ready', value: incoming, message: null, isRefreshing: false });
        return true;
      }
      const current = stateRef.current;
      if (verdict === 'ignore' && current.value !== null) {
        commit({ ...current, isRefreshing: false });
      } else {
        commit({
          phase: 'error',
          value: null,
          message: verdict === 'conflict' ? CONFLICT_MESSAGE : STALE_MESSAGE,
          isRefreshing: false,
        });
      }
      return false;
    },
    [reconcile, commit],
  );

  const cancel = useCallback((): number => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    sequenceRef.current += 1;
    return sequenceRef.current;
  }, []);

  const run = useCallback((): void => {
    const sequence = cancel();
    const controller = new AbortController();
    controllerRef.current = controller;
    load(controller.signal).then(
      (incoming) => {
        if (sequence !== sequenceRef.current) {
          return;
        }
        settle(incoming);
      },
      (error: unknown) => {
        if (sequence !== sequenceRef.current || controller.signal.aborted) {
          return;
        }
        commit({
          phase: error instanceof NetworkError ? 'offline' : 'error',
          value: null,
          message: describeError(error),
          isRefreshing: false,
        });
      },
    );
  }, [settle, cancel, commit, load]);

  const reload = useCallback((): void => {
    const current = stateRef.current;
    commit(
      current.phase === 'ready'
        ? { ...current, isRefreshing: true }
        : { phase: 'loading', value: null, message: null, isRefreshing: false },
    );
    run();
  }, [commit, run]);

  const replace = useCallback(
    (incoming: T): boolean => {
      cancel();
      return settle(incoming);
    },
    [cancel, settle],
  );

  useEffect(() => {
    run();
    const handleOnline = (): void => {
      reload();
    };
    const handleOffline = (): void => {
      cancel();
      commit({ phase: 'offline', value: null, message: OFFLINE_MESSAGE, isRefreshing: false });
    };
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
      cancel();
    };
  }, [cancel, commit, reload, run]);

  return { state, reload, replace };
}
