import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { UserEvent } from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import Convert from './Convert';
import { createSourcePortClient, draftStore, sha256Hex } from './sourcePortApi';

const SOURCE_SHA = 'a'.repeat(64);
const OTHER_SOURCE_SHA = 'e'.repeat(64);
const SOURCE_TEXT = 'fn main() {\n    println!("héllo → 😀");\n}\n';
let CANDIDATE = '';

beforeAll(async () => {
  CANDIDATE = await sha256Hex(SOURCE_TEXT);
});

// ------------------------------------------------------------------------------------------
// Boundary fixtures: a scripted server behind fetch. No live traffic and no engines.
// ------------------------------------------------------------------------------------------

interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

type Handler = (call: Call) => Response | Promise<Response>;

class FakeServer {
  readonly calls: Call[] = [];
  readonly unexpected: string[] = [];
  readonly #routes = new Map<string, Handler>();

  on(route: string, handler: Handler): void {
    this.#routes.set(route, handler);
  }

  reply(route: string, body: unknown, status = 200): void {
    this.on(route, () => json(body, status));
  }

  count(route: string): number {
    return this.calls.filter((call) => `${call.method} ${call.path}` === route).length;
  }

  mutations(): Call[] {
    return this.calls.filter((call) => call.method !== 'GET');
  }

