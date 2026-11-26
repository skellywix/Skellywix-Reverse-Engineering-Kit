import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import Reconstruct from './Reconstruct';

const RUN_ID = 'a'.repeat(32);
const OTHER_RUN_ID = 'b'.repeat(32);
const STAGED_DIGEST = 'c'.repeat(64);
const PREVIOUS_DIGEST = 'd'.repeat(64);
interface TestStage {
  name: string;
  status: string;
  completed: number | null;
  required: number | null;
  detail: Record<string, unknown>;
}

interface TestSelectedRun {
  id: string;
  attempt: number;
  status: string;
  stage: string;
  stages: TestStage[];
  proof: { matched: number | null; required: number; verified: boolean };
  model: { requested: string; actual: string | null };
  package: { available: boolean; sha256: string | null };
}

interface TestState {
  schema_version: number;
  engine: { state: string; detail: string };
  profile: {
    id: string;
    name: string;
    fixed: boolean;
    required_cases: number;
  };
  runs: Array<{ id: string; attempt: number; status: string; stage: string }>;
  selected_run: TestSelectedRun | null;
  releases: {
    active: string | null;
    previous: string | null;
    staged: string | null;
    items: unknown[];
  };
  catalog: unknown[];
  busy: boolean;
  running: string | null;
  update_error: string | null;
  builder_connected: boolean;
  legacy_evidence: Record<string, unknown> | null;
}


function stateFixture(): TestState {
  return {
    schema_version: 1,
    engine: { state: 'up', detail: 'available' },
    profile: {
      id: 'fourmilab_base64_1_5',
      name: 'Fourmilab Base64 1.5',
      fixed: true,
      required_cases: 2557,
    },
    runs: [
      { id: RUN_ID, attempt: 1, status: 'verified', stage: 'package' },
      { id: OTHER_RUN_ID, attempt: 3, status: 'blocked', stage: 'verify' },
    ],
    selected_run: {
      id: RUN_ID,
      attempt: 1,
      status: 'verified',
      stage: 'package',
      stages: [
        {
          name: 'intake',
          status: 'completed',
          completed: null,
          required: null,
          detail: { worker: 'File analyst', deterministic: true },
        },
        {
          name: 'analyze',
          status: 'completed',
          completed: 2557,
          required: 2557,
          detail: { worker: 'Behavior analyst', outcome: 'recorded' },
        },
        {
          name: 'rebuild',
          status: 'completed',
          completed: null,
          required: null,
          detail: { worker: 'Rust builder', generation: 1 },
        },
        {
          name: 'verify',
          status: 'completed',
          completed: 2557,
          required: 2557,
          detail: { worker: 'Verifier', outcome: 'matched' },
        },
        {
          name: 'package',
          status: 'verified',
          completed: 2557,
          required: 2557,
          detail: { worker: 'Packager', outcome: 'verified' },
        },
      ],
      proof: { matched: 2557, required: 2557, verified: true },
      model: { requested: 'pinned-requested-model', actual: null },
      package: { available: true, sha256: 'e'.repeat(64) },
    },
    releases: {
      active: 'f'.repeat(64),
      previous: PREVIOUS_DIGEST,
      staged: STAGED_DIGEST,
      items: [],
    },
    catalog: [],
    busy: false,
    running: null,
    update_error: null,
    builder_connected: true,
    legacy_evidence: { retained: true },
  };
}

