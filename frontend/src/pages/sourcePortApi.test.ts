import { describe, expect, it, vi } from 'vitest';
import { ApiError, StaleRevisionError, ValidationError } from '../api';
import {
  countLineEndings,
  escapeExactText,
  utf8ByteLength,
  IntentKeys,
  NetworkError,
  PHASES,
  createDraftStore,
  createSourcePortClient,
  decodePacket,
  decodeProjectDetail,
  decodeProjectList,
  decodeRuntimeState,
  deriveConvertView,
  isWellFormedText,
  sha256Hex,
} from './sourcePortApi';
import type { ConvertView, ConvertViewInput, ProjectDetail } from './sourcePortApi';

const SOURCE_SHA = 'a'.repeat(64);
const CANDIDATE_SHA = 'b'.repeat(64);

function detailBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    project: { id: 'line-count', name: 'Line count', source_sha256: SOURCE_SHA },
    revision: 0,
    phase: 'registered',
    attempt: null,
    matched: null,
    required: 3,
    blocker: null,
    export_available: false,
    ...overrides,
  };
}

function attemptBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'att-1',
    candidate_sha256: CANDIDATE_SHA,
    rust_source: 'fn main() {}\n',
    approved: false,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('sha256Hex', () => {
  it('matches the published SHA-256 vector for abc', async () => {
    await expect(sha256Hex('abc')).resolves.toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    );
  });

  it('hashes exact UTF-8 bytes without line-ending conversion', async () => {
    const lf = await sha256Hex('a\nb');
    const crlf = await sha256Hex('a\r\nb');
    expect(lf).not.toBe(crlf);
    await expect(sha256Hex(new TextEncoder().encode('a\r\nb'))).resolves.toBe(crlf);
  });
});

describe('isWellFormedText', () => {
  it('rejects lone surrogates that would not survive UTF-8', () => {
    expect(isWellFormedText('fn main() {}')).toBe(true);
    expect(isWellFormedText('\u{1F600}')).toBe(true);
    expect(isWellFormedText('a\uD800b')).toBe(false);
    expect(isWellFormedText('a\uDC00b')).toBe(false);
  });
});

describe('exact text helpers', () => {
  it('counts CRLF, LF and lone CR separately', () => {
    expect(countLineEndings('a\r\nb\nc\rd\r\r\n')).toEqual({ crlf: 2, lf: 1, cr: 2 });
  });

  it('escapes backslash, line endings, tabs and invisible characters but not ordinary text', () => {
    expect(escapeExactText('a\\b\r\n\tc \u200b\u0000é\u{1F600}')).toBe(
      'a\\\\b\\r\\n\n\\tc \\u{200b}\\u{0}é\u{1F600}',
    );
  });

  it('keeps CRLF and LF distinguishable', () => {
    expect(escapeExactText('x\r\ny')).not.toBe(escapeExactText('x\ny'));
  });

  it('measures UTF-8 bytes rather than UTF-16 units', () => {
    expect(utf8ByteLength('é\r\n')).toBe(4);
  });
});