  readonly fetch: typeof fetch = async (input, init) => {
    const method = init?.method ?? 'GET';
    const path = String(input);
    const call: Call = {
      method,
      path,
      headers: new Headers(init?.headers),
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
    };
    this.calls.push(call);
    const handler = this.#routes.get(`${method} ${path}`);
    if (handler === undefined) {
      this.unexpected.push(`${method} ${path}`);
      return json({ error: 'not_found', message: 'Unexpected request.' }, 404);
    }
    return handler(call);
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function summary(id = 'line-count', name = 'Line count', sha = SOURCE_SHA) {
  return { id, name, source_sha256: sha };
}

function attempt(overrides: Record<string, unknown> = {}) {
  return {
    id: 'att-1',
    candidate_sha256: CANDIDATE,
    rust_source: SOURCE_TEXT,
    approved: false,
    ...overrides,
  };
}

function detail(overrides: Record<string, unknown> = {}, project = summary()) {
  return {
    schema_version: 1,
    project,
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

const LIST = 'GET /api/v1/convert/projects';
const DETAIL = 'GET /api/v1/convert/projects/line-count';
const PROPOSALS = 'POST /api/v1/convert/projects/line-count/proposals';
const COMPARE = 'POST /api/v1/convert/projects/line-count/compare';
const PACKET = 'GET /api/v1/convert/projects/line-count/packet';
const SOURCE = 'GET /api/v1/convert/projects/line-count/attempts/att-1/source';

function serverWith(detailBody: unknown = detail()): FakeServer {
  const server = new FakeServer();
  server.reply(LIST, { schema_version: 1, projects: [summary()] });
  server.reply(DETAIL, detailBody);
  return server;
}

function LocationProbe(): React.JSX.Element {
  return <span data-testid="current-route">{useLocation().pathname}</span>;
}

function renderConvert(server: FakeServer, path = '/convert/projects/line-count'): void {
  const client = createSourcePortClient({ fetchImpl: server.fetch });
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/convert" element={<Convert client={client} />} />
        <Route path="/convert/projects/:id" element={<Convert client={client} />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

interface Saved {
  name: string;
  bytes: Uint8Array<ArrayBuffer>;
}

let saved: Saved[] = [];
let lastBlob: Blob | null = null;
const pendingReads: Promise<void>[] = [];

beforeEach(() => {
  draftStore.discard('line-count');
  draftStore.discard('other');
  saved = [];
  lastBlob = null;
  pendingReads.length = 0;
  URL.createObjectURL = vi.fn((blob: Blob) => {
    lastBlob = blob;
    return 'blob:fixture';
  });
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(
    this: HTMLAnchorElement,
  ) {
    const blob = lastBlob;
    if (blob === null) {
      throw new Error('anchor clicked without a blob');
    }
    const name = this.download;
    pendingReads.push(
      readBlob(blob).then((bytes) => {
        saved.push({ name, bytes });
      }),
    );
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function readBlob(blob: Blob): Promise<Uint8Array<ArrayBuffer>> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result === null || typeof reader.result === 'string') {
        reject(new Error('The saved blob could not be read as bytes.'));
        return;
      }
      resolve(new Uint8Array(reader.result));
    };
    reader.onerror = () => {
      reject(new Error('The saved blob could not be read.'));
    };
    reader.readAsArrayBuffer(blob);
  });
}

async function settleSaves(): Promise<void> {
  await act(async () => {
    await Promise.all(pendingReads);
  });
}

function box(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: /Rust proposal/ });
}

function expectDisabled(button: HTMLElement): void {
  expect(button).toHaveAttribute('aria-disabled', 'true');
}

function expectEnabled(button: HTMLElement): void {
  expect(button).not.toHaveAttribute('aria-disabled', 'true');
}

async function pasteDraft(user: UserEvent, value: string): Promise<void> {
  await user.click(box());
  await user.paste(value);
}

function expectNoUnexpectedOrExternalTraffic(server: FakeServer): void {
  expect(server.unexpected).toEqual([]);
  for (const call of server.calls) {
    expect(call.path).toMatch(/^\/api\/v1\/(convert|runtime)\//);
  }
}

// ------------------------------------------------------------------------------------------

describe('Convert: registry and selection', () => {
  it('lists registered projects and sends nothing but reads on mount', async () => {
    const server = serverWith();
    renderConvert(server, '/convert');
    expect(await screen.findByRole('link', { name: /Line count/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Convert' })).toBeInTheDocument();
    expect(server.calls.map((call) => `${call.method} ${call.path}`)).toEqual([LIST]);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expectNoUnexpectedOrExternalTraffic(server);
  });

  it('shows an empty registry as zero selectable projects', async () => {
    const server = new FakeServer();
    server.reply(LIST, { schema_version: 1, projects: [] });
    renderConvert(server, '/convert');
    expect(await screen.findByText('No source projects are registered.')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('selects a project with client-side navigation and loads only that project', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    renderConvert(server, '/convert');
    await user.click(await screen.findByRole('link', { name: /Line count/ }));
    expect(screen.getByTestId('current-route')).toHaveTextContent(
      '/convert/projects/line-count',
    );
    expect(await screen.findByRole('heading', { level: 2, name: 'Line count' })).toBeInTheDocument();
    expect(server.count(DETAIL)).toBe(1);
    expect(server.mutations()).toEqual([]);
  });

  it('never lets a late response for the previous selection overwrite the new one', async () => {
    const user = userEvent.setup();
    const lateA = deferred<Response>();
    const server = new FakeServer();
    server.reply(LIST, {
      schema_version: 1,
      projects: [summary(), summary('other', 'Other port', OTHER_SOURCE_SHA)],
    });
    server.on(DETAIL, () => lateA.promise);
    server.reply(
      'GET /api/v1/convert/projects/other',
      detail({ revision: 4, phase: 'blocked', blocker: 'Verifier offline.' }, summary('other', 'Other port', OTHER_SOURCE_SHA)),
    );
    renderConvert(server);
    await user.click(await screen.findByRole('link', { name: /Other port/ }));
    expect(await screen.findByText('Verifier offline.')).toBeInTheDocument();

    await act(async () => {
      lateA.resolve(json(detail({ revision: 9, phase: 'verified', matched: 3 })));
      await lateA.promise;
    });
    expect(screen.getByRole('heading', { level: 2, name: 'Other port' })).toBeInTheDocument();
    expect(screen.getByText('Verifier offline.')).toBeInTheDocument();
    expect(screen.queryByText(/Verified against/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
  });

  it('keeps a newer revision when an older refresh arrives late', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByRole('button', { name: 'Refresh status' });

    const lateRefresh = deferred<Response>();
    server.on(DETAIL, () => lateRefresh.promise);
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));

    server.reply(
      PROPOSALS,
      detail({
        revision: 2,
        phase: 'awaiting_review',
        attempt: attempt({ id: 'att-2', rust_source: 'fn two() {}\n', candidate_sha256: await sha256Hex('fn two() {}\n') }),
      }),
    );
    await user.clear(box());
    await user.click(box());
    await user.paste('fn two() {}\n');
    await user.click(screen.getByRole('button', { name: 'Replace proposal' }));
    expect(await screen.findByText(/attempt att-2 at revision 2/)).toBeInTheDocument();

    await act(async () => {
      lateRefresh.resolve(json(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() })));
      await lateRefresh.promise;
    });
    expect(screen.getByText(/att-2/)).toBeInTheDocument();
    expect(screen.queryByText(/att-1/)).not.toBeInTheDocument();
  });

  it('accepts an identical same-revision refresh', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByText(/Review pending/);
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText(/Review pending/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('surfaces a conflict and hides results when the same revision returns a different attempt', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByText(/Review pending/);

    server.on(DETAIL, () =>
      json(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt({ id: 'att-9', approved: true }) })),
    );
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/different frozen project identity for the same revision/);
    expect(screen.queryByText(/Review pending/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Approved outside this page/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run fixed comparison' })).not.toBeInTheDocument();
  });

  it.each([
    ['candidate bytes', { rust_source: 'fn other() {}\n' }],
    ['candidate digest', { candidate_sha256: 'f'.repeat(64) }],
    ['attempt id', { id: 'att-9' }],
  ])('rejects a changed %s at an unchanged revision and keeps export gated', async (_label, change) => {
    const user = userEvent.setup();
    const verified = detail({
      revision: 3,
      phase: 'verified',
      attempt: attempt({ approved: true }),
      matched: 3,
      export_available: true,
    });
    const server = serverWith(verified);
    renderConvert(server);
    await screen.findByRole('button', { name: 'Download port.rs' });

    server.on(DETAIL, () => json({ ...verified, attempt: attempt({ approved: true, ...change }) }));
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/different frozen project identity/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByText(/3 of 3/)).not.toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });

  it('follows pending to approved at an unchanged command revision', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 2, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByText(/Review pending/);
    expect(screen.queryByRole('button', { name: 'Run fixed comparison' })).not.toBeInTheDocument();

    server.on(DETAIL, () =>
      json(detail({ revision: 2, phase: 'approved', attempt: attempt({ approved: true }) })),
    );
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    expectEnabled(await screen.findByRole('button', { name: 'Run fixed comparison' }));
    expect(screen.queryByText(/Review pending/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });

  it('follows queued to verified to revoked at an unchanged command revision', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 3, phase: 'queued', attempt: attempt({ approved: true }) }));
    renderConvert(server);
    await screen.findByText(/Approved outside this page/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();

    server.on(DETAIL, () =>
      json(
        detail({
          revision: 3,
          phase: 'verified',
          attempt: attempt({ approved: true }),
          matched: 3,
          export_available: true,
        }),
      ),
    );
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    expectEnabled(await screen.findByRole('button', { name: 'Download port.rs' }));
    expect(screen.getByText(/3 of 3 required cases matched/)).toBeInTheDocument();

    server.on(DETAIL, () =>
      json(
        detail({
          revision: 3,
          phase: 'verified',
          attempt: attempt({ approved: false }),
          matched: 3,
          export_available: true,
        }),
      ),
    );
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByText(/conflicts with the project phase/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });

  it('drops an older verified read that arrives after a fresh revocation', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 3, phase: 'queued', attempt: attempt({ approved: true }) }));
    renderConvert(server);
    await screen.findByText(/Approved outside this page/);

    const late = deferred<Response>();
    server.on(DETAIL, () => late.promise);
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    server.on(DETAIL, () =>
      json(detail({ revision: 3, phase: 'awaiting_review', attempt: attempt({ approved: false }) })),
    );
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByText(/Review pending/);

    await act(async () => {
      late.resolve(
        json(
          detail({
            revision: 3,
            phase: 'verified',
            attempt: attempt({ approved: true }),
            matched: 3,
            export_available: true,
          }),
        ),
      );
      await late.promise;
    });
    expect(screen.getByText(/Review pending/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
  });
});

describe('Convert: proposal submission', () => {
  it('submits the exact bytes with source identity, expected revision and an idempotency key', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    server.reply(PROPOSALS, detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    expectDisabled(screen.getByRole('button', { name: 'Submit proposal' }));

    await pasteDraft(user, SOURCE_TEXT);
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    await screen.findByText(/Proposal frozen as attempt att-1 at revision 1/);

    const [post] = server.mutations();
    expect(server.mutations()).toHaveLength(1);
    expect(post?.body).toEqual({
      expected_revision: 0,
      source_sha256: SOURCE_SHA,
      rust_source: SOURCE_TEXT,
    });
    expect(post?.headers.get('Idempotency-Key')).toMatch(/^[\x21-\x7E]{1,128}$/);
    expect(screen.getByText(/Review pending/)).toBeInTheDocument();
    expect(screen.getByText(CANDIDATE)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run fixed comparison' })).not.toBeInTheDocument();
    expect(draftStore.get('line-count')).toBeUndefined();
  });

  it('reuses the idempotency key when the same submission is retried after an unknown outcome', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    let attempts = 0;
    server.on(PROPOSALS, () => {
      attempts += 1;
      if (attempts === 1) {
        throw new TypeError('Failed to fetch');
      }
      return json(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    });
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, SOURCE_TEXT);
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot be reached/i);
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    await screen.findByText(/Proposal frozen as attempt att-1/);

    const [first, second] = server.mutations();
    expect(first?.headers.get('Idempotency-Key')).toBe(second?.headers.get('Idempotency-Key'));
    expect(first?.body).toEqual(second?.body);
  });

  it('does not double-submit while a request is in flight', async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const server = serverWith();
    server.on(PROPOSALS, () => pending.promise);
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, SOURCE_TEXT);
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    expectDisabled(screen.getByRole('button', { name: 'Submit proposal' }));
    await user.keyboard('{Enter}');
    expect(server.mutations()).toHaveLength(1);
    await act(async () => {
      pending.resolve(json(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() })));
      await pending.promise;
    });
  });

  it('flags a server response whose attempt is not the submitted candidate', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    server.reply(
      PROPOSALS,
      detail({
        revision: 1,
        phase: 'awaiting_review',
        attempt: attempt({ rust_source: 'fn other() {}\n', candidate_sha256: CANDIDATE }),
      }),
    );
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, SOURCE_TEXT);
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/does not match what was submitted/i);
  });

  it('reports a stale revision, re-reads the project and does not resubmit', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    server.reply(PROPOSALS, { error: 'stale_revision', message: 'Moved on.', current_revision: 7 }, 409);
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, SOURCE_TEXT);
    server.reply(DETAIL, detail({ revision: 7, phase: 'blocked', blocker: 'Verifier unavailable.' }));
    await user.click(screen.getByRole('button', { name: 'Submit proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/now at revision 7/);
    expect(await screen.findByText('Verifier unavailable.')).toBeInTheDocument();
    expect(server.mutations()).toHaveLength(1);
    expect(box()).toHaveValue(SOURCE_TEXT);
  });

  it('keeps unsaved drafts per project in memory and never in web storage', async () => {
    const user = userEvent.setup();
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const server = new FakeServer();
    server.reply(LIST, {
      schema_version: 1,
      projects: [summary(), summary('other', 'Other port', OTHER_SOURCE_SHA)],
    });
    server.reply(DETAIL, detail());
    server.reply('GET /api/v1/convert/projects/other', detail({}, summary('other', 'Other port', OTHER_SOURCE_SHA)));
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, 'fn alpha() {}');
    await user.click(screen.getByRole('link', { name: /Other port/ }));
    await screen.findByRole('heading', { level: 2, name: 'Other port' });
    expect(box()).toHaveValue('');
    await pasteDraft(user, 'fn beta() {}');
    await user.click(screen.getByRole('link', { name: /Line count/ }));
    await screen.findByRole('heading', { level: 2, name: 'Line count' });
    expect(box()).toHaveValue('fn alpha() {}');
    expect(setItem).not.toHaveBeenCalled();
  });

  it('shows the draft identity for the exact text and does not send a proposal anywhere on typing', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await pasteDraft(user, SOURCE_TEXT);
    expect(await screen.findByText(CANDIDATE)).toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });
});

