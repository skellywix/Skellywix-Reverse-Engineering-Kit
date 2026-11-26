import { StrictMode } from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';
import Inspect from './Inspect';
import {
  MAX_INSPECT_FILE_SIZE,
  createInspectApiClient,
  type InspectState,
  type InspectStateEnvelope,
} from './inspectApi';

const DIGEST_A = 'a'.repeat(64);
const DIGEST_B = 'b'.repeat(64);
const REFERENCE = 'c'.repeat(64);

const SPECIMEN_A = {
  size: 3,
  sha256: DIGEST_A,
  format: 'elf',
  architecture: 'x86-64',
} as const;

const SPECIMEN_B = {
  size: 5,
  sha256: DIGEST_B,
  format: 'pe',
  architecture: 'arm64',
} as const;

function inspectorState(overrides: Partial<InspectState> = {}): InspectState {
  return {
    schema: 'yuzu.report/1',
    revision: 0,
    slots: {},
    events: [],
    earlier_events_omitted: 0,
    connection: 'off',
    remaining: 0,
    advisor: {
      total: 0,
      mismatches: 0,
      actors: { operator: 0, agent: 0, skellywix: 0 },
      skipped: [],
      critique: { status: 'idle', end: 0 },
    },
    ...overrides,
  };
}

function envelope(
  state: InspectState | null,
  engineState: 'up' | 'down' = state === null ? 'down' : 'up',
): InspectStateEnvelope {
  return {
    schema_version: 1,
    engine: {
      state: engineState,
      detail: engineState === 'up' ? 'available' : 'inspector unavailable',
    },
    state,
    legacy_evidence: state === null ? null : {},
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(typeof input === 'string' ? input : input.toString(), window.location.origin);
}

function requestJson(init?: RequestInit): unknown {
  return JSON.parse(String(init?.body));
}

function installFetch(
  handler: (url: URL, init: RequestInit) => Response | Promise<Response>,
): Mock<typeof fetch> {
  const fetchMock = vi.fn<typeof fetch>(async (input, init = {}) =>
    handler(requestUrl(input), init),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function getFileInput(label: string): HTMLInputElement {
  const input = screen
    .getAllByLabelText(label)
    .find(
      (element): element is HTMLInputElement =>
        element instanceof HTMLInputElement && element.type === 'file',
    );
  if (input === undefined) throw new Error(`No file input found for ${label}`);
  return input;
}

function deferredResponse(): {
  promise: Promise<Response>;
  resolve: (response: Response) => void;
} {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((fulfill) => {
    resolve = fulfill;
  });
  return { promise, resolve };
}

afterEach(() => {
  window.sessionStorage.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('inspect API boundary', () => {
  it('accepts exactly 16 MiB, sends the revision headers, and rejects a larger body before fetch', async () => {
    const updated = inspectorState({ revision: 5, slots: { a: SPECIMEN_A } });
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(updated));
    const client = createInspectApiClient({ fetchImpl });
    const accepted = new File([new Uint8Array([0x7f, 0x45, 0x4c, 0x46])], 'accepted.bin', {
      type: 'application/octet-stream',
    });
    Object.defineProperty(accepted, 'size', { value: MAX_INSPECT_FILE_SIZE });

    await client.upload('a', 4, accepted);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith(
      '/api/v1/inspect/samples/a',
      expect.objectContaining({
        method: 'POST',
        body: accepted,
        headers: expect.objectContaining({
          Accept: 'application/json',
          'Content-Type': 'application/octet-stream',
          'X-Workspace-Revision': '4',
        }),
      }),
    );

    const rejected = new File([new Uint8Array([0])], 'too-large.bin');
    Object.defineProperty(rejected, 'size', { value: MAX_INSPECT_FILE_SIZE + 1 });
    await expect(client.upload('a', 5, rejected)).rejects.toThrow(/16 MiB/i);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it('rejects engine projections outside the binding state envelope', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          ...envelope(null),
          engine: { state: 'unknown', detail: 'indeterminate' },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          ...envelope(null),
          legacy_evidence: {},
        }),
      );
    const client = createInspectApiClient({ fetchImpl });

    await expect(client.getState()).rejects.toThrow(/engine.*state/i);
    await expect(client.getState()).rejects.toThrow(/legacy_evidence/i);
  });

  it('serializes closed run and review request objects', async () => {
    const state = inspectorState({ revision: 3, slots: { a: SPECIMEN_A } });
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ result: SPECIMEN_A, state }))
      .mockResolvedValueOnce(jsonResponse(state));
    const client = createInspectApiClient({ fetchImpl });
    const runRequest = {
      revision: 3,
      tool: 'identify_file' as const,
      arguments: { slot: 'a' as const },
      extra: 'must-not-cross-the-boundary',
    };
    const reviewRequest = {
      revision: 3,
      reference: null,
      critique: false,
      extra: 'must-not-cross-the-boundary',
    };

    await client.run(runRequest);
    await client.review(reviewRequest);

    expect(requestJson(fetchImpl.mock.calls[0]?.[1])).toEqual({
      revision: 3,
      tool: 'identify_file',
      arguments: { slot: 'a' },
    });
    expect(requestJson(fetchImpl.mock.calls[1]?.[1])).toEqual({
      revision: 3,
      reference: null,
      critique: false,
    });
  });

  it('rejects malformed and wrong-schema evidence reports', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('<html>not evidence</html>', { status: 200 }))
      .mockResolvedValueOnce(
        jsonResponse({
          schema: 'unexpected.report/1',
          revision: 1,
          slots: {},
          events: [],
          earlier_events_omitted: 0,
        }),
      );
    const client = createInspectApiClient({ fetchImpl });

    await expect(client.getReport()).rejects.toThrow(/JSON body/i);
    await expect(client.getReport()).rejects.toThrow(/schema/i);
  });

});

