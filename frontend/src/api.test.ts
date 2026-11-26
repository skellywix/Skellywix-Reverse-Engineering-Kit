import { describe, expect, it, vi } from 'vitest';
import { StaleRevisionError, createApiClient, decodeSnapshot } from './api';

const validSnapshot = {
  schema_version: 1,
  cursor: 42,
  generated_at: '2026-11-24T12:00:00Z',
  projects: [
    {
      id: 'p_1',
      kind: 'convert',
      name: 'demo',
      revision: 3,
      created_at: '2026-11-24T11:00:00Z',
    },
  ],
  jobs: [
    {
      id: 'j_1',
      project_id: 'p_1',
      attempt_id: 'a_1',
      attempt: 1,
      status: 'running',
      phase: 'record',
      outcome: null,
      revision: 5,
      created_at: '2026-11-24T11:01:00Z',
      updated_at: '2026-11-24T11:02:00Z',
    },
  ],
  engines: {
    fourmilab: { state: 'up', detail: 'ready' },
    inspector: { state: 'unknown', detail: 'not checked' },
  },
  releases: { active: null, previous: null, staged: null },
};

describe('API runtime decoding', () => {
  it('rejects malformed snapshot payloads', () => {
    expect(() => decodeSnapshot({ ...validSnapshot, cursor: '42' })).toThrow(/snapshot\.cursor/i);
    expect(() =>
      decodeSnapshot({
        ...validSnapshot,
        projects: [{ ...validSnapshot.projects[0], kind: 'unknown' }],
      }),
    ).toThrow(/snapshot\.projects\[0\]\.kind/i);
  });

  it('generates an idempotency key and sends the expected revision', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          project_id: 'p_1',
          job_id: 'j_1',
          revision: 4,
          cursor: 43,
          replayed: false,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const client = createApiClient({ fetchImpl });

    await client.command({
      command: 'start_job',
      project_id: 'p_1',
      job_id: null,
      expected_revision: 3,
      payload: {},
    });

    const request = fetchImpl.mock.calls[0]?.[1];
    expect(request?.headers).toEqual(
      expect.objectContaining({
        'Content-Type': 'application/json',
        'Idempotency-Key': expect.stringMatching(/^[\x21-\x7e]{1,128}$/),
      }),
    );
    expect(JSON.parse(String(request?.body))).toEqual(
      expect.objectContaining({ expected_revision: 3 }),
    );
  });

  it('surfaces stale revisions with the server revision', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'stale_revision',
          message: 'The project changed.',
          current_revision: 7,
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const client = createApiClient({ fetchImpl });

    const request = client.command({
      command: 'cancel_job',
      project_id: 'p_1',
      job_id: 'j_1',
      expected_revision: 5,
      payload: {},
    });

    await expect(request).rejects.toMatchObject<Partial<StaleRevisionError>>({
      name: 'StaleRevisionError',
      currentRevision: 7,
    });
  });
});