describe('Convert: review and comparison', () => {
  it('shows review as pending and offers no comparison before an external approval', async () => {
    const server = serverWith(detail({ revision: 1, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    expect(await screen.findByText(/Review pending/)).toBeInTheDocument();
    expect(screen.getByText(/an operator must approve this version outside the workspace/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /comparison/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Refresh status' })).toHaveLength(1);
  });

  it('refuses to offer comparison when the phase says approved but the attempt is not', async () => {
    const server = serverWith(detail({ revision: 2, phase: 'approved', attempt: attempt({ approved: false }) }));
    renderConvert(server);
    expect(await screen.findByText(/conflicts with the project phase/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run fixed comparison' })).not.toBeInTheDocument();
  });

  it('requests the fixed comparison for exactly the approved candidate and revision', async () => {
    const user = userEvent.setup();
    const server = serverWith(detail({ revision: 2, phase: 'approved', attempt: attempt({ approved: true }) }));
    server.reply(
      COMPARE,
      detail({ revision: 3, phase: 'queued', attempt: attempt({ approved: true }) }),
    );
    renderConvert(server);
    expect(await screen.findByText(/Approved outside this page/)).toBeInTheDocument();
    const compare = await screen.findByRole('button', { name: 'Run fixed comparison' });
    await waitFor(() => expectEnabled(compare));
    await user.click(compare);
    await screen.findByText(/Comparison requested at revision 3/);

    expect(server.mutations()).toHaveLength(1);
    expect(server.mutations()[0]?.body).toEqual({
      expected_revision: 2,
      attempt_id: 'att-1',
      candidate_sha256: CANDIDATE,
    });
    expect(server.mutations()[0]?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(screen.getByText(/Comparison queued/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run fixed comparison' })).not.toBeInTheDocument();
  });

  it('blocks comparison when the shown candidate does not match its digest', async () => {
    const server = serverWith(
      detail({
        revision: 2,
        phase: 'approved',
        attempt: attempt({ approved: true, candidate_sha256: 'f'.repeat(64) }),
      }),
    );
    renderConvert(server);
    await screen.findByText(/does not match its recorded SHA-256 digest/);
    expectDisabled(screen.getByRole('button', { name: 'Run fixed comparison' }));
  });

  it('blocks all state changes when the list and detail disagree about the source identity', async () => {
    const server = serverWith(detail({ revision: 2, phase: 'approved', attempt: attempt({ approved: true }) }, summary('line-count', 'Line count', OTHER_SOURCE_SHA)));
    renderConvert(server);
    await screen.findByText(/identity differs/i);
    await waitFor(() => expectDisabled(screen.getByRole('button', { name: 'Run fixed comparison' })));
  });
});

describe('Convert: measured outcomes and failed checks', () => {
  it('reports honest counts for a mismatch and renders a blocker as plain text', async () => {
    const server = serverWith(
      detail({
        revision: 5,
        phase: 'mismatch',
        attempt: attempt({ approved: true }),
        matched: 2,
        blocker: '<img src=x onerror=alert(1)> case 3 differs',
      }),
    );
    renderConvert(server);
    expect(await screen.findByText(/2 of 3 required cases matched/)).toBeInTheDocument();
    expect(screen.getByText(/Mismatch/)).toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror=alert(1)> case 3 differs')).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(screen.queryByText(/Verified against/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expectDisabled(screen.getByRole('button', { name: 'Submit revised proposal' }));
  });

  it('says so when nothing has been measured and never implies success', async () => {
    const server = serverWith(
      detail({ revision: 4, phase: 'comparing', attempt: attempt({ approved: true }), matched: null }),
    );
    renderConvert(server);
    expect(await screen.findByText(/No measurements recorded/)).toBeInTheDocument();
    expect(screen.getByText(/3 cases are required/)).toBeInTheDocument();
    expect(screen.queryByText(/Verified against/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeInTheDocument();
  });

  it('shows a blocked state explicitly and lets the operator submit a revised proposal', async () => {
    const user = userEvent.setup();
    const server = serverWith(
      detail({ revision: 4, phase: 'blocked', attempt: attempt({ approved: true }), blocker: 'Verifier unavailable.' }),
    );
    renderConvert(server);
    expect(await screen.findByText('Verifier unavailable.')).toBeInTheDocument();
    expect(screen.getByText(/^Blocked/)).toBeInTheDocument();
    expectDisabled(screen.getByRole('button', { name: 'Submit revised proposal' }));
    await user.click(box());
    await user.paste('// revised\n');
    expectEnabled(screen.getByRole('button', { name: 'Submit revised proposal' }));
  });

  it('rejects a response that claims an export without a verified, fully matched attempt', async () => {
    const server = serverWith(
      detail({
        revision: 5,
        phase: 'comparing',
        attempt: attempt({ approved: true }),
        matched: 3,
        export_available: true,
      }),
    );
    renderConvert(server);
    expect(await screen.findByRole('alert')).toHaveTextContent(/did not match the contract/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByText(/3 of 3/)).not.toBeInTheDocument();
  });
});

describe('Convert: offline and reconnect', () => {
  it('hides stale successful results when the server becomes unreachable and re-reads on reconnect', async () => {
    const server = serverWith(
      detail({
        revision: 6,
        phase: 'verified',
        attempt: attempt({ approved: true }),
        matched: 3,
        export_available: true,
      }),
    );
    renderConvert(server);
    expectEnabled(await screen.findByRole('button', { name: 'Download port.rs' }));
    expect(screen.getByText(/3 of 3 required cases matched/)).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    expect(await screen.findByText(/cannot be reached/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByText(/3 of 3/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Verified/)).not.toBeInTheDocument();

    const before = server.calls.length;
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expect(await screen.findByRole('button', { name: 'Download port.rs' })).toBeInTheDocument();
    expect(server.calls.slice(before).every((call) => call.method === 'GET')).toBe(true);
    expect(server.mutations()).toEqual([]);
  });

  it('starts offline with no results, keeps the draft editable and sends no command', async () => {
    const user = userEvent.setup();
    const server = new FakeServer();
    server.on(LIST, () => {
      throw new TypeError('Failed to fetch');
    });
    server.on(DETAIL, () => {
      throw new TypeError('Failed to fetch');
    });
    renderConvert(server);
    expect(await screen.findAllByText(/cannot be reached/i)).not.toHaveLength(0);
    await pasteDraft(user, 'fn offline() {}');
    expectEnabled(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.queryByRole('button', { name: /Submit/ })).not.toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });
});

describe('Convert: guarded downloads', () => {
  function verified(overrides: Record<string, unknown> = {}): unknown {
    return detail({
      revision: 6,
      phase: 'verified',
      attempt: attempt({ approved: true }),
      matched: 3,
      export_available: true,
      ...overrides,
    });
  }

  it('downloads the exact checked source bytes of a verified current candidate', async () => {
    const user = userEvent.setup();
    const server = serverWith(verified());
    server.on(SOURCE, () =>
      new Response(SOURCE_TEXT, {
        status: 200,
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      }),
    );
    renderConvert(server);
    await user.click(await screen.findByRole('button', { name: 'Download port.rs' }));
    await screen.findByText(/Saved port\.rs/);
    await settleSaves();
    expect(saved).toHaveLength(1);
    expect(saved[0]?.name).toBe('port.rs');
    expect(new TextDecoder().decode(saved[0]?.bytes)).toBe(SOURCE_TEXT);
    expect(await sha256Hex(saved[0]?.bytes ?? new Uint8Array())).toBe(CANDIDATE);
  });

  it('saves nothing when the served bytes do not hash to the verified candidate', async () => {
    const user = userEvent.setup();
    const server = serverWith(verified());
    server.on(SOURCE, () =>
      new Response('fn tampered() {}\n', {
        status: 200,
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      }),
    );
    renderConvert(server);
    await user.click(await screen.findByRole('button', { name: 'Download port.rs' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/do not match the verified candidate/i);
    await settleSaves();
    expect(saved).toEqual([]);
  });

  it('saves nothing when the project changes while the source is downloading', async () => {
    const user = userEvent.setup();
    const server = serverWith(verified());
    const pendingSource = deferred<Response>();
    server.on(SOURCE, () => pendingSource.promise);
    renderConvert(server);
    await user.click(await screen.findByRole('button', { name: 'Download port.rs' }));

    server.reply(
      DETAIL,
      detail({ revision: 7, phase: 'mismatch', attempt: attempt({ approved: true }), matched: 1 }),
    );
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    await screen.findByText(/1 of 3 required cases matched/);

    await act(async () => {
      pendingSource.resolve(
        new Response(SOURCE_TEXT, { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }),
      );
      await pendingSource.promise;
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed while downloading/i);
    await settleSaves();
    expect(saved).toEqual([]);
  });

  it.each([
    ['approval is revoked', () => ({ attempt: attempt({ approved: false }), matched: 3 })],
    [
      'export eligibility is withdrawn after a mismatch',
      () => ({ attempt: attempt({ approved: true }), matched: 2, export_available: false }),
    ],
  ])('saves nothing when %s at the same revision during a download', async (_label, change) => {
    const user = userEvent.setup();
    const server = serverWith(verified());
    const pendingSource = deferred<Response>();
    server.on(SOURCE, () => pendingSource.promise);
    renderConvert(server);
    await user.click(await screen.findByRole('button', { name: 'Download port.rs' }));

    server.reply(
      DETAIL,
      detail({ revision: 6, phase: 'verified', export_available: true, ...change() }),
    );
    await act(async () => {
      pendingSource.resolve(
        new Response(SOURCE_TEXT, { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }),
      );
      await pendingSource.promise;
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed while downloading/i);
    await settleSaves();
    expect(saved).toEqual([]);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByText('Verified against every required test')).not.toBeInTheDocument();
  });

  it('shows no download and never requests source for an unverified candidate', async () => {
    const server = serverWith(
      detail({ revision: 3, phase: 'awaiting_review', attempt: attempt() }),
    );
    renderConvert(server);
    await screen.findByText(/Review pending/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(server.count(SOURCE)).toBe(0);
  });

  it('downloads the exact proposal packet only on request, after checking its identity', async () => {
    const user = userEvent.setup();
    const source = 'print(len(open(0).readlines()))\n';
    const sourceSha = await sha256Hex(source);
    const raw = JSON.stringify({
      schema_version: 1,
      project_id: 'line-count',
      source_sha256: sourceSha,
      language: 'python',
      source,
      brief: 'Port the line counter.',
    });
    const server = new FakeServer();
    server.reply(LIST, { schema_version: 1, projects: [summary('line-count', 'Line count', sourceSha)] });
    server.reply(DETAIL, detail({}, summary('line-count', 'Line count', sourceSha)));
    server.on(PACKET, () => new Response(raw, { status: 200 }));
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    expect(server.count(PACKET)).toBe(0);

    await user.click(screen.getByRole('button', { name: 'Download proposal packet' }));
    await screen.findByText(/Saved line-count-proposal-packet\.json/);
    await settleSaves();
    expect(saved[0]?.name).toBe('line-count-proposal-packet.json');
    expect(new TextDecoder().decode(saved[0]?.bytes)).toBe(raw);
    expect(server.mutations()).toEqual([]);
  });

  it('refuses a packet whose source does not match the registered identity', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    server.on(
      PACKET,
      () =>
        new Response(
          JSON.stringify({
            schema_version: 1,
            project_id: 'line-count',
            source_sha256: SOURCE_SHA,
            language: 'python',
            source: 'print("not the registered source")\n',
            brief: 'b',
          }),
          { status: 200 },
        ),
    );
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await user.click(screen.getByRole('button', { name: 'Download proposal packet' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/packet.*does not match/i);
    await settleSaves();
    expect(saved).toEqual([]);
  });
});

describe('Convert: accepted identity survives errors', () => {
  function verifiedBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return detail({
      revision: 3,
      phase: 'verified',
      attempt: attempt({ approved: true }),
      matched: 3,
      export_available: true,
      ...overrides,
    });
  }

  async function goOffline(): Promise<void> {
    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await screen.findByText(/cannot be reached/i);
  }

  it('rejects a same-revision substitution that arrives after an error', async () => {
    const user = userEvent.setup();
    const server = serverWith(verifiedBody());
    renderConvert(server);
    await screen.findByRole('button', { name: 'Download port.rs' });

    await goOffline();
    server.on(DETAIL, () => json(verifiedBody({ attempt: attempt({ id: 'att-9', approved: true }) })));
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/different frozen project identity/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
    expect(screen.queryByText(/3 of 3/)).not.toBeInTheDocument();
    expect(server.mutations()).toEqual([]);
  });

  it('rejects changed candidate bytes after a server error', async () => {
    const user = userEvent.setup();
    const server = serverWith(verifiedBody());
    renderConvert(server);
    await screen.findByRole('button', { name: 'Download port.rs' });

    server.on(DETAIL, () => json({ error: 'internal', message: 'Boom.' }, 500));
    await user.click(screen.getByRole('button', { name: 'Refresh status' }));
    await screen.findByRole('button', { name: 'Retry' });
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();

    server.on(DETAIL, () =>
      json(verifiedBody({ attempt: attempt({ approved: true, rust_source: 'fn other() {}\n' }) })),
    );
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/different frozen project identity/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
  });

  it('shows no older revision after an error', async () => {
    const user = userEvent.setup();
    const server = serverWith(verifiedBody({ revision: 5 }));
    renderConvert(server);
    await screen.findByRole('button', { name: 'Download port.rs' });

    await goOffline();
    server.on(DETAIL, () =>
      json(detail({ revision: 4, phase: 'awaiting_review', attempt: attempt() })),
    );
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/older revision/);
    expect(screen.queryByText(/Review pending/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
  });

  it('still accepts newer mutable evidence for the same identity after an error', async () => {
    const server = serverWith(
      detail({ revision: 3, phase: 'queued', attempt: attempt({ approved: true }) }),
    );
    renderConvert(server);
    await screen.findByText(/Approved outside this page/);

    await goOffline();
    server.on(DETAIL, () => json(verifiedBody()));
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expectEnabled(await screen.findByRole('button', { name: 'Download port.rs' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    await goOffline();
    server.on(DETAIL, () => json(verifiedBody({ revision: 4, phase: 'awaiting_review', matched: null, export_available: false, attempt: attempt({ id: 'att-2' }) })));
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await screen.findByText(/Review pending/);
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();
  });
});

describe('Convert: public proposal packet view', () => {
  const PACKET_SOURCE = '<img src=x onerror="alert(1)">\nprint(len(open(0).readlines()))\n';
  const PACKET_BRIEF = '<script>alert(1)</script> Port <b>this</b> & "that".';

  async function packetServer(): Promise<{ server: FakeServer; sourceSha: string }> {
    const sourceSha = await sha256Hex(PACKET_SOURCE);
    const server = new FakeServer();
    server.reply(LIST, { schema_version: 1, projects: [summary('line-count', 'Line count', sourceSha)] });
    server.reply(DETAIL, detail({}, summary('line-count', 'Line count', sourceSha)));
    server.on(
      PACKET,
      () =>
        new Response(
          JSON.stringify({
            schema_version: 1,
            project_id: 'line-count',
            source_sha256: sourceSha,
            language: 'python',
            source: PACKET_SOURCE,
            brief: PACKET_BRIEF,
          }),
          { status: 200 },
        ),
    );
    return { server, sourceSha };
  }

  it('requests nothing on mount and shows source and brief as inert text on request', async () => {
    const user = userEvent.setup();
    const { server } = await packetServer();
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    expect(server.count(PACKET)).toBe(0);
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'View proposal packet' }));
    const source = await screen.findByRole('textbox', { name: /Packet source/ });
    expect(source).toHaveValue(PACKET_SOURCE);
    expect(screen.getByRole('textbox', { name: /Packet brief/ })).toHaveValue(PACKET_BRIEF);
    expect(document.querySelector('img, script, b')).toBeNull();
    expect(server.count(PACKET)).toBe(1);
    expect(server.mutations()).toEqual([]);
    expect(saved).toEqual([]);

    await user.click(screen.getByRole('button', { name: 'Hide proposal packet' }));
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download proposal packet' })).toBeInTheDocument();
  });

  it('shows nothing from a packet whose source does not match the registered identity', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    server.on(
      PACKET,
      () =>
        new Response(
          JSON.stringify({
            schema_version: 1,
            project_id: 'line-count',
            source_sha256: SOURCE_SHA,
            language: 'python',
            source: 'print("not the registered source")\n',
            brief: 'b',
          }),
          { status: 200 },
        ),
    );
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await user.click(screen.getByRole('button', { name: 'View proposal packet' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/packet.*does not match/i);
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();
  });

  it('hides packet contents when the project becomes unreachable and ignores a late packet', async () => {
    const user = userEvent.setup();
    const { server, sourceSha } = await packetServer();
    renderConvert(server);
    await screen.findByText(/No proposal has been submitted/);
    await user.click(screen.getByRole('button', { name: 'View proposal packet' }));
    await screen.findByRole('textbox', { name: /Packet source/ });

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await screen.findByText(/cannot be reached/i);
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await screen.findByText(/No proposal has been submitted/);
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();

    const late = deferred<Response>();
    server.on(PACKET, () => late.promise);
    await user.click(screen.getByRole('button', { name: 'View proposal packet' }));
    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    await screen.findByText(/cannot be reached/i);
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await screen.findByText(/No proposal has been submitted/);
    await act(async () => {
      late.resolve(
        json({
          schema_version: 1,
          project_id: 'line-count',
          source_sha256: sourceSha,
          language: 'python',
          source: PACKET_SOURCE,
          brief: PACKET_BRIEF,
        }),
      );
      await late.promise;
      await new Promise((resolve) => {
        setTimeout(resolve, 50);
      });
    });
    expect(screen.queryByRole('textbox', { name: /Packet source/ })).not.toBeInTheDocument();
  });
});

describe('Convert: exact frozen text', () => {
  it('shows line endings, escapes and byte count for CRLF text without offering an unverified download', async () => {
    const user = userEvent.setup();
    const crlf = 'fn a() {}\r\nlet s = "\\n";\r\nfn b() {}\rlast\n';
    const server = serverWith(
      detail({
        revision: 2,
        phase: 'awaiting_review',
        attempt: attempt({ rust_source: crlf, candidate_sha256: await sha256Hex(crlf) }),
      }),
    );
    renderConvert(server);
    await screen.findByText(/Review pending/);
    expect(screen.queryByRole('textbox', { name: /Exact candidate text/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Show exact line endings' }));
    const exact = await screen.findByRole('textbox', { name: /Exact candidate text/ });
    expect(exact).toHaveValue('fn a() {}\\r\\n\nlet s = "\\\\n";\\r\\n\nfn b() {}\\rlast\\n\n');
    expect(screen.getByText(/2 CRLF, 1 LF, 1 CR/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`${String(new TextEncoder().encode(crlf).length)} bytes`))).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: /Frozen candidate \(reading view/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download port.rs' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Hide exact line endings' }));
    expect(screen.queryByRole('textbox', { name: /Exact candidate text/ })).not.toBeInTheDocument();
  });
});

describe('Convert: accessibility', () => {
  it('has labeled regions, one live region and exactly one primary action per state', async () => {
    const server = serverWith(detail({ revision: 3, phase: 'awaiting_review', attempt: attempt() }));
    renderConvert(server);
    await screen.findByText(/Review pending/);
    expect(screen.getByRole('region', { name: 'Registered source projects' })).toBeInTheDocument();
    // Astryx buttons each carry their own empty loading announcer; the page adds exactly one.
    expect(
      screen.getAllByRole('status').filter((region) => region instanceof HTMLOutputElement),
    ).toHaveLength(1);
    expect(document.querySelectorAll('[data-primary-action="true"]')).toHaveLength(1);
    const scope = screen.getByRole('region', { name: 'Primary action' });
    expect(within(scope).getByRole('button', { name: 'Refresh status' })).toBeInTheDocument();
  });

  it('selects a project from the keyboard', async () => {
    const user = userEvent.setup();
    const server = serverWith();
    renderConvert(server, '/convert');
    const link = await screen.findByRole('link', { name: /Line count/ });
    link.focus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('heading', { level: 2, name: 'Line count' })).toBeInTheDocument();
  });
});
