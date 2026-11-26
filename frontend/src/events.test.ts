import { describe, expect, it, vi } from 'vitest';
import { WorkspaceEventsClient, decodeWorkspaceEvent } from './events';
import type { EventSourceFactory, EventSourceLike } from './events';
import type { WorkspaceEvent, WorkspaceSnapshot } from './types';

class FakeEventSource implements EventSourceLike {
  readonly listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>();
  readonly close = vi.fn();

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, data: unknown, lastEventId = ''): void {
    const event = new MessageEvent<string>(type, {
      data: JSON.stringify(data),
      lastEventId,
    });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

const event = (eventId: number): WorkspaceEvent =>
  decodeWorkspaceEvent({
    schema_version: 1,
    event_id: eventId,
    project_id: 'p_1',
    job_id: 'j_1',
    attempt_id: 'a_1',
    revision: eventId,
    phase: 'record',
    type: 'job.status_changed',
    at: '2026-11-24T12:00:00Z',
    payload: { from: 'starting', to: 'running' },
  });

const snapshotAt = (cursor: number): WorkspaceSnapshot => ({
  schema_version: 1,
  cursor,
  generated_at: '2026-11-24T12:00:00Z',
  projects: [],
  jobs: [],
  engines: {
    fourmilab: { state: 'unknown', detail: 'not checked' },
    inspector: { state: 'unknown', detail: 'not checked' },
  },
  releases: { active: null, previous: null, staged: null },
});


describe('WorkspaceEventsClient', () => {
  it('delivers global engine updates without a project but rejects unscoped job updates', async () => {
    const source = new FakeEventSource();
    const onEvent = vi.fn();
    const onError = vi.fn();
    const client = new WorkspaceEventsClient({ initialCursor: 0, eventSourceFactory: () => source,
      reloadSnapshot: vi.fn(), onEvent, onError });
    client.start();
    const engine = { ...event(1), type: 'engine.state_changed', revision: 0, project_id: null,
      job_id: null, attempt_id: null, phase: null, payload: { engine: 'fourmilab' } };
    source.emit('workspace', engine, '1');
    await client.whenIdle();
    expect(onEvent).toHaveBeenCalledWith(engine);
    expect(onError).not.toHaveBeenCalled();
    source.emit('workspace', { ...event(2), project_id: null }, '2');
    await client.whenIdle();
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    client.dispose();
  });

  it('deduplicates events and delivers them in cursor order', async () => {
    const sources: FakeEventSource[] = [];
    const factory: EventSourceFactory = vi.fn((url: string) => {
      expect(url).toContain('after=0');
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    const delivered: number[] = [];
    const client = new WorkspaceEventsClient({
      initialCursor: 0,
      eventSourceFactory: factory,
      reloadSnapshot: vi.fn(),
      onEvent: async (nextEvent) => {
        await Promise.resolve();
        delivered.push(nextEvent.event_id);
      },
    });

    client.start();
    sources[0]?.emit('workspace', event(1), '1');
    sources[0]?.emit('workspace', event(1), '1');
    sources[0]?.emit('workspace', event(2), '2');
    await client.whenIdle();

    expect(delivered).toEqual([1, 2]);
    client.dispose();
  });

  it('reconnects from the last event id after a transport failure', () => {
    const sources: FakeEventSource[] = [];
    const urls: string[] = [];
    const factory: EventSourceFactory = (url) => {
      urls.push(url);
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    };
    const client = new WorkspaceEventsClient({
      initialCursor: 4,
      eventSourceFactory: factory,
      reloadSnapshot: vi.fn(),
      onEvent: vi.fn(),
      scheduleReconnect: (reconnect) => {
        reconnect();
        return () => undefined;
      },
    });

    client.start();
    sources[0]?.emit('workspace', event(5), '5');
    sources[0]?.emit('error', null);

    expect(urls).toHaveLength(2);
    expect(urls[1]).toContain('after=5');
    client.dispose();
  });

  it('reloads the snapshot and resumes at its high-water cursor on reset', async () => {
    const sources: FakeEventSource[] = [];
    const urls: string[] = [];
    const factory: EventSourceFactory = (url) => {
      urls.push(url);
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    };
    const onSnapshot = vi.fn();
    const reloadSnapshot = vi.fn().mockResolvedValue(snapshotAt(12));
    const client = new WorkspaceEventsClient({
      initialCursor: 2,
      eventSourceFactory: factory,
      reloadSnapshot,
      onSnapshot,
      onEvent: vi.fn(),
    });

    client.start();
    sources[0]?.emit('reset', { cursor: 9 }, '9');
    await client.whenIdle();

    expect(reloadSnapshot).toHaveBeenCalledOnce();
    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ cursor: 12 }));
    expect(urls[1]).toContain('after=12');
    client.dispose();
  });

  it('retries a failed reset snapshot before reconnecting the stream', async () => {
    const sources: FakeEventSource[] = [];
    const urls: string[] = [];
    const scheduled: Array<() => void> = [];
    const factory: EventSourceFactory = (url) => {
      urls.push(url);
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    };
    const reloadSnapshot = vi
      .fn()
      .mockRejectedValueOnce(new Error('snapshot unavailable'))
      .mockResolvedValueOnce(snapshotAt(12));
    const onError = vi.fn();
    const client = new WorkspaceEventsClient({
      initialCursor: 2,
      eventSourceFactory: factory,
      reloadSnapshot,
      onEvent: vi.fn(),
      onError,
      scheduleReconnect: (retry) => {
        scheduled.push(retry);
        return () => undefined;
      },
    });

    client.start();
    sources[0]?.emit('reset', { cursor: 9 }, '9');
    await client.whenIdle();

    expect(reloadSnapshot).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(new Error('snapshot unavailable'));
    client.start();
    expect(urls).toHaveLength(1);
    expect(scheduled).toHaveLength(1);

    scheduled[0]?.();
    await client.whenIdle();

    expect(reloadSnapshot).toHaveBeenCalledTimes(2);
    expect(urls).toHaveLength(2);
    expect(urls[1]).toContain('after=12');
    client.dispose();
  });

  it('retries opening the stream at the confirmed snapshot after a construction failure', async () => {
    const initial = new FakeEventSource();
    const recovered = new FakeEventSource();
    const urls: string[] = [];
    const factory: EventSourceFactory = (url) => {
      urls.push(url);
      if (urls.length === 2) throw new Error('stream unavailable');
      return urls.length === 1 ? initial : recovered;
    };
    const retries: Array<() => void> = [];
    const onEvent = vi.fn();
    const onError = vi.fn();
    const reloadSnapshot = vi.fn().mockResolvedValue(snapshotAt(12));
    const client = new WorkspaceEventsClient({
      initialCursor: 2,
      eventSourceFactory: factory,
      reloadSnapshot,
      onEvent,
      onError,
      scheduleReconnect: (retry) => {
        retries.push(retry);
        return () => undefined;
      },
    });
    client.start();
    initial.emit('reset', { cursor: 9 });
    await client.whenIdle();
    expect(onError).toHaveBeenCalledWith(new Error('stream unavailable'));
    expect(retries).toHaveLength(1);
    retries[0]?.();
    recovered.emit('workspace', event(13));
    await client.whenIdle();
    expect(urls).toEqual(['/api/v1/events?after=2', '/api/v1/events?after=12', '/api/v1/events?after=12']);
    expect(reloadSnapshot).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledWith(event(13));
    client.dispose();
  });
});
