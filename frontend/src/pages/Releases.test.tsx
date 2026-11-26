import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import Releases from './Releases';

const ACTIVE_DIGEST = 'a'.repeat(64);
const PREVIOUS_DIGEST = 'b'.repeat(64);
const STAGED_DIGEST = 'c'.repeat(64);
const CHANGED_DIGEST = 'd'.repeat(64);
const SOURCE_DIGEST = 'e'.repeat(64);
const FILE_DIGEST = 'f'.repeat(64);
const KIT_COMMIT = '1'.repeat(40);
const REA_COMMIT = '2'.repeat(40);
const SHELL_ACTIVE = '3'.repeat(64);
const SHELL_PREVIOUS = '4'.repeat(64);
const PRIVATE_PATH = 'C:\\Users\\private-operator\\release-secrets\\receipt.json';
const RUN_ID = '5'.repeat(32);

interface EngineFixtureOptions {
  active?: string | null;
  previous?: string | null;
  staged?: string | null;
  items?: unknown[];
  catalog?: unknown[];
  selectedRun?: Record<string, unknown> | null;
  updateError?: string | null;
  builderConnected?: boolean;
  busy?: boolean;
}

function engineFixture({
  active = ACTIVE_DIGEST,
  previous = PREVIOUS_DIGEST,
  staged = STAGED_DIGEST,
  items = [],
  catalog = [],
  selectedRun = null,
  updateError = null,
  builderConnected = true,
  busy = false,
}: EngineFixtureOptions = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    engine: { state: 'up', detail: 'available' },
    profile: {
      id: 'fourmilab_base64_1_5',
      name: 'Fourmilab Base64 1.5',
      fixed: true,
      required_cases: 2557,
    },
    runs:
      selectedRun === null
        ? []
        : [{ id: RUN_ID, attempt: 1, status: 'verified', stage: 'package' }],
    selected_run: selectedRun,
    releases: { active, previous, staged, items },
    catalog,
    busy,
    running: null,
    update_error: updateError,
    builder_connected: builderConnected,
  };
}

function verifiedRun(): Record<string, unknown> {
  return {
    id: RUN_ID,
    attempt: 1,
    status: 'verified',
    stage: 'package',
    stages: ['intake', 'analyze', 'rebuild', 'verify', 'package'].map((name) => ({
      name,
      status: name === 'package' ? 'verified' : 'completed',
      completed: name === 'analyze' || name === 'verify' || name === 'package' ? 2557 : null,
      required: name === 'analyze' || name === 'verify' || name === 'package' ? 2557 : null,
      detail: {},
    })),
    proof: { matched: 2557, required: 2557, verified: true },
    model: { requested: 'pinned-model', actual: 'reported-model' },
    package: { available: true, sha256: FILE_DIGEST },
  };
}

function downEngineFixture(): Record<string, unknown> {
  return {
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
  };
}