describe('decoders', () => {
  it('accepts the contract project list and rejects malformed identities', () => {
    const list = decodeProjectList({
      schema_version: 1,
      projects: [{ id: 'line-count', name: 'Line count', source_sha256: SOURCE_SHA }],
    });
    expect(list).toEqual([{ id: 'line-count', name: 'Line count', source_sha256: SOURCE_SHA }]);
    expect(() =>
      decodeProjectList({
        schema_version: 1,
        projects: [{ id: 'x', name: 'x', source_sha256: SOURCE_SHA.toUpperCase() }],
      }),
    ).toThrow(ValidationError);
    expect(() =>
      decodeProjectList({
        schema_version: 1,
        projects: [{ id: 'x', name: 'x', source_sha256: 'abc' }],
      }),
    ).toThrow(/source_sha256/);
    expect(() => decodeProjectList({ schema_version: 2, projects: [] })).toThrow(
      /schema_version/,
    );
  });

  it('treats an empty registry as zero selectable projects', () => {
    expect(decodeProjectList({ schema_version: 1, projects: [] })).toEqual([]);
  });

  it('rejects duplicate project ids', () => {
    const row = { id: 'x', name: 'x', source_sha256: SOURCE_SHA };
    expect(() => decodeProjectList({ schema_version: 1, projects: [row, row] })).toThrow(
      /duplicate/i,
    );
  });

  it('decodes every contract phase and rejects an unknown one', () => {
    for (const phase of PHASES) {
      expect(decodeProjectDetail(detailBody({ phase })).phase).toBe(phase);
    }
    expect(() => decodeProjectDetail(detailBody({ phase: 'done' }))).toThrow(/phase/);
  });

  it('requires matched to be null or within required', () => {
    expect(decodeProjectDetail(detailBody({ matched: 2 })).matched).toBe(2);
    expect(() => decodeProjectDetail(detailBody({ matched: 4 }))).toThrow(/matched/);
    expect(() => decodeProjectDetail(detailBody({ matched: '2' }))).toThrow(/matched/);
    expect(() => decodeProjectDetail(detailBody({ required: 0 }))).toThrow(/required/);
  });

  it('refuses an export flag that is not backed by a verified, fully matched attempt', () => {
    expect(() => decodeProjectDetail(detailBody({ export_available: true }))).toThrow(
      /export_available/,
    );
    expect(() =>
      decodeProjectDetail(
        detailBody({
          export_available: true,
          phase: 'comparing',
          attempt: attemptBody({ approved: true }),
          matched: 3,
        }),
      ),
    ).toThrow(/export_available/);
    expect(() =>
      decodeProjectDetail(
        detailBody({
          export_available: true,
          phase: 'verified',
          attempt: attemptBody({ approved: true }),
          matched: 2,
        }),
      ),
    ).toThrow(/export_available/);
    expect(
      decodeProjectDetail(
        detailBody({
          export_available: true,
          phase: 'verified',
          attempt: attemptBody({ approved: true }),
          matched: 3,
        }),
      ).export_available,
    ).toBe(true);
  });

  it('rejects an attempt with a malformed candidate digest or approval flag', () => {
    expect(() =>
      decodeProjectDetail(detailBody({ attempt: attemptBody({ candidate_sha256: 'B'.repeat(64) }) })),
    ).toThrow(/candidate_sha256/);
    expect(() =>
      decodeProjectDetail(detailBody({ attempt: attemptBody({ approved: 'yes' }) })),
    ).toThrow(/approved/);
    expect(() =>
      decodeProjectDetail(detailBody({ attempt: attemptBody({ rust_source: undefined }) })),
    ).toThrow(/rust_source/);
  });

  it('decodes the proposal packet and requires a matching identity', () => {
    const packet = {
      schema_version: 1,
      project_id: 'line-count',
      source_sha256: SOURCE_SHA,
      language: 'python',
      source: 'print(1)\n',
      brief: 'Port it.',
    };
    expect(decodePacket(packet).language).toBe('python');
    expect(() => decodePacket({ ...packet, source_sha256: 'x' })).toThrow(/source_sha256/);
    expect(() => decodePacket({ ...packet, brief: 3 })).toThrow(/brief/);
  });

  it('decodes runtime recipes and rejects an unknown state', () => {
    const recipe = {
      project_id: 'line-count',
      name: 'Line count',
      recipe_sha256: 'c'.repeat(64),
      toolchain_sha256: 'd'.repeat(64),
      state: 'configured',
      detail: 'No build recorded.',
    };
    expect(decodeRuntimeState({ schema_version: 1, recipes: [recipe] }).recipes).toHaveLength(1);
    expect(() =>
      decodeRuntimeState({ schema_version: 1, recipes: [{ ...recipe, state: 'ready' }] }),
    ).toThrow(/state/);
    expect(() =>
      decodeRuntimeState({ schema_version: 1, recipes: [{ ...recipe, recipe_sha256: 'x' }] }),
    ).toThrow(/recipe_sha256/);
  });
});

