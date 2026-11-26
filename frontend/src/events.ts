import { useEffect, useMemo, useState } from 'react';
import { ValidationError, decodeWorkspaceEvent } from './api';
import type { ApiClient } from './api';
import type { WorkspaceEvent, WorkspaceSnapshot } from './types';

export { decodeWorkspaceEvent } from './api';

export interface EventSourceLike {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;
export type ReconnectScheduler = (reconnect: () => void) => () => void;

export interface WorkspaceEventsClientOptions {
  initialCursor: number;
  baseUrl?: string;
  eventSourceFactory?: EventSourceFactory;
  reloadSnapshot: () => Promise<WorkspaceSnapshot>;
  onEvent: (event: WorkspaceEvent) => void | Promise<void>;
  onSnapshot?: (snapshot: WorkspaceSnapshot) => void;
  onError?: (error: Error) => void;
  scheduleReconnect?: ReconnectScheduler;
}

const defaultEventSourceFactory: EventSourceFactory = (url) => new EventSource(url);
const defaultReconnectScheduler: ReconnectScheduler = (reconnect) => {
  const timeoutId = window.setTimeout(reconnect, 1_000);
  return () => window.clearTimeout(timeoutId);
};

function normalizedBaseUrl(baseUrl: string): string {
  return baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
}

function messageData(event: Event, name: string): string {
  if (!(event instanceof MessageEvent) || typeof event.data !== 'string') {
    throw new ValidationError(name, 'an SSE message with string data');
  }
  return event.data;
}

function parseEventData(event: Event): WorkspaceEvent {
  let value: unknown;
  try {
    value = JSON.parse(messageData(event, 'workspace event'));
  } catch (error) {
    if (error instanceof ValidationError) {
      throw error;
    }
    throw new ValidationError('workspace event.data', 'valid JSON');
  }
  return decodeWorkspaceEvent(value);
}

function parseResetCursor(event: Event): number {
  let value: unknown;
  try {
    value = JSON.parse(messageData(event, 'reset event'));
  } catch (error) {
    if (error instanceof ValidationError) {
      throw error;
    }
    throw new ValidationError('reset event.data', 'valid JSON');
  }
  if (
    typeof value !== 'object' ||
    value === null ||
    Array.isArray(value) ||
    !('cursor' in value) ||
    typeof value.cursor !== 'number' ||
    !Number.isSafeInteger(value.cursor) ||
    value.cursor < 0
  ) {
    throw new ValidationError('reset.cursor', 'a non-negative safe integer');
  }
  return value.cursor;
}

export class WorkspaceEventsClient {
  private readonly baseUrl: string;
  private readonly eventSourceFactory: EventSourceFactory;
  private readonly reloadSnapshot: () => Promise<WorkspaceSnapshot>;
  private readonly onEvent: (event: WorkspaceEvent) => void | Promise<void>;
  private readonly onSnapshot: ((snapshot: WorkspaceSnapshot) => void) | undefined;
  private readonly onError: ((error: Error) => void) | undefined;
  private readonly scheduleReconnect: ReconnectScheduler;
  private cursor: number;
  private source: EventSourceLike | null = null;
  private cancelReconnect: (() => void) | null = null;
  private delivery: Promise<void> = Promise.resolve();
  private isDisposed = false;
  private isResetting = false;

  constructor(options: WorkspaceEventsClientOptions) {
    if (!Number.isSafeInteger(options.initialCursor) || options.initialCursor < 0) {
      throw new ValidationError('events.initialCursor', 'a non-negative safe integer');
    }
    this.cursor = options.initialCursor;
    this.baseUrl = normalizedBaseUrl(options.baseUrl ?? '/api/v1/events');
    this.eventSourceFactory = options.eventSourceFactory ?? defaultEventSourceFactory;
    this.reloadSnapshot = options.reloadSnapshot;
    this.onEvent = options.onEvent;
    this.onSnapshot = options.onSnapshot;
    this.onError = options.onError;
    this.scheduleReconnect = options.scheduleReconnect ?? defaultReconnectScheduler;
  }

  start(): void {
    this.connect();
  }

  dispose(): void {
    this.isDisposed = true;
    this.cancelReconnect?.();
    this.cancelReconnect = null;
    this.disconnect();
  }

  whenIdle(): Promise<void> {
    return this.delivery;
  }