function workspaceFixture(
  releases: Record<string, unknown> = {
    active: { target_id: SHELL_ACTIVE, path: PRIVATE_PATH },
    previous: SHELL_PREVIOUS,
    staged: null,
  },
): Record<string, unknown> {
  return {
    schema_version: 1,
    cursor: 7,
    generated_at: '2026-11-24T12:00:00Z',
    projects: [],
    jobs: [],
    engines: {
      fourmilab: { state: 'up', detail: 'available' },
      inspector: { state: 'unknown', detail: 'not observed' },
    },
    releases,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface MockApiOptions {
  states: readonly unknown[];
  workspace?: unknown | Error;
  posts?: readonly unknown[];
}

function mockApi({
  states,
  workspace = workspaceFixture(),
  posts = [],
}: MockApiOptions): Mock<typeof fetch> {
  const stateQueue = [...states];
  const postQueue = [...posts];
  const fetchMock = vi.fn<typeof fetch>().mockImplementation((input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    if (method === 'GET' && url === '/api/v1/reconstruct/state') {
      const next = stateQueue.shift();
      if (next === undefined) {
        return Promise.reject(new Error('Unexpected reconstruction state read'));
      }
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(jsonResponse(next));
    }
    if (method === 'GET' && url === '/api/v1/snapshot') {
      return workspace instanceof Error
        ? Promise.reject(workspace)
        : Promise.resolve(jsonResponse(workspace));
    }
    if (method === 'POST' && url.startsWith('/api/v1/reconstruct/releases/')) {
      const next = postQueue.shift();
      if (next === undefined) {
        return Promise.reject(new Error(`Unexpected release action: ${url}`));
      }
      return Promise.resolve(jsonResponse(next));
    }
    return Promise.reject(new Error(`Unexpected request: ${method} ${url}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function postCalls(fetchMock: Mock<typeof fetch>): Parameters<typeof fetch>[] {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
}

function expectPost(
  fetchMock: Mock<typeof fetch>,
  path: string,
  body: Record<string, unknown>,
): void {
  const calls = postCalls(fetchMock);
  expect(calls).toHaveLength(1);
  const [input, init] = calls[0]!;
  expect(String(input)).toBe(path);
  expect(init?.body).toBe(JSON.stringify(body));
  expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Releases page', () => {
  it.each([{ builderConnected: false }, { busy: true }])('keeps a fresh report readable when changes are paused: %o', async (options) => {
    const fetchMock = mockApi({ states: [engineFixture(options)] });
    render(<Releases />);
    expect(await screen.findByText('Current snapshot')).toBeVisible();
    expect(screen.getByText(/release changes are paused/i)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Release controls' }));
    expect(screen.getByRole('button', { name: 'Promote staged release' })).toHaveAttribute('aria-disabled', 'true');
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it('shows only recorded Fourmilab pins and keeps workspace-shell identities separate', async () => {
    const receipt = {
      digest: ACTIVE_DIGEST,
      info: {
        kit: KIT_COMMIT,
        rea: '6.9.0',
        rea_commit: REA_COMMIT,
        rea_source_sha256: SOURCE_DIGEST,
        files: {
          'inspect_file.py': FILE_DIGEST,
          'rea-source.tgz': SOURCE_DIGEST,
        },
        path: PRIVATE_PATH,
        image: { path: PRIVATE_PATH },
      },
      path: PRIVATE_PATH,
    };
    const fetchMock = mockApi({
      states: [
        engineFixture({
          items: [receipt],
          catalog: [
            {
              role: 'Rust builder',
              skill: 'Fourmilab Base64 compatibility',
              scope: 'Text-only subscription session; no tools or filesystem.',
              path: PRIVATE_PATH,
            },
          ],
        }),
      ],
    });

    render(<Releases />);

    expect(await screen.findByRole('heading', { name: 'Releases' })).toBeVisible();
    const engine = screen.getByRole('region', { name: 'Fixed Fourmilab engine releases' });
    expect(within(engine).getAllByText(ACTIVE_DIGEST)).not.toHaveLength(0);
    expect(within(engine).getByText(KIT_COMMIT)).toBeVisible();
    expect(within(engine).getByText(REA_COMMIT)).toBeVisible();
    expect(within(engine).getAllByText(SOURCE_DIGEST)).not.toHaveLength(0);
    expect(within(engine).getByText(/image identity is not recorded/i)).toBeVisible();
    expect(within(engine).getByText(/probe receipt is not recorded/i)).toBeVisible();
    expect(within(engine).getByText('Rust builder')).toBeVisible();

    const shell = screen.getByRole('region', { name: 'Workspace shell snapshot' });
    expect(within(shell).getByText(SHELL_ACTIVE)).toBeVisible();
    expect(within(shell).getByText(SHELL_PREVIOUS)).toBeVisible();
    expect(within(shell).getByText(/not proof of what is running/i)).toBeVisible();
    expect(within(shell).getByText(/only the release command can switch the workspace version/i)).toBeVisible();

    expect(document.body).not.toHaveTextContent(PRIVATE_PATH);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it('distinguishes an empty recorded catalog from unavailable state', async () => {
    mockApi({
      states: [
        engineFixture({
          active: null,
          previous: null,
          staged: null,
          items: [],
          catalog: [],
        }),
      ],
      workspace: workspaceFixture({ active: null, previous: null, staged: null }),
    });

    render(<Releases />);

    expect(await screen.findByRole('heading', { name: 'No engine release receipts recorded' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'No declared worker catalog' })).toBeVisible();
    expect(screen.getByText(/no workspace-shell target identities are recorded/i)).toBeVisible();
    expect(screen.queryByText(/engine unavailable/i)).not.toBeInTheDocument();
  });

  it('renders unavailable engine and workspace reads without presenting them as empty', async () => {
    mockApi({
      states: [downEngineFixture()],
      workspace: new Error(`snapshot failed at ${PRIVATE_PATH}`),
    });

    render(<Releases />);

    expect(await screen.findByText('Fourmilab engine unavailable')).toBeVisible();
    expect(screen.getByText('legacy engine unreachable')).toBeVisible();
    expect(screen.getByText('Workspace shell snapshot unavailable')).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'No engine release receipts recorded' })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(PRIVATE_PATH);
    expect(screen.queryByRole('button', { name: 'Release controls' })).not.toBeInTheDocument();
  });

  it('rechecks current data and sends no mutation when the confirmed staged target changed', async () => {
    const fetchMock = mockApi({
      states: [
        engineFixture(),
        engineFixture({ staged: CHANGED_DIGEST }),
      ],
    });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Promote staged release' }));
    const dialog = screen.getByRole('alertdialog', { name: /promote staged release/i });
    expect(dialog).toHaveTextContent(STAGED_DIGEST);
    await user.click(within(dialog).getByRole('button', { name: 'Promote this digest' }));

    expect(await screen.findByText('Release target changed')).toBeVisible();
    expect(screen.getByText(/staged target changed before promotion/i)).toBeVisible();
    expect(screen.getByText(CHANGED_DIGEST)).toBeVisible();
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it('promotes only the exact rechecked staged digest and verifies current state afterward', async () => {
    const promoted = engineFixture({ active: STAGED_DIGEST });
    const fetchMock = mockApi({
      states: [engineFixture(), engineFixture(), promoted],
      posts: [{ ok: true }],
    });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Promote staged release' }));
    await user.click(
      within(screen.getByRole('alertdialog', { name: /promote staged release/i })).getByRole(
        'button',
        { name: 'Promote this digest' },
      ),
    );

    expectPost(fetchMock, '/api/v1/reconstruct/releases/promote', {
      target_digest: STAGED_DIGEST,
      confirm_target: STAGED_DIGEST,
    });
    expect(await screen.findByText(/promotion verified in current engine state/i)).toBeVisible();
  });

  it('reports stage acceptance without attributing a pre-existing staged selector', async () => {
    const verified = engineFixture({
      active: null,
      previous: null,
      staged: STAGED_DIGEST,
      selectedRun: verifiedRun(),
    });
    const fetchMock = mockApi({
      states: [verified, verified, verified],
      posts: [{ ok: true }],
    });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));

    expectPost(fetchMock, '/api/v1/reconstruct/releases/stage', {});
    expect(await screen.findByText('Stage request accepted')).toBeVisible();
    expect(screen.getByText(/response did not identify a staged target/i)).toBeVisible();
    expect(screen.getAllByText(STAGED_DIGEST)).not.toHaveLength(0);
    expect(screen.queryByText(/staged target verified/i)).not.toBeInTheDocument();
  });

  it('does not treat a stale post-stage snapshot as proof and keeps actions disabled', async () => {
    const verified = engineFixture({
      active: null,
      previous: null,
      staged: null,
      selectedRun: verifiedRun(),
    });
    const stale = engineFixture({
      active: null,
      previous: null,
      staged: STAGED_DIGEST,
      selectedRun: verifiedRun(),
      updateError: 'legacy poll timed out',
    });
    const fetchMock = mockApi({
      states: [verified, verified, stale],
      posts: [{ ok: true }],
    });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Stage engine update' }));

    expectPost(fetchMock, '/api/v1/reconstruct/releases/stage', {});
    expect(await screen.findByText('Release result not verified')).toBeVisible();
    expect(screen.queryByText('Stage request accepted')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stage engine update' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('marks a failed confirmation recheck unavailable and sends no action', async () => {
    const fetchMock = mockApi({ states: [engineFixture()] });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Promote staged release' }));
    await user.click(
      within(screen.getByRole('alertdialog', { name: /promote staged release/i })).getByRole(
        'button',
        { name: 'Promote this digest' },
      ),
    );

    expect(await screen.findByText('Current release data unavailable')).toBeVisible();
    expect(screen.getByText('Stale or unavailable')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Promote staged release' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it('rolls back only the exact rechecked previous digest through the fixed action path', async () => {
    const rolledBack = engineFixture({ active: PREVIOUS_DIGEST, previous: ACTIVE_DIGEST });
    const fetchMock = mockApi({
      states: [engineFixture(), engineFixture(), rolledBack],
      posts: [{ ok: true }],
    });
    const user = userEvent.setup();
    render(<Releases />);

    await user.click(await screen.findByRole('button', { name: 'Release controls' }));
    await user.click(screen.getByRole('button', { name: 'Rollback to previous release' }));
    await user.click(
      within(screen.getByRole('alertdialog', { name: /rollback release/i })).getByRole('button', {
        name: 'Roll back',
      }),
    );

    expectPost(fetchMock, '/api/v1/reconstruct/releases/rollback', {
      target_digest: PREVIOUS_DIGEST,
      confirm_target: PREVIOUS_DIGEST,
    });
    expect(await screen.findByText(/rollback verified in current engine state/i)).toBeVisible();
  });
});