describe('source-port client', () => {
  it('reads with GET only and encodes the project id', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(detailBody()));
    const client = createSourcePortClient({ fetchImpl });
    await expect(client.getProject('line-count')).resolves.toMatchObject({ revision: 0 });
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('/api/v1/convert/projects/line-count');
    expect(init?.method ?? 'GET').toBe('GET');
    expect(init?.body ?? null).toBeNull();

    fetchImpl.mockResolvedValueOnce(json(detailBody({ project: { id: 'a b', name: 'n', source_sha256: SOURCE_SHA } })));
    await client.getProject('a b');
    expect(fetchImpl.mock.calls[1]?.[0]).toBe('/api/v1/convert/projects/a%20b');
  });

  it('rejects a detail whose project id is not the one requested', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json(detailBody()));
    await expect(createSourcePortClient({ fetchImpl }).getProject('other')).rejects.toThrow(
      ValidationError,
    );
  });

  it('submits exactly expected_revision, source_sha256 and rust_source with an idempotency key', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      json(detailBody({ revision: 1, phase: 'awaiting_review', attempt: attemptBody() })),
    );
    const client = createSourcePortClient({ fetchImpl });
    const detail = await client.submitProposal(
      'line-count',
      { expected_revision: 0, source_sha256: SOURCE_SHA, rust_source: 'fn main() {}\n' },
      { idempotencyKey: 'key-1' },
    );
    expect(detail.revision).toBe(1);
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('/api/v1/convert/projects/line-count/proposals');
    expect(init?.method).toBe('POST');
    const headers = new Headers(init?.headers);
    expect(headers.get('Idempotency-Key')).toBe('key-1');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 0,
      source_sha256: SOURCE_SHA,
      rust_source: 'fn main() {}\n',
    });
  });

  it('requests a comparison with exactly expected_revision, attempt_id and candidate_sha256', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      json(
        detailBody({
          revision: 3,
          phase: 'queued',
          attempt: attemptBody({ approved: true }),
        }),
      ),
    );
    const client = createSourcePortClient({ fetchImpl });
    await client.requestComparison(
      'line-count',
      { expected_revision: 2, attempt_id: 'att-1', candidate_sha256: CANDIDATE_SHA },
      { idempotencyKey: 'key-2' },
    );
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('/api/v1/convert/projects/line-count/compare');
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 2,
      attempt_id: 'att-1',
      candidate_sha256: CANDIDATE_SHA,
    });
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBe('key-2');
  });

  it('refuses malformed idempotency keys and identities before sending anything', async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const client = createSourcePortClient({ fetchImpl });
    await expect(
      client.submitProposal(
        'line-count',
        { expected_revision: 0, source_sha256: SOURCE_SHA, rust_source: 'x' },
        { idempotencyKey: 'has space' },
      ),
    ).rejects.toThrow(ValidationError);
    await expect(
      client.requestComparison(
        'line-count',
        { expected_revision: 0, attempt_id: 'a', candidate_sha256: 'nope' },
        { idempotencyKey: 'k' },
      ),
    ).rejects.toThrow(ValidationError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('maps a stale revision conflict to StaleRevisionError with the current revision', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      json({ error: 'stale_revision', message: 'changed', current_revision: 7 }, 409),
    );
    const client = createSourcePortClient({ fetchImpl });
    const error = await client
      .requestComparison(
        'line-count',
        { expected_revision: 2, attempt_id: 'a', candidate_sha256: CANDIDATE_SHA },
        { idempotencyKey: 'k' },
      )
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(StaleRevisionError);
    expect((error as StaleRevisionError).currentRevision).toBe(7);
  });

  it('maps other error bodies to ApiError and unreadable bodies to a generic failure', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({ error: 'not_found', message: 'No such project.' }, 404))
      .mockResolvedValueOnce(new Response('<html>boom</html>', { status: 502 }));
    const client = createSourcePortClient({ fetchImpl });
    await expect(client.getProject('x')).rejects.toMatchObject({
      status: 404,
      code: 'not_found',
      message: 'No such project.',
    });
    const generic = await client.getProject('x').catch((e: unknown) => e);
    expect(generic).toBeInstanceOf(ApiError);
    expect((generic as ApiError).status).toBe(502);
  });

  it('reports a rejected fetch as NetworkError but lets aborts through', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new DOMException('aborted', 'AbortError'));
    const client = createSourcePortClient({ fetchImpl });
    await expect(client.getProject('x')).rejects.toBeInstanceOf(NetworkError);
    await expect(client.getProject('x')).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('returns the exact packet bytes after validating them', async () => {
    const raw = JSON.stringify({
      schema_version: 1,
      project_id: 'line-count',
      source_sha256: SOURCE_SHA,
      language: 'python',
      source: 's',
      brief: 'b',
    });
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response(raw, { status: 200 }));
    const { packet, bytes } = await createSourcePortClient({ fetchImpl }).getPacket('line-count');
    expect(packet.project_id).toBe('line-count');
    expect(new TextDecoder().decode(bytes)).toBe(raw);
    expect(fetchImpl.mock.calls[0]?.[0]).toBe('/api/v1/convert/projects/line-count/packet');
  });

  it('downloads source bytes only from the attempt source route and requires text/plain', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response('fn main() {}\r\n', {
          status: 200,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        }),
      )
      .mockResolvedValueOnce(
        new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
      );
    const client = createSourcePortClient({ fetchImpl });
    const bytes = await client.downloadSource('line-count', 'att 1');
    expect(new TextDecoder().decode(bytes)).toBe('fn main() {}\r\n');
    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      '/api/v1/convert/projects/line-count/attempts/att%201/source',
    );
    await expect(client.downloadSource('line-count', 'att')).rejects.toThrow(ValidationError);
  });

  it('reads runtime state with a plain GET', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ schema_version: 1, recipes: [] }));
    await createSourcePortClient({ fetchImpl }).getRuntimeState();
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    expect(url).toBe('/api/v1/runtime/state');
    expect(init?.method ?? 'GET').toBe('GET');
  });
});

