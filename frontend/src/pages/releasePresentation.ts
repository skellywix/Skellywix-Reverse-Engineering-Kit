import type { JsonValue } from '../types';

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const VERSION_PATTERN = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
const PRIVATE_PATH_PATTERN = /(?:[A-Za-z]:[\\/]|\\\\|\/(?:Users|home|tmp|var|etc|opt|srv|mnt|root)\/)[^\s,;]*/gi;
const SAFE_RECEIPT_FILES = new Set([
  'inspect_file.py',
  'rea-skill.md',
  'rea-package.json',
  'rea-lock.json',
  'rea-source.tgz',
]);

export interface ReleaseFilePin {
  name: string;
  sha256: string;
}

export interface EngineReleaseReceipt {
  digest: string;
  kitCommit: string | null;
  reaVersion: string | null;
  reaCommit: string | null;
  sourceSha256: string | null;
  imageIdentity: string | null;
  probeReceipt: string | null;
  files: readonly ReleaseFilePin[];
}

export interface WorkerCatalogEntry {
  role: string;
  skill: string;
  scope: string;
}

export interface EngineReleasePresentation {
  receipts: readonly EngineReleaseReceipt[];
  catalog: readonly WorkerCatalogEntry[];
  recordedReceiptCount: number;
  recordedCatalogCount: number;
}

export type WorkspaceIdentity =
  | { kind: 'absent' }
  | { kind: 'recorded'; digest: string }
  | { kind: 'unavailable' };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function digest(value: unknown): string | null {
  return typeof value === 'string' && SHA256_PATTERN.test(value) ? value : null;
}

function commit(value: unknown): string | null {
  return typeof value === 'string' && COMMIT_PATTERN.test(value) ? value : null;
}

function hasControlCharacter(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 32 || code === 127) {
      return true;
    }
  }
  return false;
}

function safeText(value: unknown, maximumLength = 240): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const text = value.trim();
  if (
    text.length === 0 ||
    text.length > maximumLength ||
    hasControlCharacter(text) ||
    PRIVATE_PATH_PATTERN.test(text)
  ) {
    PRIVATE_PATH_PATTERN.lastIndex = 0;
    return null;
  }
  PRIVATE_PATH_PATTERN.lastIndex = 0;
  return text;
}

function receiptFiles(value: unknown): readonly ReleaseFilePin[] {
  if (!isRecord(value)) {
    return [];
  }
  return Object.entries(value)
    .filter(([name, valueDigest]) => SAFE_RECEIPT_FILES.has(name) && digest(valueDigest) !== null)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, valueDigest]) => ({ name, sha256: valueDigest as string }));
}

function recordedImage(info: Record<string, unknown>): string | null {
  const direct = digest(info.image_sha256) ?? digest(info.image_id);
  if (direct !== null) {
    return direct;
  }
  return isRecord(info.image) ? digest(info.image.sha256) ?? digest(info.image.id) : null;
}

function recordedProbe(info: Record<string, unknown>): string | null {
  const candidate = isRecord(info.probe_receipt)
    ? info.probe_receipt
    : isRecord(info.probe)
      ? info.probe
      : null;
  if (candidate === null) {
    return digest(info.probe_receipt_sha256) ?? digest(info.probe_id);
  }
  const identity = digest(candidate.probe_id) ?? digest(candidate.sha256);
  const result = candidate.result === 'succeeded' || candidate.result === 'failed' ? candidate.result : null;
  if (identity !== null && result !== null) {
    return `${result} · ${identity}`;
  }
  return identity ?? result;
}

function releaseReceipt(value: unknown): EngineReleaseReceipt | null {
  if (!isRecord(value)) {
    return null;
  }
  const receiptDigest = digest(value.digest);
  if (receiptDigest === null || !isRecord(value.info)) {
    return null;
  }
  const info = value.info;
  const version = safeText(info.rea, 80);
  return {
    digest: receiptDigest,
    kitCommit: commit(info.kit),
    reaVersion: version !== null && VERSION_PATTERN.test(version) ? version : null,
    reaCommit: commit(info.rea_commit),
    sourceSha256: digest(info.rea_source_sha256),
    imageIdentity: recordedImage(info),
    probeReceipt: recordedProbe(info),
    files: receiptFiles(info.files),
  };
}

function catalogEntry(value: unknown): WorkerCatalogEntry | null {
  if (!isRecord(value)) {
    return null;
  }
  const role = safeText(value.role, 100);
  const skill = safeText(value.skill, 160);
  const scope = safeText(value.scope, 320);
  return role === null || skill === null || scope === null ? null : { role, skill, scope };
}

export function parseEngineReleasePresentation(value: unknown): EngineReleasePresentation {
  if (!isRecord(value)) {
    return { receipts: [], catalog: [], recordedReceiptCount: 0, recordedCatalogCount: 0 };
  }
  const releases = isRecord(value.releases) ? value.releases : null;
  const rawReceipts = releases !== null && Array.isArray(releases.items) ? releases.items : [];
  const rawCatalog = Array.isArray(value.catalog) ? value.catalog : [];
  const seen = new Set<string>();
  const receipts = rawReceipts.flatMap((item) => {
    const parsed = releaseReceipt(item);
    if (parsed === null || seen.has(parsed.digest)) {
      return [];
    }
    seen.add(parsed.digest);
    return [parsed];
  });
  return {
    receipts,
    catalog: rawCatalog.flatMap((item) => {
      const parsed = catalogEntry(item);
      return parsed === null ? [] : [parsed];
    }),
    recordedReceiptCount: rawReceipts.length,
    recordedCatalogCount: rawCatalog.length,
  };
}

export function workspaceIdentity(value: JsonValue): WorkspaceIdentity {
  if (value === null) {
    return { kind: 'absent' };
  }
  const direct = digest(value);
  if (direct !== null) {
    return { kind: 'recorded', digest: direct };
  }
  if (isRecord(value)) {
    const nested = digest(value.target_id) ?? digest(value.digest) ?? digest(value.id);
    if (nested !== null) {
      return { kind: 'recorded', digest: nested };
    }
  }
  return { kind: 'unavailable' };
}

export function redactPrivatePaths(message: string): string {
  PRIVATE_PATH_PATTERN.lastIndex = 0;
  const redacted = message.replace(PRIVATE_PATH_PATTERN, '[private path]');
  PRIVATE_PATH_PATTERN.lastIndex = 0;
  return redacted;
}
