import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Runtime from './Runtime';
import { createSourcePortClient } from './sourcePortApi';

const RECIPE_SHA = '1'.repeat(64);
const TOOLCHAIN_SHA = '2'.repeat(64);
const STATE = 'GET /api/v1/runtime/state';

interface Call {
  method: string;
  path: string;
}

type Handler = () => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

class FakeServer {
  readonly calls: Call[] = [];
  readonly unexpected: string[] = [];
  #handler: Handler;

  constructor(handler: Handler) {
    this.#handler = handler;
  }

  respond(handler: Handler): void {
    this.#handler = handler;
  }

  readonly fetch: typeof fetch = async (input, init) => {
    const call: Call = { method: init?.method ?? 'GET', path: String(input) };
    this.calls.push(call);
    if (`${call.method} ${call.path}` !== STATE) {
      this.unexpected.push(`${call.method} ${call.path}`);
      return json({ error: 'not_found', message: 'Unexpected request.' }, 404);
    }
    return this.#handler();
  };
}

function recipe(overrides: Record<string, unknown> = {}) {
  return {
    project_id: 'line-count',
    name: 'Line count',
    recipe_sha256: RECIPE_SHA,
    toolchain_sha256: TOOLCHAIN_SHA,
    state: 'configured',
    detail: 'No build recorded.',
    ...overrides,
  };
}

function runtimeBody(recipes: unknown[]): unknown {
  return { schema_version: 1, recipes };
}

function renderRuntime(server: FakeServer): void {
  const client = createSourcePortClient({ fetchImpl: server.fetch });
  render(
    <MemoryRouter>
      <Runtime client={client} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Runtime: read-only state', () => {
  it('reads the state once on mount and offers no command, input or console', async () => {
    const server = new FakeServer(() => json(runtimeBody([recipe()])));
    renderRuntime(server);
    expect(await screen.findByRole('heading', { level: 2, name: 'Line count' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Runtime' })).toBeInTheDocument();
    expect(server.calls).toEqual([{ method: 'GET', path: '/api/v1/runtime/state' }]);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeInTheDocument();
    expect(server.unexpected).toEqual([]);
  });

  it('never presents a configured recipe as exercised', async () => {
    const server = new FakeServer(() => json(runtimeBody([recipe()])));
    renderRuntime(server);
    expect(await screen.findByText(/^Set up, not tested/)).toBeInTheDocument();
    expect(screen.queryByText(/^Test result recorded/)).not.toBeInTheDocument();
    expect(screen.getByText(/Set up only: 1\. Result recorded: 0/)).toBeInTheDocument();
  });

  it('shows exercised, unavailable and failed states explicitly with full identities', async () => {
    const server = new FakeServer(() =>
      json(
        runtimeBody([
          recipe({ state: 'exercised', detail: 'All 3 cases matched.' }),
          recipe({ project_id: 'b', name: 'Beta', state: 'unavailable', detail: 'Engine missing.' }),
          recipe({ project_id: 'c', name: 'Gamma', state: 'failed', detail: '<b>boom</b>' }),
        ]),
      ),
    );
    renderRuntime(server);
    expect(await screen.findByText(/^Test result recorded\./)).toBeInTheDocument();
    expect(screen.getByText(/^Unavailable\./)).toBeInTheDocument();
    expect(screen.getByText(/^Failed\./)).toBeInTheDocument();
    expect(screen.getByText(/Result recorded: 2\..*Failed: 1\./)).toBeVisible();
    expect(screen.getByText('<b>boom</b>')).toBeInTheDocument();
    expect(document.querySelector('b')).toBeNull();
    expect(screen.getAllByText(RECIPE_SHA)).toHaveLength(3);
    expect(screen.getAllByText(TOOLCHAIN_SHA)).toHaveLength(3);
  });

  it('says so when no recipe is configured and when a detail is missing', async () => {
    const server = new FakeServer(() => json(runtimeBody([])));
    renderRuntime(server);
    expect(await screen.findByText('No test tools are set up.')).toBeInTheDocument();

    const second = new FakeServer(() => json(runtimeBody([recipe({ detail: '' })])));
    renderRuntime(second);
    expect(await screen.findByText('No detail recorded.')).toBeInTheDocument();
  });

  it('rejects an unknown state value instead of guessing', async () => {
    const server = new FakeServer(() => json(runtimeBody([recipe({ state: 'running' })])));
    renderRuntime(server);
    expect(await screen.findByRole('alert')).toHaveTextContent(/did not match the contract/);
    expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument();
  });
});

describe('Runtime: offline and refresh', () => {
  it('shows no results while unreachable and retries with a read only', async () => {
    const user = userEvent.setup();
    const server = new FakeServer(() => {
      throw new TypeError('Failed to fetch');
    });
    renderRuntime(server);
    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot be reached/i);
    expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument();

    server.respond(() => json(runtimeBody([recipe({ state: 'exercised' })])));
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/^Test result recorded\./)).toBeInTheDocument();
    expect(server.calls.every((call) => call.method === 'GET')).toBe(true);
  });

  it('hides a stale successful result when the browser goes offline and re-reads on reconnect', async () => {
    const server = new FakeServer(() => json(runtimeBody([recipe({ state: 'exercised' })])));
    renderRuntime(server);
    expect(await screen.findByText(/^Test result recorded\./)).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot be reached/i);
    expect(screen.queryByText(/^Test result recorded\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/Runtime state loaded/)).not.toBeInTheDocument();

    const before = server.calls.length;
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expect(await screen.findByText(/^Test result recorded\./)).toBeInTheDocument();
    expect(server.calls.length).toBeGreaterThan(before);
    expect(server.calls.every((call) => call.method === 'GET')).toBe(true);
  });

  it('keeps the newest read when an older response arrives late', async () => {
    let release: (response: Response) => void = () => undefined;
    const late = new Promise<Response>((resolve) => {
      release = resolve;
    });
    let reads = 0;
    const server = new FakeServer(() => {
      reads += 1;
      return reads === 1 ? late : json(runtimeBody([recipe({ state: 'exercised' })]));
    });
    renderRuntime(server);
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expect(await screen.findByText(/^Test result recorded\./)).toBeInTheDocument();

    await act(async () => {
      release(json(runtimeBody([recipe({ state: 'configured' })])));
      await late;
    });
    expect(screen.getByText(/^Test result recorded\./)).toBeInTheDocument();
    expect(screen.queryByText(/^Set up, not tested/)).not.toBeInTheDocument();
  });

  it('has one primary action and one live region of its own', async () => {
    const server = new FakeServer(() => json(runtimeBody([recipe()])));
    renderRuntime(server);
    await screen.findByText(/^Set up, not tested/);
    expect(document.querySelectorAll('[data-primary-action="true"]')).toHaveLength(1);
    expect(
      screen.getAllByRole('status').filter((region) => region instanceof HTMLOutputElement),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Verifier recipes' })).toBeInTheDocument();
  });
});