describe('passive Inspect workflow', () => {
  it('enforces A before B and accepts replacement A as a clearing response', async () => {
    const user = userEvent.setup();
    let state = inspectorState();
    const fetchMock = installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/samples/a') {
        state = inspectorState({ revision: state.revision + 1, slots: { a: SPECIMEN_A } });
        return jsonResponse(state);
      }
      if (url.pathname === '/api/v1/inspect/samples/b') {
        state = inspectorState({
          revision: state.revision + 1,
          slots: { a: SPECIMEN_A, b: SPECIMEN_B },
        });
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByRole('button', { name: 'Choose specimen A' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Specimen B file' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );

    const a = new File([new Uint8Array([1, 2, 3])], 'a.bin');
    await user.upload(getFileInput('Specimen A file'), a);
    expect(await screen.findByText(/elf · 3 bytes/i)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Specimen B file' })).not.toHaveAttribute(
      'aria-disabled',
    );

    const b = new File([new Uint8Array([4, 5])], 'b.bin');
    await user.upload(getFileInput('Specimen B file'), b);
    expect(await screen.findByText(/pe · 5 bytes/i)).toBeVisible();

    state = inspectorState({
      revision: 2,
      slots: { a: SPECIMEN_A, b: SPECIMEN_B },
      events: [
        { n: 1, actor: 'operator', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
      ],
      advisor: {
        total: 1,
        mismatches: 0,
        actors: { operator: 1, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    await user.click(screen.getByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText('1 entry recorded.')).toBeVisible();
    const replacement = new File([new Uint8Array([1, 2, 3])], 'replacement.bin');
    await user.upload(getFileInput('Specimen A file'), replacement);
    await user.click(await screen.findByRole('button', { name: 'Replace A and clear' }));
    await waitFor(() => expect(screen.getByText('Specimen B is empty.')).toBeVisible());
    expect(screen.getByText('No evidence has been recorded.')).toBeVisible();
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/inspect/samples/a',
      expect.objectContaining({ body: replacement }),
    );
  });

  it('serializes each tool exactly and exposes prepared, checking, and result states', async () => {
    const user = userEvent.setup();
    const state = inspectorState({ revision: 2, slots: { a: SPECIMEN_A, b: SPECIMEN_B } });
    const identifyResponse = deferredResponse();
    const compareResponse = deferredResponse();
    const checksumResponse = deferredResponse();
    const runBodies: unknown[] = [];
    installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/run') {
        const body = requestJson(init) as { tool: string };
        runBodies.push(body);
        if (body.tool === 'identify_file') return identifyResponse.promise;
        if (body.tool === 'compare_builds') return compareResponse.promise;
        return checksumResponse.promise;
      }
      if (url.pathname === '/api/v1/inspect/report') {
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByText('Ready to inspect specimen A.')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Inspect specimen' }));
    expect(screen.getByRole('button', { name: 'Checking' })).toHaveAttribute('aria-disabled', 'true');
    expect(runBodies[0]).toEqual({
      revision: 2,
      tool: 'identify_file',
      arguments: { slot: 'a' },
    });

    await act(async () => {
      identifyResponse.resolve(
        jsonResponse({
          result: SPECIMEN_A,
          state: {
            ...state,
            events: [
              { n: 1, actor: 'operator', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
            ],
          },
        }),
      );
    });
    expect(await screen.findByRole('heading', { name: 'File identity' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Export evidence' })).toBeEnabled();

    await user.click(screen.getByRole('tab', { name: 'Compare files' }));
    expect(screen.getByText('Ready to compare specimens A and B.')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Compare specimens' }));
    expect(screen.getByRole('button', { name: 'Checking' })).toHaveAttribute('aria-disabled', 'true');
    await act(async () => {
      compareResponse.resolve(
        jsonResponse({
          result: {
            identical: false,
            size_delta: 2,
            format_changed: true,
            architecture_changed: true,
          },
          state,
        }),
      );
    });
    expect(await screen.findByRole('heading', { name: 'Files differ' })).toBeVisible();

    await user.click(screen.getByRole('tab', { name: 'Hash check' }));
    await user.type(screen.getByLabelText('Expected SHA-256'), REFERENCE.toUpperCase());
    await user.click(screen.getByRole('button', { name: 'Verify checksum' }));
    expect(screen.getByRole('button', { name: 'Checking' })).toHaveAttribute('aria-disabled', 'true');
    await act(async () => {
      checksumResponse.resolve(
        jsonResponse({
          result: { matches: false, expected: REFERENCE, actual: DIGEST_A },
          state,
        }),
      );
    });
    expect(await screen.findByRole('heading', { name: 'Checksum mismatch' })).toBeVisible();

    expect(runBodies).toEqual([
      { revision: 2, tool: 'identify_file', arguments: { slot: 'a' } },
      { revision: 2, tool: 'compare_builds', arguments: {} },
      {
        revision: 2,
        tool: 'verify_checksum',
        arguments: { slot: 'a', sha256: REFERENCE },
      },
    ]);
  });

  it('refreshes after a revision conflict, retries with the current revision, and hides answers offline', async () => {
    const user = userEvent.setup();
    let stateGets = 0;
    let runs = 0;
    installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') {
        stateGets += 1;
        if (stateGets === 1) {
          return jsonResponse(envelope(inspectorState({ revision: 2, slots: { a: SPECIMEN_A } })));
        }
        if (stateGets === 2) {
          return jsonResponse(envelope(inspectorState({ revision: 3, slots: { a: SPECIMEN_A } })));
        }
        return jsonResponse(envelope(null));
      }
      if (url.pathname === '/api/v1/inspect/run') {
        runs += 1;
        if (runs === 1) {
          return jsonResponse(
            {
              error: 'stale_revision',
              message: 'The files changed. Refresh the case and try again.',
              current_revision: 3,
            },
            409,
          );
        }
        expect(requestJson(init)).toEqual({
          revision: 3,
          tool: 'identify_file',
          arguments: { slot: 'a' },
        });
        return jsonResponse({
          result: SPECIMEN_A,
          state: inspectorState({ revision: 3, slots: { a: SPECIMEN_A } }),
        });
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Inspect specimen' }));
    expect(await screen.findByText(/files changed/i)).toBeVisible();
    expect(await screen.findByText('Revision 3')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Retry check' }));
    expect(await screen.findByRole('heading', { name: 'File identity' })).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText(/inspector is offline/i)).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'File identity' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry check' })).toBeEnabled();
  });

  it('polls passive state changes and hides prior answers when the engine drops', async () => {
    let currentEnvelope = envelope(
      inspectorState({ revision: 5, slots: { a: SPECIMEN_A } }),
    );
    let poll: (() => void) | undefined;
    const intervalSpy = vi
      .spyOn(window, 'setInterval')
      .mockImplementation((callback, delay) => {
        if (typeof callback === 'function' && delay === 2_200) {
          poll = callback as () => void;
        }
        return 1;
      });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(currentEnvelope);
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByText('Revision 5')).toBeVisible();
    expect(poll).toBeTypeOf('function');
    expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), 2_200);
    currentEnvelope = envelope(null);
    await act(async () => {
      poll?.();
      await Promise.resolve();
    });
    expect(await screen.findByText(/inspector is offline/i)).toBeVisible();
    expect(screen.getByText(/Ledger unavailable while offline\./)).toBeVisible();
    expect(screen.queryAllByText(/Specimen [AB] is empty\./)).toHaveLength(0);
    expect(screen.getByText(/Files unavailable while offline\./)).toBeVisible();
    expect(screen.queryByText('No evidence has been recorded.')).not.toBeInTheDocument();
  });

  it('replaces a local result with the latest matching event from passive state', async () => {
    const user = userEvent.setup();
    const firstEvent = {
      n: 1,
      actor: 'operator' as const,
      tool: 'identify_file' as const,
      slot: 'a' as const,
      result: SPECIMEN_A,
    };
    let currentEnvelope = envelope(
      inspectorState({ revision: 5, slots: { a: SPECIMEN_A } }),
    );
    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((callback, delay) => {
      if (typeof callback === 'function' && delay === 2_200) {
        poll = callback as () => void;
      }
      return 1;
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(currentEnvelope);
      if (url.pathname === '/api/v1/inspect/run') {
        const next = inspectorState({
          revision: 5,
          slots: { a: SPECIMEN_A },
          events: [firstEvent],
        });
        currentEnvelope = envelope(next);
        return jsonResponse({ result: SPECIMEN_A, state: next });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Inspect specimen' }));
    expect(await screen.findByRole('heading', { name: 'File identity' })).toBeVisible();
    expect(await screen.findByRole('button', { name: 'Export evidence' })).toBeEnabled();
    currentEnvelope = envelope(
      inspectorState({
        revision: 5,
        slots: { a: SPECIMEN_A },
        events: [
          firstEvent,
          {
            ...firstEvent,
            n: 2,
            actor: 'agent',
            result: { ...SPECIMEN_A, format: 'PE' },
          },
        ],
      }),
    );
    await act(async () => {
      poll?.();
      await Promise.resolve();
    });

    const currentCheck = screen.getByRole('heading', { name: 'Current check' }).parentElement;
    expect(currentCheck).not.toBeNull();
    await waitFor(() => expect(within(currentCheck!).getByText('PE')).toBeVisible());
  });

  it('announces critic outcomes received through passive polling', async () => {
    const event = {
      n: 1,
      actor: 'operator' as const,
      tool: 'identify_file' as const,
      slot: 'a' as const,
      result: SPECIMEN_A,
    };
    let currentEnvelope = envelope(
      inspectorState({
        revision: 6,
        slots: { a: SPECIMEN_A },
        events: [event],
        advisor: {
          total: 1,
          mismatches: 0,
          actors: { operator: 1, agent: 0, skellywix: 0 },
          skipped: [],
          critique: { status: 'pending', end: 1 },
        },
      }),
    );
    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((callback, delay) => {
      if (typeof callback === 'function' && delay === 2_200) {
        poll = callback as () => void;
      }
      return 1;
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(currentEnvelope);
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);
    expect(await screen.findByText('Revision 6')).toBeVisible();

    currentEnvelope = envelope(
      inspectorState({
        revision: 6,
        slots: { a: SPECIMEN_A },
        events: [event],
        advisor: {
          total: 1,
          mismatches: 0,
          actors: { operator: 1, agent: 0, skellywix: 0 },
          skipped: [],
          critique: {
            status: 'ready',
            end: 1,
            text: 'The retained finding is internally consistent.',
            cites: [1],
          },
        },
      }),
    );
    await act(async () => {
      poll?.();
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Inspection updates' })).toHaveTextContent(
        'Model critique is ready.',
      ),
    );
  });

  it('keeps initial controls locked until the current StrictMode request settles', async () => {
    const first = deferredResponse();
    const second = deferredResponse();
    let reads = 0;
    installFetch((url) => {
      if (url.pathname !== '/api/v1/inspect/state') {
        throw new Error(`Unexpected request: ${url.pathname}`);
      }
      reads += 1;
      return reads === 1 ? first.promise : second.promise;
    });
    render(
      <StrictMode>
        <Inspect />
      </StrictMode>,
    );

    await waitFor(() => expect(reads).toBe(2));
    expect(screen.queryByRole('button', { name: 'Checking' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry check' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    await act(async () => {
      first.resolve(
        jsonResponse(
          envelope(inspectorState({ revision: 1, slots: { a: SPECIMEN_A } })),
        ),
      );
    });
    expect(screen.getByRole('button', { name: 'Retry check' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    await act(async () => {
      second.resolve(
        jsonResponse(
          envelope(inspectorState({ revision: 2, slots: { a: SPECIMEN_A } })),
        ),
      );
    });
    expect(await screen.findByRole('button', { name: 'Inspect specimen' })).toBeEnabled();
    expect(screen.getByText('Revision 2')).toBeVisible();
  });

  it('uses the retry action to restore state after a run transport failure', async () => {
    const user = userEvent.setup();
    let stateReads = 0;
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') {
        stateReads += 1;
        return jsonResponse(
          envelope(inspectorState({ revision: 2, slots: { a: SPECIMEN_A } })),
        );
      }
      if (url.pathname === '/api/v1/inspect/run') {
        throw new TypeError('connection interrupted');
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Inspect specimen' }));
    expect(await screen.findByText(/inspector is offline/i)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Retry check' }));
    await waitFor(() => expect(stateReads).toBe(2));
    expect(await screen.findByRole('button', { name: 'Inspect specimen' })).toBeEnabled();
  });

  it('falls back to the current prerequisite when stale recovery cleared a specimen', async () => {
    const user = userEvent.setup();
    let reads = 0;
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') {
        reads += 1;
        return jsonResponse(
          envelope(
            reads === 1
              ? inspectorState({ revision: 2, slots: { a: SPECIMEN_A } })
              : inspectorState({ revision: 3 }),
          ),
        );
      }
      if (url.pathname === '/api/v1/inspect/run') {
        return jsonResponse(
          {
            error: 'stale_revision',
            message: 'The files changed. Refresh the case and try again.',
            current_revision: 3,
          },
          409,
        );
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Inspect specimen' }));
    expect(await screen.findByText('Revision 3')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Choose specimen A' })).toBeEnabled();
  });

  it('retains the non-secret mode, specimen selection, and checksum draft in the tab', async () => {
    const user = userEvent.setup();
    const state = inspectorState({
      revision: 4,
      slots: { a: SPECIMEN_A, b: SPECIMEN_B },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    const first = render(<Inspect />);

    await user.click(await screen.findByRole('tab', { name: 'Hash check' }));
    await user.click(screen.getByRole('combobox', { name: 'Specimen' }));
    await user.click(screen.getByRole('option', { name: 'Specimen B' }));
    await user.type(screen.getByLabelText('Expected SHA-256'), REFERENCE);
    first.unmount();
    render(<Inspect />);

    expect(await screen.findByRole('tab', { name: 'Hash check' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('combobox', { name: 'Specimen' })).toHaveTextContent('Specimen B');
    expect(screen.getByLabelText('Expected SHA-256')).toHaveValue(REFERENCE);
  });

  it('restores current contract results with absolute sizes and a verified check icon', async () => {
    const user = userEvent.setup();
    const state = inspectorState({
      revision: 6,
      slots: { a: SPECIMEN_A },
      events: [
        { n: 1, actor: 'operator', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
        {
          n: 2,
          actor: 'operator',
          tool: 'verify_checksum',
          slot: 'a',
          result: { matches: true, expected: DIGEST_A, actual: DIGEST_A },
        },
      ],
      advisor: {
        total: 2,
        mismatches: 0,
        actors: { operator: 2, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/report') {
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByRole('heading', { name: 'File identity' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Export evidence' })).toBeEnabled();
    expect(screen.queryByText('+3 bytes')).not.toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Hash check' }));
    await user.type(screen.getByLabelText('Expected SHA-256'), DIGEST_A);
    const verified = await screen.findByRole('heading', { name: 'Checksum matches reference' });
    expect(verified.querySelector('.astryx-icon')).toBeInTheDocument();
  });

  it('ignores an action response from an older case revision', async () => {
    const user = userEvent.setup();
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') {
        return jsonResponse(envelope(inspectorState({ revision: 4, slots: { a: SPECIMEN_A } })));
      }
      if (url.pathname === '/api/v1/inspect/run') {
        return jsonResponse({
          result: SPECIMEN_A,
          state: inspectorState({ revision: 3, slots: { a: SPECIMEN_A } }),
        });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Inspect specimen' }));
    expect(await screen.findByRole('status', { name: 'Inspection updates' })).toHaveTextContent(
      /ignored a stale response/i,
    );
    expect(screen.getByText('Revision 4')).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'File identity' })).not.toBeInTheDocument();
  });

  it('ignores a delayed poll response after a newer same-revision action', async () => {
    const user = userEvent.setup();
    const initial = inspectorState({ revision: 4, slots: { a: SPECIMEN_A } });
    const newerResult = { ...SPECIMEN_A, format: 'PE' };
    const newer = inspectorState({
      revision: 4,
      slots: { a: SPECIMEN_A },
      events: [
        {
          n: 1,
          actor: 'operator',
          tool: 'identify_file',
          slot: 'a',
          result: newerResult,
        },
      ],
    });
    const delayedPoll = deferredResponse();
    let stateReads = 0;
    let poll: (() => void) | undefined;
    vi.spyOn(window, 'setInterval').mockImplementation((callback, delay) => {
      if (typeof callback === 'function' && delay === 2_200) {
        poll = callback as () => void;
      }
      return 1;
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') {
        stateReads += 1;
        return stateReads === 1
          ? jsonResponse(envelope(initial))
          : delayedPoll.promise;
      }
      if (url.pathname === '/api/v1/inspect/run') {
        return jsonResponse({ result: newerResult, state: newer });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByRole('button', { name: 'Inspect specimen' })).toBeEnabled();
    await act(async () => {
      poll?.();
      await Promise.resolve();
    });
    expect(stateReads).toBe(2);
    await user.click(screen.getByRole('button', { name: 'Inspect specimen' }));
    const currentCheck = screen.getByRole('heading', { name: 'Current check' }).parentElement;
    expect(currentCheck).not.toBeNull();
    expect(await within(currentCheck!).findByText('PE')).toBeVisible();
    expect(await screen.findByRole('button', { name: 'Export evidence' })).toBeEnabled();

    await act(async () => {
      delayedPoll.resolve(jsonResponse(envelope(initial)));
      await Promise.resolve();
    });

    expect(screen.getByRole('status', { name: 'Inspection updates' })).toHaveTextContent(
      /ignored a stale response/i,
    );
    expect(within(currentCheck!).getByText('PE')).toBeVisible();
  });

  it('exports only on request and requires explicit reset confirmation', async () => {
    const user = userEvent.setup();
    let state = inspectorState({ revision: 1, slots: { a: SPECIMEN_A } });
    const report = {
      schema: 'yuzu.report/1',
      revision: 1,
      slots: { a: SPECIMEN_A },
      events: [],
      earlier_events_omitted: 0,
    };
    const requests: Array<{ path: string; body: unknown }> = [];
    installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/report') {
        requests.push({ path: url.pathname, body: null });
        return jsonResponse(report);
      }
      if (url.pathname === '/api/v1/inspect/reset') {
        requests.push({ path: url.pathname, body: requestJson(init) });
        state = inspectorState({ revision: 2 });
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    const createObjectUrl = vi.fn<(blob: Blob) => string>(() => 'blob:inspect-report');
    const revokeObjectUrl = vi.fn();
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: createObjectUrl },
      revokeObjectURL: { configurable: true, value: revokeObjectUrl },
    });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('button', { name: 'Export evidence (JSON)' }));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(createObjectUrl).toHaveBeenCalledWith(expect.any(Blob));
    const downloaded = createObjectUrl.mock.calls[0]?.[0];
    expect(downloaded).toBeInstanceOf(Blob);
    expect(JSON.parse(await downloaded!.text())).toEqual(report);
    expect(requests).toEqual([{ path: '/api/v1/inspect/report', body: null }]);

    await user.click(screen.getByRole('button', { name: 'Reset case' }));
    expect(screen.getByText(/clear both specimens and the evidence ledger/i)).toBeVisible();
    expect(requests).toHaveLength(1);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Confirm reset' })).toHaveFocus(),
    );
    await user.click(screen.getByRole('button', { name: 'Keep case' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reset case' })).toHaveFocus());
    await user.click(screen.getByRole('button', { name: 'Reset case' }));
    await user.click(screen.getByRole('button', { name: 'Confirm reset' }));
    await waitFor(() => expect(screen.getByText('Specimen A is empty.')).toBeVisible());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Refresh inspector' })).toHaveFocus(),
    );
    expect(requests[1]).toEqual({ path: '/api/v1/inspect/reset', body: { revision: 1 } });
  });

  it('does not download a delayed report after the Inspect view unmounts', async () => {
    const user = userEvent.setup();
    const state = inspectorState({ revision: 2, slots: { a: SPECIMEN_A } });
    const delayedReport = deferredResponse();
    let reportRequested = false;
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/report') {
        reportRequested = true;
        return delayedReport.promise;
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    const createObjectUrl = vi.fn(() => 'blob:inspect-report');
    const revokeObjectUrl = vi.fn();
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: createObjectUrl },
      revokeObjectURL: { configurable: true, value: revokeObjectUrl },
    });
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);
    const view = render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('button', { name: 'Export evidence (JSON)' }));
    await waitFor(() => expect(reportRequested).toBe(true));
    view.unmount();
    await act(async () => {
      delayedReport.resolve(jsonResponse(state));
      await Promise.resolve();
    });

    expect(createObjectUrl).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it('shows case-key, review, and critic states without persisting or placing the key in a URL', async () => {
    const user = userEvent.setup();
    const caseKey = 'private-case-key';
    let state = inspectorState({ revision: 7, slots: { a: SPECIMEN_A } });
    const writes: Array<{ path: string; body: unknown }> = [];
    installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      writes.push({ path: url.pathname, body: requestJson(init) });
      if (url.pathname === '/api/v1/inspect/pair') {
        state = inspectorState({
          revision: 7,
          slots: { a: SPECIMEN_A },
          connection: 'ready',
          remaining: 32,
        });
        return jsonResponse({ token: caseKey, state });
      }
      if (url.pathname === '/api/v1/inspect/disconnect') {
        state = inspectorState({ revision: 7, slots: { a: SPECIMEN_A } });
        return jsonResponse(state);
      }
      if (url.pathname === '/api/v1/inspect/review') {
        state = inspectorState({
          revision: 7,
          slots: { a: SPECIMEN_A },
          events: [
            { n: 1, actor: 'skellywix', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
          ],
          advisor: {
            total: 1,
            mismatches: 0,
            actors: { operator: 0, agent: 0, skellywix: 1 },
            skipped: ['compare_builds'],
            critique: { status: 'pending', end: 1 },
          },
        });
        return jsonResponse(state);
      }
      if (url.pathname === '/api/v1/inspect/critic/cancel') {
        state = {
          ...state,
          advisor: {
            ...state.advisor,
            critique: { status: 'cancelled', end: 1 },
          },
        };
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem');
    render(<Inspect />);

    await user.click(await screen.findByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('button', { name: 'Pair read-only agent' }));
    expect(await screen.findByText('Key issued · awaiting agent')).toBeVisible();
    expect(screen.getByText('32 tool calls remaining.')).toBeVisible();
    expect(screen.queryByDisplayValue(caseKey)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Reveal case key' }));
    expect(screen.getByDisplayValue(caseKey)).toHaveAttribute('readonly');
    expect(window.location.href).not.toContain(caseKey);
    expect(storageSpy).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Revoke case key' }));
    expect(await screen.findByText('No key issued')).toBeVisible();
    await user.type(screen.getByRole('textbox', { name: /Reference SHA-256 for A/ }), REFERENCE);
    await user.click(screen.getByRole('checkbox', { name: 'Request a model critique' }));
    await user.click(screen.getByRole('button', { name: 'Run file checks' }));
    expect(
      await screen.findByText(/No critic connected yet\. No model note\./),
    ).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Cancel critique' }));
    expect(await screen.findByText('Critique cancelled.')).toBeVisible();

    expect(writes).toEqual([
      { path: '/api/v1/inspect/pair', body: { revision: 7 } },
      { path: '/api/v1/inspect/disconnect', body: { revision: 7 } },
      {
        path: '/api/v1/inspect/review',
        body: { revision: 7, reference: REFERENCE, critique: true },
      },
      { path: '/api/v1/inspect/critic/cancel', body: { revision: 7, end: 1 } },
    ]);
  });

  it('renders key expiry, exhaustion, call counts, and every non-ready critic state', async () => {
    const user = userEvent.setup();
    let state = inspectorState({
      revision: 9,
      slots: { a: SPECIMEN_A },
      connection: 'ready',
      remaining: 32,
      advisor: {
        total: 0,
        mismatches: 0,
        actors: { operator: 0, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'reviewing', end: 1 },
      },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);
    await user.click(await screen.findByRole('button', { name: 'Advanced inspection controls' }));

    expect(screen.getByText('Key issued · awaiting agent')).toBeVisible();
    expect(screen.getByText('32 tool calls remaining.')).toBeVisible();
    expect(
      screen.getByText('The critic is reading this ledger snapshot. No model note yet.'),
    ).toBeVisible();

    state = {
      ...state,
      connection: 'connected',
      remaining: 5,
      advisor: { ...state.advisor, critique: { status: 'failed', end: 1 } },
    };
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText('Agent paired')).toBeVisible();
    expect(screen.getByText('5 tool calls remaining.')).toBeVisible();
    expect(screen.getByText(/Critique not completed: the critic declined/)).toBeVisible();

    state = {
      ...state,
      connection: 'spent',
      remaining: 0,
      advisor: { ...state.advisor, critique: { status: 'expired', end: 1 } },
    };
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText('Key spent')).toBeVisible();
    expect(
      screen.getByText('Critique request expired before a note arrived. No note is current.'),
    ).toBeVisible();

    state = {
      ...state,
      connection: 'expired',
      advisor: { ...state.advisor, critique: { status: 'stale', end: 1 } },
    };
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText('Key expired')).toBeVisible();
    expect(
      screen.getByText('The ledger changed. The previous model note no longer applies.'),
    ).toBeVisible();

    state = {
      ...state,
      connection: 'off',
      advisor: { ...state.advisor, critique: { status: 'not_requested', end: 1 } },
    };
    await user.click(screen.getByRole('button', { name: 'Refresh inspector' }));
    expect(await screen.findByText('No key issued')).toBeVisible();
    expect(screen.getByText('Pass logged. No model critique requested.')).toBeVisible();
  });

  it('renders retained ledger evidence, omission counts, and a cited ready critique', async () => {
    const user = userEvent.setup();
    const state = inspectorState({
      revision: 8,
      slots: { a: SPECIMEN_A },
      events: [
        { n: 8, actor: 'operator', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
        {
          n: 9,
          actor: 'operator',
          tool: 'verify_checksum',
          slot: 'a',
          result: { matches: false, expected: REFERENCE, actual: DIGEST_A },
        },
      ],
      earlier_events_omitted: 7,
      advisor: {
        total: 9,
        mismatches: 2,
        actors: { operator: 9, agent: 0, skellywix: 0 },
        skipped: [],
        critique: {
          status: 'ready',
          end: 9,
          text: 'The trusted reference does not match specimen A.',
          cites: [9],
        },
      },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByText('7 earlier entries are counted but no longer retained.')).toBeVisible();
    expect(screen.getByText('2 checksum mismatches recorded.')).toBeVisible();
    expect(screen.getByText('The trusted reference does not match specimen A.')).toBeVisible();
    await user.click(screen.getByText('Hash check · A · You · #9'));
    expect(screen.getByText('Checksum mismatch')).toBeVisible();
    const critique = screen.getByRole('region', { name: 'Model critique' });
    expect(within(critique).getByRole('link', { name: '#9' })).toHaveAttribute(
      'href',
      '#inspect-finding-8-9',
    );
  });
  it('shows the comparison verdict in an expanded ledger entry', async () => {
    const user = userEvent.setup();
    const state = inspectorState({
      revision: 9,
      slots: {
        a: { ...SPECIMEN_A, format: 'png', architecture: 'unknown' },
        b: { ...SPECIMEN_B, format: 'zip', architecture: 'unknown' },
      },
      events: [
        {
          n: 10,
          actor: 'operator',
          tool: 'compare_builds',
          result: {
            identical: false,
            size_delta: 2,
            format_changed: true,
            architecture_changed: false,
          },
        },
      ],
      advisor: {
        total: 1,
        mismatches: 0,
        actors: { operator: 1, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    await user.click(await screen.findByText('Compare files · You · #10'));
    expect(screen.getByText('Files differ')).toBeVisible();
    expect(screen.getByText('Cannot compare: architecture unknown')).toBeVisible();
  });

  it('loads into an empty ledger directly and keeps the file name out of every JSON request', async () => {
    const user = userEvent.setup();
    let state = inspectorState({ revision: 1 });
    const jsonBodies: string[] = [];
    const fetchMock = installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/samples/a') {
        state = inspectorState({ revision: 2, slots: { a: SPECIMEN_A } });
        return jsonResponse(state);
      }
      if (url.pathname === '/api/v1/inspect/review') {
        jsonBodies.push(String(init.body), JSON.stringify(init.headers));
        state = inspectorState({
          revision: 2,
          slots: { a: SPECIMEN_A },
          events: [
            { n: 1, actor: 'skellywix', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
          ],
          advisor: {
            total: 1,
            mismatches: 0,
            actors: { operator: 0, agent: 0, skellywix: 1 },
            skipped: ['compare_builds'],
            critique: { status: 'pending', end: 1 },
          },
        });
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    render(<Inspect />);
    await screen.findByRole('button', { name: 'Choose specimen A' });

    const file = new File([new Uint8Array([1, 2, 3])], 'C:\\cases\\private-name.bin');
    await user.upload(getFileInput('Specimen A file'), file);

    expect(await screen.findByText('Loaded: private-name.bin')).toBeVisible();
    expect(screen.queryByRole('heading', { name: /Replace specimen/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/inspect/samples/a',
      expect.objectContaining({ body: file }),
    );

    await user.click(screen.getByRole('button', { name: 'Advanced inspection controls' }));
    await user.click(screen.getByRole('checkbox', { name: 'Request a model critique' }));
    await user.click(screen.getByRole('button', { name: 'Run file checks' }));
    expect(await screen.findByText(/No critic connected yet\. No model note\./)).toBeVisible();
    expect(jsonBodies.join(' ')).not.toContain('private-name');
    expect(screen.getByText('By actor: Skellywix 1.')).toBeVisible();
  });

  it('guards a populated ledger with the measured loss and lets the operator keep it', async () => {
    const user = userEvent.setup();
    const state = inspectorState({
      revision: 4,
      slots: { a: SPECIMEN_A, b: SPECIMEN_B },
      events: [
        {
          n: 3,
          actor: 'operator',
          tool: 'verify_checksum',
          slot: 'a',
          result: { matches: false, expected: REFERENCE, actual: DIGEST_A },
        },
      ],
      earlier_events_omitted: 2,
      connection: 'ready',
      remaining: 30,
      advisor: {
        total: 3,
        mismatches: 1,
        actors: { operator: 3, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    const fetchMock = installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    render(<Inspect />);
    await screen.findByText('Revision 4');

    await user.upload(
      getFileInput('Specimen A file'),
      new File([new Uint8Array([9])], 'replacement.bin'),
    );

    const guard = await screen.findByRole('region', { name: 'Replace specimen A?' });
    const detail = within(guard).getByText(/will clear:/);
    expect(detail).toHaveTextContent('3 ledger entries');
    expect(detail).toHaveTextContent('1 recorded checksum mismatch');
    expect(detail).toHaveTextContent('specimen B');
    expect(detail).toHaveTextContent('read-only agent key');
    expect(within(guard).getByText(/2 earlier entries are counted but not retained/)).toBeVisible();
    expect(screen.getByText('Selected: replacement.bin · not loaded yet')).toBeVisible();
    expect(within(guard).getByRole('button', { name: 'Keep case' })).toHaveFocus();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/samples/'))).toBe(
      false,
    );

    await user.click(within(guard).getByRole('button', { name: 'Keep case' }));
    expect(screen.queryByRole('region', { name: 'Replace specimen A?' })).not.toBeInTheDocument();
    expect(screen.queryByText(/replacement\.bin/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/samples/'))).toBe(
      false,
    );
  });

  it('exports before replacing and does not replace when the export fails', async () => {
    const user = userEvent.setup();
    let state = inspectorState({
      revision: 4,
      slots: { a: SPECIMEN_A },
      events: [
        { n: 1, actor: 'operator', tool: 'identify_file', slot: 'a', result: SPECIMEN_A },
      ],
      advisor: {
        total: 1,
        mismatches: 0,
        actors: { operator: 1, agent: 0, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    const order: string[] = [];
    let reportOk = false;
    installFetch((url, init) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      if (url.pathname === '/api/v1/inspect/report') {
        order.push('report');
        return reportOk
          ? jsonResponse(state)
          : jsonResponse({ error: 'internal', message: 'report failed' }, 500);
      }
      if (url.pathname === '/api/v1/inspect/samples/a') {
        order.push('upload');
        state = inspectorState({ revision: 5, slots: { a: SPECIMEN_B } });
        return jsonResponse(state);
      }
      throw new Error(`Unexpected request: ${init.method ?? 'GET'} ${url.pathname}`);
    });
    vi.stubGlobal('URL', class extends URL {
      static override createObjectURL = vi.fn(() => 'blob:test');
      static override revokeObjectURL = vi.fn();
    });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<Inspect />);
    await screen.findByText('Revision 4');

    await user.upload(getFileInput('Specimen A file'), new File([new Uint8Array([9])], 'next.bin'));
    let guard = await screen.findByRole('region', { name: 'Replace specimen A?' });
    await user.click(within(guard).getByRole('button', { name: 'Export evidence, then replace' }));
    await waitFor(() => expect(order).toEqual(['report']));
    expect(order).not.toContain('upload');
    expect(screen.getByRole('region', { name: 'Replace specimen A?' })).toBeVisible();

    reportOk = true;
    guard = screen.getByRole('region', { name: 'Replace specimen A?' });
    await user.click(within(guard).getByRole('button', { name: 'Export evidence, then replace' }));
    await waitFor(() => expect(order).toEqual(['report', 'report', 'upload']));
    expect(await screen.findByText('Loaded: next.bin')).toBeVisible();
  });

  it('keeps the ledger mismatch history and actor counts visible after a later match', async () => {
    const state = inspectorState({
      revision: 6,
      slots: { a: SPECIMEN_A },
      events: [
        {
          n: 2,
          actor: 'agent',
          tool: 'verify_checksum',
          slot: 'a',
          result: { matches: true, expected: DIGEST_A, actual: DIGEST_A },
        },
      ],
      advisor: {
        total: 2,
        mismatches: 1,
        actors: { operator: 1, agent: 1, skellywix: 0 },
        skipped: [],
        critique: { status: 'idle', end: 0 },
      },
    });
    installFetch((url) => {
      if (url.pathname === '/api/v1/inspect/state') return jsonResponse(envelope(state));
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    render(<Inspect />);

    expect(await screen.findByText('By actor: You 1 · Paired agent 1.')).toBeVisible();
    expect(screen.getByText(/1 checksum mismatch recorded\./)).toHaveTextContent(
      'A later match does not erase an earlier mismatch in this case.',
    );
  });

});