  private readonly handleWorkspace: EventListener = (rawEvent) => {
    try {
      const event = parseEventData(rawEvent);
      if (event.event_id <= this.cursor || this.isResetting || this.isDisposed) {
        return;
      }
      this.cursor = event.event_id;
      this.delivery = this.delivery
        .then(async () => {
          if (!this.isDisposed) {
            await this.onEvent(event);
          }
        })
        .catch((error: unknown) => this.report(error));
    } catch (error) {
      this.report(error);
    }
  };

  private readonly handleReset: EventListener = (rawEvent) => {
    if (this.isResetting || this.isDisposed) {
      return;
    }
    try {
      this.cursor = Math.max(this.cursor, parseResetCursor(rawEvent));
    } catch (error) {
      this.report(error);
      return;
    }
    this.isResetting = true;
    this.disconnect();
    this.reloadResetSnapshot();
  };

  private reloadResetSnapshot(): void {
    this.delivery = this.delivery.then(async () => {
      try {
        const snapshot = await this.reloadSnapshot();
        if (this.isDisposed) {
          return;
        }
        this.cursor = snapshot.cursor;
        this.onSnapshot?.(snapshot);
        this.isResetting = false;
        this.connect();
      } catch (error) {
        if (!this.isDisposed) {
          this.report(error);
          this.queueResetReload();
        }
      }
    });
  }

  private queueResetReload(): void {
    if (this.isDisposed || !this.isResetting || this.cancelReconnect !== null) {
      return;
    }
    this.cancelReconnect = this.scheduleReconnect(() => {
      this.cancelReconnect = null;
      this.reloadResetSnapshot();
    });
  }

  private readonly handleTransportError: EventListener = () => {
    if (!this.isDisposed && !this.isResetting) {
      this.disconnect();
      this.queueReconnect();
    }
  };

  private connect(): void {
    if (this.isDisposed || this.isResetting || this.source !== null) {
      return;
    }
    const separator = this.baseUrl.includes('?') ? '&' : '?';
    let source: EventSourceLike;
    try {
      source = this.eventSourceFactory(
        `${this.baseUrl}${separator}after=${encodeURIComponent(String(this.cursor))}`,
      );
    } catch (error) {
      this.report(error);
      this.queueReconnect();
      return;
    }
    source.addEventListener('workspace', this.handleWorkspace);
    source.addEventListener('reset', this.handleReset);
    source.addEventListener('error', this.handleTransportError);
    this.source = source;
  }

  private disconnect(): void {
    if (this.source === null) {
      return;
    }
    this.source.removeEventListener('workspace', this.handleWorkspace);
    this.source.removeEventListener('reset', this.handleReset);
    this.source.removeEventListener('error', this.handleTransportError);
    this.source.close();
    this.source = null;
  }

  private queueReconnect(): void {
    if (this.isDisposed || this.cancelReconnect !== null) {
      return;
    }
    this.cancelReconnect = this.scheduleReconnect(() => {
      this.cancelReconnect = null;
      this.connect();
    });
  }

  private report(error: unknown): void {
    const normalized = error instanceof Error ? error : new Error('Unknown event stream error');
    this.onError?.(normalized);
  }
}

export interface UseWorkspaceEventsOptions {
  api: ApiClient;
  initialSnapshot: WorkspaceSnapshot;
  baseUrl?: string;
  onEvent?: (event: WorkspaceEvent) => void;
}

export interface WorkspaceEventsState {
  snapshot: WorkspaceSnapshot;
  events: readonly WorkspaceEvent[];
  error: Error | null;
}

export function useWorkspaceEvents({
  api,
  initialSnapshot,
  baseUrl,
  onEvent,
}: UseWorkspaceEventsOptions): WorkspaceEventsState {
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const [events, setEvents] = useState<WorkspaceEvent[]>([]);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const client = new WorkspaceEventsClient({
      initialCursor: initialSnapshot.cursor,
      ...(baseUrl === undefined ? {} : { baseUrl }),
      reloadSnapshot: () => api.getSnapshot(),
      onEvent: (event) => {
        setEvents((current) => [...current, event]);
        onEvent?.(event);
      },
      onSnapshot: (nextSnapshot) => {
        setSnapshot(nextSnapshot);
        setEvents([]);
      },
      onError: setError,
    });
    client.start();
    return () => client.dispose();
  }, [api, baseUrl, initialSnapshot.cursor, onEvent]);

  return useMemo(() => ({ snapshot, events, error }), [snapshot, events, error]);
}