function requireSelectedRun(state: TestState): TestSelectedRun {
  if (state.selected_run === null) {
    throw new Error('Fixture must contain a selected run.');
  }
  return state.selected_run;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mockResponses(...bodies: unknown[]): Mock<typeof fetch> {
  const fetchMock = vi.fn<typeof fetch>();
  for (const body of bodies) {
    fetchMock.mockResolvedValueOnce(jsonResponse(body));
  }
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationProbe(): React.JSX.Element {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function renderReconstruct(initialEntry = `/reconstruct/runs/${RUN_ID}`): void {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/reconstruct"
          element={
            <>
              <Reconstruct />
              <LocationProbe />
            </>
          }
        />
        <Route
          path="/reconstruct/runs/:id"
          element={
            <>
              <Reconstruct />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function expectPost(
  fetchMock: Mock<typeof fetch>,
  callIndex: number,
  path: string,
  body: unknown,
): void {
  const call = fetchMock.mock.calls[callIndex];
  expect(call).toBeDefined();
  const [input, init] = call!;
  expect(String(input)).toBe(path);
  expect(init?.method).toBe('POST');
  expect(init?.body).toBe(JSON.stringify(body));
  expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Reconstruct workflow', () => {
  it('shows original tests in progress before there is any comparison result', async () => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    Object.assign(run, { status: 'running', stage: 'analyze', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    run.stages = run.stages.map((step) => step.name === 'intake' ? step : step.name === 'analyze'
      ? { ...step, status: 'running', completed: 256, detail: { worker: 'Behavior analyst' } }
      : { ...step, status: 'pending', completed: null, required: null, detail: {} });
    state.runs = [{ id: RUN_ID, status: 'running', stage: 'analyze', attempt: 1 }];
    state.running = RUN_ID;
    state.busy = true;
    mockResponses(state);
    renderReconstruct();

    expect(await screen.findByText(/no comparison result yet/i)).toBeVisible();
    const steps = within(screen.getByRole('list', { name: 'Reconstruction stages' }));
    expect(steps.getByRole('listitem', { current: 'step' })).toHaveTextContent('Test the original');
    expect(steps.getByRole('progressbar', { name: /original/i })).toHaveAttribute('aria-valuenow', '256');
    expect(screen.queryByRole('link', { name: /download verified package/i })).not.toBeInTheDocument();
  });

  it('never marks a finished run as having a current step', async () => {
    mockResponses(stateFixture());
    renderReconstruct();
    const stages = await screen.findByRole('list', { name: 'Reconstruction stages' });
    expect(within(stages).getAllByRole('listitem')).toHaveLength(5);
    expect(within(stages).getByText('Rust source written')).toBeVisible();
    expect(within(stages).queryByRole('listitem', { current: 'step' })).toBeNull();
  });

  it('reports written source rather than a completed build before the compiler starts', async () => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    Object.assign(run, { status: 'completed', stage: 'rebuild', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    run.stages = run.stages.map(s => s.name === 'verify' || s.name === 'package'
      ? { ...s, status: 'pending', completed: null, required: null, detail: {} } : s);
    state.runs = [{ id: RUN_ID, attempt: 1, status: 'completed', stage: 'rebuild' }];
    mockResponses(state);
    renderReconstruct();
    expect(await screen.findByText('Attempt 1 · Build the Rust version · Rust source written')).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download verified package' })).toBeNull();
  });

  it.each([true, false])('keeps a saved running report readable without a live worker (builder connected: %s)', async (builderConnected) => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    Object.assign(run, { stage: 'analyze', status: 'running', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    run.stages[1] = { ...run.stages[1]!, status: 'running', completed: 256 };
    state.runs = [{ id: RUN_ID, status: 'running', stage: 'analyze', attempt: 1 }];
    mockResponses({ ...state, builder_connected: builderConnected });
    renderReconstruct();
    expect(await screen.findByText('Last reported as running')).toBeVisible();
    expect(screen.queryByRole('listitem', { current: 'step' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel current run' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'View current step' })).toBeNull();
    expect(screen.getByRole('link', { name: 'View saved steps' })).toHaveAttribute('href', '#reconstruct-stages');
  });

  it('explains the missing active release rather than offering a start that must fail', async () => {
    const state = stateFixture();
    Object.assign(state, { runs: [], selected_run: null, releases: { active: null, previous: null, staged: null, items: [] } });
    const fetchMock = mockResponses(state);
    renderReconstruct('/reconstruct');
    expect(await screen.findByText(/no active release/i)).toBeVisible();
    expect(screen.getByText(/Fourmilab operator desk.*Releases and updates/i)).toBeVisible();
    const start = screen.getByRole('button', { name: 'Start reconstruction' });
    expect(start).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(start);
    expect(fetchMock.mock.calls.every(([, init]) => init?.method === undefined)).toBe(true);
  });

  it('shows a rejected comparison as a difference even if the comparison step finished', async () => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    Object.assign(run, { status: 'running', stage: 'rebuild', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    run.stages[2] = { name: 'rebuild', status: 'running', completed: null, required: null, detail: { generation: 2, worker: 'Rust builder' } };
    run.stages[3] = { name: 'verify', status: 'completed', completed: null, required: 2557, detail: { generation: 1, outcome: 'rejected', matched: 2556 } };
    run.stages[4] = { name: 'package', status: 'pending', completed: null, required: null, detail: {} };
    state.runs = [{ id: RUN_ID, status: 'running', stage: 'rebuild', attempt: 1 }];
    state.busy = true;
    state.running = RUN_ID;
    mockResponses(state);
    renderReconstruct();
    const stages = await screen.findByRole('list', { name: 'Reconstruction stages' });
    expect(within(stages).getByRole('listitem', { current: 'step' })).toHaveTextContent('Build the Rust version');
    expect(within(stages).getByText('Results differed')).toBeVisible();
    expect(screen.getByText('Build 1: 2,556 of 2,557 tests matched.')).toBeVisible();
    expect(screen.queryByRole('link', { name: /download verified package/i })).toBeNull();
  });

  it('keeps the build number when a rejected comparison has no counts', async () => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    run.proof = { matched: null, required: 2557, verified: false };
    run.stages[3] = { name: 'verify', status: 'completed', completed: null, required: null, detail: { generation: 1, outcome: 'rejected' } };
    mockResponses(state);
    renderReconstruct();
    expect(await screen.findByText('Build 1: the comparison found differences.')).toBeVisible();
  });

  it('shows current-attempt evidence without presenting the requested model as actual', async () => {
    const fetchMock = mockResponses(stateFixture());
    renderReconstruct();

    expect(await screen.findByRole('heading', { name: 'Reconstruct' })).toBeVisible();
    expect(screen.getByText('Fourmilab Base64 1.5')).toBeVisible();
    expect(screen.getByText('Attempt 1')).toBeVisible();
    expect(screen.getByText('2,557 of 2,557 tests matched')).toBeVisible();
    expect(screen.getByText('pinned-requested-model')).toBeVisible();
    expect(screen.getByText(/unknown.*no model receipt/i)).toBeVisible();
    expect(screen.getByRole('progressbar', { name: 'Original tests run' })).toHaveAttribute(
      'aria-valuenow',
      '2557',
    );
    const handoffTable = screen.getByRole('table', { name: 'Recorded handoffs' });
    expect(within(handoffTable).getAllByText('Test the original')).not.toHaveLength(0);
    expect(within(handoffTable).getAllByText('Build the Rust version')).not.toHaveLength(0);
    expect(screen.queryByText('retained')).not.toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `/api/v1/reconstruct/state?run=${RUN_ID}`,
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined();
  });

  it('refreshes state after capturing the SSE cursor so bootstrap events cannot be skipped', async () => {
    class FakeEventSource {
      static readonly urls: string[] = [];
      readonly listeners = new Map<string, Set<EventListener>>();
      readonly close = vi.fn();

      constructor(url: string) {
        FakeEventSource.urls.push(url);
      }

      addEventListener(type: string, listener: EventListener): void {
        const listeners = this.listeners.get(type) ?? new Set<EventListener>();
        listeners.add(listener);
        this.listeners.set(type, listeners);
      }

      removeEventListener(type: string, listener: EventListener): void {
        this.listeners.get(type)?.delete(listener);
      }
    }

    const empty = {
      ...stateFixture(),
      runs: [],
      selected_run: null,
      releases: { active: null, previous: null, staged: null, items: [] },
    };
    // Promise.withResolvers is outside this project's ES2023 TypeScript library.
    let resolveInitial!: (response: Response) => void;
    let resolveSnapshot!: (response: Response) => void;
    const initialResponse = new Promise<Response>((resolve) => {
      resolveInitial = resolve;
    });
    const snapshotResponse = new Promise<Response>((resolve) => {
      resolveSnapshot = resolve;
    });
    let stateReads = 0;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation((input) => {
      const url = String(input);
      if (url.startsWith('/api/v1/reconstruct/state')) {
        stateReads += 1;
        return stateReads === 1
          ? initialResponse
          : Promise.resolve(jsonResponse(stateFixture()));
      }
      if (url === '/api/v1/snapshot') {
        return snapshotResponse;
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', FakeEventSource);
    renderReconstruct('/reconstruct');

    resolveInitial(jsonResponse(empty));
    expect(await screen.findByRole('heading', { name: 'No reconstruction runs' })).toBeVisible();
    resolveSnapshot(
      jsonResponse({
        schema_version: 1,
        cursor: 7,
        generated_at: '2026-11-24T12:00:00Z',
        projects: [
          {
            id: 'p_1',
            kind: 'reconstruct',
            name: 'Fourmilab Base64',
            revision: 1,
            created_at: '2026-11-24T11:00:00Z',
          },
        ],
        jobs: [],
        engines: {
          fourmilab: { state: 'up', detail: 'available' },
          inspector: { state: 'unknown', detail: 'not checked' },
        },
        releases: { active: null, previous: null, staged: null },
      }),
    );

    expect(await screen.findByText('Attempt 1')).toBeVisible();
    expect(FakeEventSource.urls).toEqual(['/api/v1/events?after=7']);
    expect(stateReads).toBe(2);
    expect(fetchMock.mock.calls.every(([, init]) => init?.method === undefined)).toBe(true);
  });

  it.each([
    {
      name: 'engine down',
      state: {
        schema_version: 1,
        engine: { state: 'down', detail: 'legacy engine unreachable' },
        profile: null,
        runs: null,
        selected_run: null,
        releases: null,
        catalog: null,
        busy: null,
        running: null,
        update_error: null,
        builder_connected: null,
        legacy_evidence: null,
      },
      message: /legacy engine unreachable/i,
      actionName: /start reconstruction/i,
    },
    {
      name: 'stale projection',
      state: { ...stateFixture(), update_error: 'legacy poll timed out' },
      message: /legacy poll timed out/i,
      actionName: /state unavailable/i,
    },
  ])('disables mutations for $name', async ({ state, message, actionName }) => {
    const fetchMock = mockResponses(state);
    renderReconstruct('/reconstruct');

    expect((await screen.findAllByText(message))[0]).toBeVisible();
    expect(screen.getByRole('button', { name: actionName })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not present an offline engine as an empty saved-run history', async () => {
    mockResponses({
      schema_version: 1,
      engine: { state: 'down', detail: 'legacy engine unreachable' },
      profile: null,
      runs: null,
      selected_run: null,
      releases: null,
      catalog: null,
      busy: null,
      running: null,
      update_error: null,
      builder_connected: null,
      legacy_evidence: null,
    });

    renderReconstruct('/reconstruct');

    expect(await screen.findByText('legacy engine unreachable')).toBeVisible();
    expect(
      screen.queryByRole('heading', { name: 'No reconstruction runs' }),
    ).not.toBeInTheDocument();
  });


  it.each([
    {
      name: 'a different selected run',
      state: {
        ...stateFixture(),
        selected_run: { ...stateFixture().selected_run, id: OTHER_RUN_ID },
      },
      message: /selected run does not match the route/i,
    },
    {
      name: 'a stale selected attempt',
      state: {
        ...stateFixture(),
        runs: stateFixture().runs.map((run) =>
          run.id === RUN_ID ? { ...run, attempt: 2 } : run,
        ),
      },
      message: /selected attempt does not match the current run/i,
    },
  ])('refuses package and actions for $name', async ({ state, message }) => {
    const fetchMock = mockResponses(state);
    renderReconstruct();

    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.queryByRole('link', { name: /download verified package/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /state unavailable/i })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps a verified package downloadable while the builder is offline', async () => {
    const builderOffline = { ...stateFixture(), builder_connected: false };
    const fetchMock = mockResponses(builderOffline);
    const user = userEvent.setup();
    renderReconstruct();

    expect(await screen.findByText('Builder unavailable')).toBeVisible();
    expect(
      screen.getByRole('link', { name: 'Download verified package' }),
    ).toHaveAttribute('href', `/api/v1/reconstruct/runs/${RUN_ID}/package`);

    await user.click(screen.getByRole('button', { name: 'Release controls' }));
    for (const name of [
      'Stage engine update',
      'Promote staged release',
      'Rollback to previous release',
    ]) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true');
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps an incomplete proof unverified and retries only after the operator acts', async () => {
    const incomplete = stateFixture();
    const incompleteRun = requireSelectedRun(incomplete);
    incomplete.selected_run = {
      ...incompleteRun,
      status: 'failed',
      stage: 'verify',
      proof: { matched: 2556, required: 2557, verified: false },
      package: { available: false, sha256: null },
      stages: incompleteRun.stages.map((stage) =>
        stage.name === 'verify'
          ? { ...stage, status: 'failed', completed: 2556 }
          : stage.name === 'package'
            ? { ...stage, status: 'pending', completed: null, required: null }
            : stage,
      ),
    };
    incomplete.runs = incomplete.runs.map((run) =>
      run.id === RUN_ID ? { ...run, status: 'failed', stage: 'verify' } : run,
    );
    const retried = {
      ...incomplete,
      selected_run: { ...requireSelectedRun(incomplete), attempt: 2, status: 'queued' },
      runs: incomplete.runs.map((run) =>
        run.id === RUN_ID ? { ...run, attempt: 2, status: 'queued' } : run,
      ),
    };
    const fetchMock = mockResponses(incomplete, { ok: true, run: RUN_ID }, retried);
    const user = userEvent.setup();
    renderReconstruct();

    expect(await screen.findByText('2,556 of 2,557 tests matched')).toBeVisible();
    expect(screen.queryByRole('link', { name: /download verified package/i })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: 'Retry original inputs' }));

    await screen.findByText(/retry accepted/i);
    expectPost(fetchMock, 1, `/api/v1/reconstruct/runs/${RUN_ID}/retry`, {});
    expect(await screen.findByText('Attempt 2')).toBeVisible();
  });

  it('cannot retry a stopped run while another owns the engine without a release lock', async () => {
    const state = stateFixture();
    const run = requireSelectedRun(state);
    Object.assign(run, { status: 'failed', stage: 'verify', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    state.runs[0] = { id: RUN_ID, attempt: 1, status: 'failed', stage: 'verify' };
    state.running = OTHER_RUN_ID;
    const request = mockResponses(state);
    renderReconstruct();
    const retry = await screen.findByRole('button', { name: 'Retry original inputs' });
    expect(retry).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(retry);
    expect(request.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
  });

  it('will not confirm cancellation after the same attempt finishes', async () => {
    const active = stateFixture();
    const run = requireSelectedRun(active);
    Object.assign(run, { status: 'running', stage: 'rebuild', proof: { matched: null, required: 2557, verified: false }, package: { available: false, sha256: null } });
    active.runs[0] = { id: RUN_ID, attempt: 1, status: 'running', stage: 'rebuild' };
    active.running = RUN_ID;
    const stream = new EventTarget();
    let state = active;
    let reads = 0;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      if (String(input) === '/api/v1/snapshot') return jsonResponse({ schema_version: 1, cursor: 0, generated_at: '2026-11-24T12:00:00Z', projects: [], jobs: [],
        engines: { fourmilab: active.engine, inspector: active.engine }, releases: { active: null, staged: null, previous: null } });
      if (String(input).startsWith('/api/v1/reconstruct/state')) {
        reads += 1;
        return jsonResponse(state);
      }
      throw new Error('Unexpected request');
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', class {
      addEventListener = stream.addEventListener.bind(stream);
      removeEventListener = stream.removeEventListener.bind(stream);
      close() {}
    });
    renderReconstruct();
    await waitFor(() => expect(reads).toBe(2));
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel current run' }));
    state = stateFixture();
    stream.dispatchEvent(new MessageEvent('workspace', { data: JSON.stringify({
      schema_version: 1, event_id: 1, type: 'project.created', project_id: 'p_1', revision: 1,
      job_id: null, attempt_id: null, phase: null, at: '2026-11-24T12:00:00Z', payload: {},
    }) }));
    await screen.findByText('Last reported step');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel this run' }));
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('starts the fixed workflow only after the primary action and sends an empty object', async () => {
    const empty = {
      ...stateFixture(),
      runs: [],
      selected_run: null,
    };
    const queued = stateFixture();
    const queuedRun = requireSelectedRun(queued);
    queued.selected_run = {
      ...queuedRun,
      status: 'queued',
      stage: 'intake',
      proof: { matched: null, required: 2557, verified: false },
      package: { available: false, sha256: null },
    };
    queued.runs = [{ id: RUN_ID, attempt: 1, status: 'queued', stage: 'intake' }];
    const fetchMock = mockResponses(empty, { ok: true, run: RUN_ID }, queued);
    const user = userEvent.setup();
    renderReconstruct('/reconstruct');

    expect(await screen.findByRole('heading', { name: 'No reconstruction runs' })).toBeVisible();

    const start = await screen.findByRole('button', { name: 'Start reconstruction' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await user.click(start);

    expectPost(fetchMock, 1, '/api/v1/reconstruct/start', {});
    await waitFor(() => {
      expect(screen.getByLabelText('Current route')).toHaveTextContent(
        `/reconstruct/runs/${RUN_ID}`,
      );
    });
    expect(await screen.findByText(/reconstruction accepted/i)).toBeVisible();
  });

  it('locks further mutations when a success response cannot prove the action outcome', async () => {
    const empty = {
      ...stateFixture(),
      runs: [],
      selected_run: null,
    };
    const fetchMock = mockResponses(empty, { unexpected: true });
    const user = userEvent.setup();
    renderReconstruct('/reconstruct');

    await user.click(await screen.findByRole('button', { name: 'Start reconstruction' }));

    expectPost(fetchMock, 1, '/api/v1/reconstruct/start', {});
    expect(await screen.findByText(/action outcome unknown/i)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Start reconstruction' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('requires exact-run confirmation before cancelling and respects rejection', async () => {
    const active = stateFixture();
    const activeRun = requireSelectedRun(active);
    active.selected_run = { ...activeRun, status: 'running', stage: 'analyze' };
    active.runs = active.runs.map((run) =>
      run.id === RUN_ID ? { ...run, status: 'running', stage: 'analyze' } : run,
    );
    active.running = RUN_ID;
    active.busy = true;
    const cancelled = {
      ...active,
      selected_run: { ...requireSelectedRun(active), status: 'cancelled' },
      running: null,
      busy: false,
    };
    const fetchMock = mockResponses(active, { ok: true, run: RUN_ID }, cancelled);
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('button', { name: 'Cancel current run' }));
    const dialog = screen.getByRole('alertdialog', { name: /cancel run/i });
    expect(dialog).toHaveTextContent(RUN_ID);
    expect(dialog).toHaveTextContent('attempt 1');
    await user.click(within(dialog).getByRole('button', { name: 'Keep running' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: 'Cancel current run' }));
    await user.click(
      within(screen.getByRole('alertdialog', { name: /cancel run/i })).getByRole('button', {
        name: 'Cancel this run',
      }),
    );

    expectPost(fetchMock, 1, `/api/v1/reconstruct/runs/${RUN_ID}/cancel`, {});
    expect(await screen.findByText(/cancellation accepted/i)).toBeVisible();
  });

  it.each(['completed', 'running'])('does not present the saved status %s as a step name', async (status) => {
    const state = stateFixture();
    state.runs[1] = { id: OTHER_RUN_ID, attempt: 3, status, stage: status };
    mockResponses(state);
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('combobox', { name: 'Run' }));
    const option = screen.getByRole('option', { name: new RegExp(OTHER_RUN_ID.slice(0, 8)) });
    expect(within(option).queryByText(status)).not.toBeInTheDocument();
  });

  it('selects a run through the route and reloads that exact record', async () => {
    const first = stateFixture();
    const second = stateFixture();
    const secondRun = requireSelectedRun(second);
    second.selected_run = {
      ...secondRun,
      id: OTHER_RUN_ID,
      attempt: 3,
      status: 'blocked',
      stage: 'verify',
      proof: { matched: 2000, required: 2557, verified: false },
      package: { available: false, sha256: null },
    };
    const fetchMock = mockResponses(first, second);
    const user = userEvent.setup();
    renderReconstruct('/reconstruct');

    const selector = await screen.findByRole('combobox', { name: 'Run' });
    await user.click(selector);
    await user.click(screen.getByRole('option', { name: new RegExp(OTHER_RUN_ID.slice(0, 8)) }));

    expect(await screen.findByLabelText('Current route')).toHaveTextContent(
      `/reconstruct/runs/${OTHER_RUN_ID}`,
    );
    await waitFor(() => {
      expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
        `/api/v1/reconstruct/state?run=${OTHER_RUN_ID}`,
      );
    });
    expect(await screen.findByText('Attempt 3')).toBeVisible();
  });

  it('stages explicitly and confirms the exact staged digest before promotion', async () => {
    const fetchMock = mockResponses(
      stateFixture(),
      { ok: true },
      stateFixture(),
      { ok: true },
      stateFixture(),
    );
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));
    expectPost(fetchMock, 1, '/api/v1/reconstruct/releases/stage', {});
    expect(
      await screen.findByText(/stage request accepted; current release state refreshed/i),
    ).toBeVisible();
    expect(screen.queryByText(/release staged/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Promote staged release' }));
    let dialog = screen.getByRole('alertdialog', { name: /promote staged release/i });
    expect(dialog).toHaveTextContent(STAGED_DIGEST);
    await user.click(within(dialog).getByRole('button', { name: 'Keep current release' }));
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await user.click(screen.getByRole('button', { name: 'Promote staged release' }));
    dialog = screen.getByRole('alertdialog', { name: /promote staged release/i });
    await user.click(within(dialog).getByRole('button', { name: 'Promote this digest' }));

    expectPost(fetchMock, 3, '/api/v1/reconstruct/releases/promote', {
      target_digest: STAGED_DIGEST,
      confirm_target: STAGED_DIGEST,
    });
    expect(await screen.findByText(/promotion accepted/i)).toBeVisible();
  });

  it('does not report successful staging when the post-action refresh fails', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(stateFixture()))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockRejectedValueOnce(new Error('refresh offline'));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));

    expectPost(fetchMock, 1, '/api/v1/reconstruct/releases/stage', {});
    expect(
      await screen.findByText(/action accepted, but current state could not be verified/i),
    ).toBeVisible();
    expect(screen.queryByText(/stage request accepted/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/release staged/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stage engine update' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('does not verify staging from a stale HTTP-200 refresh', async () => {
    const stale = { ...stateFixture(), update_error: 'legacy poll timed out' };
    const fetchMock = mockResponses(stateFixture(), { ok: true }, stale);
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));

    expectPost(fetchMock, 1, '/api/v1/reconstruct/releases/stage', {});
    expect(
      await screen.findByText(/action accepted, but current state could not be verified/i),
    ).toBeVisible();
    expect(screen.queryByText(/stage request accepted/i)).not.toBeInTheDocument();
    expect(screen.getAllByText('legacy poll timed out')).not.toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Stage engine update' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('does not verify staging from a response superseded by a stale live refresh', async () => {
    let finishSnapshot!: (response: Response) => void;
    let finishStageRead!: (response: Response) => void;
    const snapshot = new Promise<Response>((resolve) => { finishSnapshot = resolve; });
    const stageRead = new Promise<Response>((resolve) => { finishStageRead = resolve; });
    const stale = { ...stateFixture(), update_error: 'Newer catalog read is stale' };
    let reads = 0;
    vi.stubGlobal('EventSource', class extends EventTarget { close = vi.fn(); });
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === '/api/v1/snapshot') return snapshot;
      if (url === '/api/v1/reconstruct/releases/stage') return jsonResponse({ ok: true });
      if (url === `/api/v1/reconstruct/state?run=${RUN_ID}`) {
        reads += 1;
        if (reads === 1) return jsonResponse(stateFixture());
        if (reads === 2) return stageRead;
        if (reads === 3) return jsonResponse(stale);
      }
      throw new Error(`Unexpected request: ${url}`);
    }));
    const user = userEvent.setup();
    renderReconstruct();
    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));
    await waitFor(() => expect(reads).toBe(2));

    finishSnapshot(jsonResponse({
      schema_version: 1, cursor: 7, generated_at: '2026-11-24T12:00:00Z',
      projects: [], jobs: [],
      engines: {
        fourmilab: { state: 'up', detail: 'available' },
        inspector: { state: 'unknown', detail: 'not checked' },
      },
      releases: { active: null, previous: null, staged: null },
    }));
    expect((await screen.findAllByText(stale.update_error))[0]).toBeVisible();
    finishStageRead(jsonResponse(stateFixture()));

    const outcome = await screen.findByText(/action accepted, but|stage request accepted;/i);
    expect(outcome).toHaveTextContent(/current state could not be verified/i);
    expect(screen.getByRole('button', { name: 'Stage engine update' })).toHaveAttribute(
      'aria-disabled', 'true',
    );
  });

  it('confirms the exact previous digest before rollback', async () => {
    const fetchMock = mockResponses(stateFixture(), { ok: true }, stateFixture());
    const user = userEvent.setup();
    renderReconstruct();

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Rollback to previous release' }));
    const dialog = screen.getByRole('alertdialog', { name: /rollback release/i });
    expect(dialog).toHaveTextContent(PREVIOUS_DIGEST);
    await user.click(within(dialog).getByRole('button', { name: 'Roll back' }));

    expectPost(fetchMock, 1, '/api/v1/reconstruct/releases/rollback', {
      target_digest: PREVIOUS_DIGEST,
      confirm_target: PREVIOUS_DIGEST,
    });
    expect(await screen.findByText(/rollback accepted/i)).toBeVisible();
  });
});