describe('IntentKeys', () => {
  it('reuses a key for an identical unfinished intent and mints a new one after settling', () => {
    let n = 0;
    const keys = new IntentKeys(() => `key-${(n += 1)}`);
    expect(keys.keyFor('submit:1')).toBe('key-1');
    expect(keys.keyFor('submit:1')).toBe('key-1');
    expect(keys.keyFor('submit:2')).toBe('key-2');
    keys.settle('submit:1');
    expect(keys.keyFor('submit:1')).toBe('key-3');
  });
});

describe('draft store', () => {
  it('keeps drafts per project in memory and can discard one', () => {
    const store = createDraftStore();
    store.set('a', 'fn a() {}');
    store.set('b', 'fn b() {}');
    expect(store.get('a')).toBe('fn a() {}');
    expect(store.get('b')).toBe('fn b() {}');
    store.discard('a');
    expect(store.get('a')).toBeUndefined();
    expect(store.get('b')).toBe('fn b() {}');
  });

  it('never touches web storage', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    createDraftStore().set('a', 'secret');
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });
});

function view(
  detailOverrides: Record<string, unknown>,
  input: Partial<Omit<ConvertViewInput, 'detail'>> = {},
): ConvertView {
  const detail: ProjectDetail = decodeProjectDetail(detailBody(detailOverrides));
  return deriveConvertView({
    detail,
    integrity: 'ok',
    listed: detail.project,
    draft: 'fn draft() {}',
    isBusy: false,
    ...input,
  });
}

describe('deriveConvertView', () => {
  it('offers one primary action per phase', () => {
    expect(view({}).primary.kind).toBe('submit');
    expect(view({ phase: 'awaiting_review', attempt: attemptBody() }).primary.kind).toBe('refresh');
    expect(
      view({ phase: 'approved', attempt: attemptBody({ approved: true }) }).primary.kind,
    ).toBe('compare');
    expect(
      view({ phase: 'queued', attempt: attemptBody({ approved: true }) }).primary.kind,
    ).toBe('refresh');
    expect(
      view({ phase: 'comparing', attempt: attemptBody({ approved: true }), matched: 1 }).primary.kind,
    ).toBe('refresh');
    expect(
      view({ phase: 'mismatch', attempt: attemptBody({ approved: true }), matched: 1 }).primary.kind,
    ).toBe('submit');
    expect(view({ phase: 'blocked', attempt: attemptBody(), blocker: 'verifier offline' }).primary.kind).toBe('submit');
    expect(
      view({
        phase: 'verified',
        attempt: attemptBody({ approved: true }),
        matched: 3,
        export_available: true,
      }).primary.kind,
    ).toBe('download');
  });

  it('cannot compare without both the approved phase and an approved attempt', () => {
    const pending = view({ phase: 'awaiting_review', attempt: attemptBody() });
    expect(pending.review).toBe('pending');
    expect(pending.primary.kind).not.toBe('compare');

    // The server says approved, but the attempt is not approved: conflict, never comparable.
    const conflicting = view({ phase: 'approved', attempt: attemptBody({ approved: false }) });
    expect(conflicting.review).toBe('conflict');
    expect(conflicting.primary.kind).not.toBe('compare');

    // An approved attempt in a review-pending phase is also a conflict.
    const reversed = view({ phase: 'awaiting_review', attempt: attemptBody({ approved: true }) });
    expect(reversed.review).toBe('conflict');
    expect(reversed.primary.kind).not.toBe('compare');

    const approved = view({ phase: 'approved', attempt: attemptBody({ approved: true }) });
    expect(approved.review).toBe('approved');
    expect(approved.primary).toMatchObject({ kind: 'compare', disabledReason: null });
  });

  it('blocks comparison and export when the candidate text does not match its digest', () => {
    const approved = view(
      { phase: 'approved', attempt: attemptBody({ approved: true }) },
      { integrity: 'mismatch' },
    );
    expect(approved.primary.disabledReason).toMatch(/digest/i);
    const verified = view(
      { phase: 'verified', attempt: attemptBody({ approved: true }), matched: 3, export_available: true },
      { integrity: 'mismatch' },
    );
    expect(verified.canDownload).toBe(false);
    for (const integrity of ['checking', 'unavailable'] as const) {
      const unknown = view(
        { phase: 'verified', attempt: attemptBody({ approved: true }), matched: 3, export_available: true },
        { integrity },
      );
      expect(unknown.canDownload).toBe(false);
      expect(unknown.primary.kind).not.toBe('download');
    }
  });

  it('blocks everything that changes state when detail and list identities disagree', () => {
    const detail = decodeProjectDetail(
      detailBody({ phase: 'approved', attempt: attemptBody({ approved: true }) }),
    );
    const conflicting = deriveConvertView({
      detail,
      integrity: 'ok',
      listed: { ...detail.project, source_sha256: 'c'.repeat(64) },
      draft: 'fn x() {}',
      isBusy: false,
    });
    expect(conflicting.identityConflict).toBe(true);
    expect(conflicting.primary.disabledReason).toMatch(/identity/i);
    expect(conflicting.submit.disabledReason).toMatch(/identity/i);
  });

  it('exposes download only for a verified, fully matched, exported, intact candidate', () => {
    const base = {
      phase: 'verified',
      attempt: attemptBody({ approved: true }),
      matched: 3,
      export_available: true,
    };
    expect(view(base).canDownload).toBe(true);
    for (const phase of PHASES.filter((p) => p !== 'verified')) {
      expect(view({ ...base, phase, export_available: false }).canDownload).toBe(false);
    }
    expect(view({ ...base, export_available: false }).canDownload).toBe(false);
    expect(view({ ...base, matched: 2, export_available: false }).canDownload).toBe(false);
    expect(view({ ...base, attempt: attemptBody({ approved: false }) }).canDownload).toBe(false);
    expect(view(base, { isBusy: true }).canDownload).toBe(false);
  });

  it('keeps the editor read-only while a build is queued, running or already verified', () => {
    const approved = attemptBody({ approved: true });
    expect(view({}).editable).toBe(true);
    expect(view({ phase: 'awaiting_review', attempt: attemptBody() }).editable).toBe(true);
    expect(view({ phase: 'approved', attempt: approved }).editable).toBe(true);
    expect(view({ phase: 'mismatch', attempt: approved, matched: 1 }).editable).toBe(true);
    expect(view({ phase: 'blocked', attempt: approved }).editable).toBe(true);
    expect(view({ phase: 'queued', attempt: approved }).editable).toBe(false);
    expect(view({ phase: 'comparing', attempt: approved, matched: 0 }).editable).toBe(false);
    expect(
      view({ phase: 'verified', attempt: approved, matched: 3, export_available: true }).editable,
    ).toBe(false);
  });

  it('requires a non-empty, well-formed draft that differs from a failed attempt', () => {
    expect(view({}, { draft: '' }).submit.disabledReason).toMatch(/rust/i);
    expect(view({}, { draft: '   \n' }).submit.disabledReason).toMatch(/rust/i);
    expect(view({}, { draft: 'a\uD800' }).submit.disabledReason).toMatch(/utf-8/i);
    expect(view({}, { draft: 'fn a() {}' }).submit.disabledReason).toBeNull();
    const text = 'fn main() {}\n';
    const failed = view(
      { phase: 'mismatch', attempt: attemptBody({ approved: true, rust_source: text }), matched: 1 },
      { draft: text },
    );
    expect(failed.primary.disabledReason).toMatch(/change/i);
    const revised = view(
      { phase: 'mismatch', attempt: attemptBody({ approved: true, rust_source: text }), matched: 1 },
      { draft: 'fn fixed() {}' },
    );
    expect(revised.primary.disabledReason).toBeNull();
  });

  it('disables every action while a request is in flight', () => {
    expect(view({}, { isBusy: true }).primary.disabledReason).toMatch(/progress/i);
    expect(
      view({ phase: 'approved', attempt: attemptBody({ approved: true }) }, { isBusy: true }).primary
        .disabledReason,
    ).toMatch(/progress/i);
  });

  it('offers replacement as a secondary action only when a pending attempt differs from the draft', () => {
    const pending = view({ phase: 'awaiting_review', attempt: attemptBody() }, { draft: 'fn other() {}' });
    expect(pending.submit.available).toBe(true);
    const same = view(
      { phase: 'awaiting_review', attempt: attemptBody() },
      { draft: 'fn main() {}\n' },
    );
    expect(same.submit.available).toBe(false);
  });
});
