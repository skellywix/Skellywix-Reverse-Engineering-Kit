import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Banner } from '@astryxdesign/core/Banner';
import { FormLayout } from '@astryxdesign/core/FormLayout';
import { Grid } from '@astryxdesign/core/Grid';
import { Section } from '@astryxdesign/core/Section';
import { Selector } from '@astryxdesign/core/Selector';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { Tab, TabList } from '@astryxdesign/core/TabList';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Heading, Text } from '@astryxdesign/core/Text';
import InspectionResult from '../components/InspectionResult';
import InspectorAdvanced from '../components/InspectorAdvanced';
import InspectorLedger from '../components/InspectorLedger';
import PageHeader from '../components/PageHeader';
import { PrimaryAction, PrimaryActionScope } from '../components/PrimaryAction';
import SpecimenPicker, {
  ReplacementGuard,
  type SpecimenPickerHandle,
  type SpecimenSelection,
} from '../components/SpecimenPicker';
import {
  InspectApiError,
  InspectStaleRevisionError,
  createInspectApiClient,
  isChecksumResult,
  measureReplacementLoss,
  needsReplacementConfirmation,
  type CriticStatus,
  type InspectResult,
  type InspectSlot,
  type InspectState,
  type InspectStateEnvelope,
  type InspectTool,
} from './inspectApi';

type BusyAction =
  | 'refresh'
  | 'upload'
  | 'run'
  | 'export'
  | 'reset'
  | 'review'
  | 'pair'
  | 'disconnect'
  | 'critic'
  | null;
type RetryAction = 'refresh' | 'run' | 'export' | null;

interface ActiveFinding {
  revision: number;
  tool: InspectTool;
  slot?: InspectSlot;
  digest?: string;
  result: InspectResult;
}

/** A file name this tab loaded, bound to the digest it describes. Never sent anywhere. */
interface LoadedName {
  name: string;
  sha256: string;
}
type LoadedNames = Partial<Record<InspectSlot, LoadedName>>;

interface PendingReplacement {
  slot: InspectSlot;
  file: File;
  name: string;
  revision: number;
}

function localBasename(name: string): string {
  return name.split(/[\\/]/).pop() ?? name;
}

const MODES: ReadonlyArray<{
  value: InspectTool;
  label: string;
  description: string;
}> = [
    {
      value: 'identify_file',
      label: 'File ID',
      description: 'Show the file type, architecture, size, and SHA-256 fingerprint.',
    },
    {
      value: 'compare_builds',
      label: 'Compare files',
      description: 'See how files A and B differ in size, type, architecture, or hash.',
    },
    {
      value: 'verify_checksum',
      label: 'Hash check',
      description: 'Check the file against a SHA-256 fingerprint from a trusted source.',
    },
  ];

const DIGEST_PATTERN = /^[0-9a-f]{64}$/i;
const INSPECT_DRAFT_KEY = 'skellywix.inspect.draft.v1';

interface InspectDraft {
  mode: InspectTool;
  slot: InspectSlot;
  digest: string;
}

const DEFAULT_INSPECT_DRAFT: InspectDraft = {
  mode: 'identify_file',
  slot: 'a',
  digest: '',
};

function busyLabel(action: Exclude<BusyAction, null>): string {
  switch (action) {
    case 'refresh':
      return 'Refreshing';
    case 'upload':
      return 'Loading specimen';
    case 'run':
      return 'Checking';
    case 'export':
      return 'Exporting evidence';
    case 'reset':
      return 'Resetting case';
    case 'review':
      return 'Running review';
    case 'pair':
      return 'Issuing case key';
    case 'disconnect':
      return 'Revoking case key';
    case 'critic':
      return 'Cancelling critique';
  }
}

function criticAnnouncement(status: CriticStatus): string {
  switch (status) {
    case 'idle':
      return 'Model critique is idle.';
    case 'not_requested':
      return 'Review completed without a model critique.';
    case 'pending':
      return 'Model critique is pending. No critic is connected yet.';
    case 'reviewing':
      return 'The critic is reviewing the ledger snapshot.';
    case 'ready':
      return 'Model critique is ready.';
    case 'failed':
      return 'Model critique did not complete.';
    case 'expired':
      return 'Model critique expired.';
    case 'cancelled':
      return 'Model critique was cancelled.';
    case 'stale':
      return 'Model critique is stale.';
  }
}

function isInspectTool(value: string): value is InspectTool {
  return MODES.some((mode) => mode.value === value);
}

function isInspectSlot(value: string): value is InspectSlot {
  return value === 'a' || value === 'b';
}

function loadInspectDraft(): InspectDraft {
  try {
    const encoded = window.sessionStorage.getItem(INSPECT_DRAFT_KEY);
    if (encoded === null) return DEFAULT_INSPECT_DRAFT;
    const parsed: unknown = JSON.parse(encoded);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_INSPECT_DRAFT;
    }
    const candidate = parsed as Record<string, unknown>;
    return {
      mode:
        typeof candidate.mode === 'string' && isInspectTool(candidate.mode)
          ? candidate.mode
          : DEFAULT_INSPECT_DRAFT.mode,
      slot:
        typeof candidate.slot === 'string' && isInspectSlot(candidate.slot)
          ? candidate.slot
          : DEFAULT_INSPECT_DRAFT.slot,
      digest:
        typeof candidate.digest === 'string' && candidate.digest.length <= 256
          ? candidate.digest
          : DEFAULT_INSPECT_DRAFT.digest,
    };
  } catch {
    return DEFAULT_INSPECT_DRAFT;
  }
}

function saveInspectDraft(draft: InspectDraft): void {
  try {
    window.sessionStorage.setItem(INSPECT_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Session storage is optional; the mounted page still retains the draft.
  }
}

function findVisibleFinding(
  state: InspectState | null,
  finding: ActiveFinding | null,
  mode: InspectTool,
  slot: InspectSlot,
  normalizedDigest: string,
): ActiveFinding | null {
  if (state === null) return null;

  for (let index = state.events.length - 1; index >= 0; index -= 1) {
    const event = state.events[index];
    if (event === undefined || event.tool !== mode) continue;
    if (mode === 'compare_builds') {
      return {
        revision: state.revision,
        tool: mode,
        result: event.result,
      };
    }
    if (event.slot !== slot) continue;
    if (mode === 'verify_checksum') {
      if (
        !isChecksumResult(event.result) ||
        event.result.expected !== normalizedDigest
      ) {
        continue;
      }
      return {
        revision: state.revision,
        tool: mode,
        slot,
        digest: normalizedDigest,
        result: event.result,
      };
    }
    return {
      revision: state.revision,
      tool: mode,
      slot,
      result: event.result,
    };
  }
  if (
    finding !== null &&
    finding.revision === state.revision &&
    finding.tool === mode &&
    (mode === 'compare_builds' || finding.slot === slot) &&
    (mode !== 'verify_checksum' || finding.digest === normalizedDigest)
  ) {
    return finding;
  }
  return null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return 'The inspector request failed.';
}

export default function Inspect(): React.JSX.Element {
  const clientRef = useRef(createInspectApiClient());
  const pickerRef = useRef<SpecimenPickerHandle>(null);
  const exportAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const stateRef = useRef<InspectState | null>(null);
  const requestSequence = useRef(0);
  const acceptedSequence = useRef(0);
  const busyRef = useRef<BusyAction>('refresh');

  const [initialDraft] = useState(loadInspectDraft);
  const [state, setState] = useState<InspectState | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [engineDetail, setEngineDetail] = useState('Connecting to the inspector.');
  const [busy, setBusy] = useState<BusyAction>('refresh');
  const [error, setError] = useState('');
  const [announcement, setAnnouncement] = useState('Connecting to the inspector.');
  const [retryAction, setRetryAction] = useState<RetryAction>(null);
  const [mode, setMode] = useState<InspectTool>(initialDraft.mode);
  const [slot, setSlot] = useState<InspectSlot>(initialDraft.slot);
  const [digest, setDigest] = useState(initialDraft.digest);
  const [finding, setFinding] = useState<ActiveFinding | null>(null);
  const [loadedNames, setLoadedNames] = useState<LoadedNames>({});
  const [selection, setSelection] = useState<SpecimenSelection | null>(null);
  const [pendingFile, setPendingFile] = useState<PendingReplacement | null>(null);
  const [caseCredential, setCaseCredential] = useState<{
    revision: number;
    token: string;
  } | null>(null);

  const nextRequest = useCallback((): number => {
    requestSequence.current += 1;
    return requestSequence.current;
  }, []);


  const setBusyAction = useCallback((next: BusyAction): void => {
    busyRef.current = next;
    if (mountedRef.current) setBusy(next);
  }, []);
  const acceptState = useCallback((next: InspectState, requestId: number): boolean => {
    const current = stateRef.current;
    if (
      requestId < requestSequence.current ||
      requestId < acceptedSequence.current ||
      (current !== null && next.revision < current.revision)
    ) {
      if (mountedRef.current) setAnnouncement('Ignored a stale response from an older case.');
      return false;
    }

    acceptedSequence.current = requestId;
    const revisionChanged = current !== null && current.revision !== next.revision;
    stateRef.current = next;
    if (!mountedRef.current) return false;
    setState(next);
    setHasLoaded(true);
    setEngineDetail('Inspector available.');
    if (revisionChanged) {
      setFinding(null);
      setCaseCredential(null);
      setSelection((held) => (held?.phase === 'confirm' ? held : null));
    } else if (next.connection !== 'ready' && next.connection !== 'connected') {
      setCaseCredential(null);
    }
    if (next.slots.a === undefined) {
      // A file chosen for B cannot be loaded without A.
      setPendingFile((held) => (held?.slot === 'b' ? null : held));
      setSelection((held) => (held?.slot === 'b' && held.phase === 'confirm' ? null : held));
    }
    return true;
  }, []);

  const acceptEnvelope = useCallback(
    (next: InspectStateEnvelope, requestId: number): boolean => {
      if (next.state !== null) return acceptState(next.state, requestId);
      if (requestId < requestSequence.current || requestId < acceptedSequence.current) {
        if (mountedRef.current) setAnnouncement('Ignored a stale availability response.');
        return false;
      }
      acceptedSequence.current = requestId;
      stateRef.current = null;
      if (!mountedRef.current) return false;
      setState(null);
      setHasLoaded(true);
      setEngineDetail(next.engine.detail);
      setFinding(null);
      setCaseCredential(null);
      setLoadedNames({});
      setPendingFile(null);
      setSelection(null);
      return true;
    },
    [acceptState],
  );

  const markOffline = useCallback((detail: string, requestId: number): void => {
    if (
      requestId < requestSequence.current ||
      requestId < acceptedSequence.current
    ) {
      return;
    }
    acceptedSequence.current = requestId;
    stateRef.current = null;
    if (!mountedRef.current) return;
    setState(null);
    setHasLoaded(true);
    setEngineDetail(detail);
    setFinding(null);
    setCaseCredential(null);
    setLoadedNames({});
    setPendingFile(null);
    setSelection(null);
  }, []);

  const refresh = useCallback(
    async (preserveError = false, signal?: AbortSignal): Promise<void> => {
      const requestId = nextRequest();
      if (mountedRef.current) {
        setBusyAction('refresh');
        if (!preserveError) setError('');
      }
      try {
        const next = await clientRef.current.getState(signal);
        if (acceptEnvelope(next, requestId)) {
          if (!preserveError) setRetryAction(null);
          setAnnouncement(
            next.state === null
              ? 'Inspector unavailable. Previous answers are hidden.'
              : `Inspector state refreshed at revision ${next.state.revision}.`,
          );
        }
      } catch (caught) {
        if (isAbortError(caught)) return;
        markOffline('The inspector could not be reached.', requestId);
        if (mountedRef.current) {
          if (!preserveError) setError('The inspector could not be reached.');
          setRetryAction('refresh');
          setAnnouncement('Inspector offline. Previous answers are hidden.');
        }
      } finally {
        if (mountedRef.current && requestId === requestSequence.current) {
          setBusyAction(null);
        }
      }
    },
    [acceptEnvelope, markOffline, nextRequest, setBusyAction],
  );

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    let isActive = true;
    let isReading = false;
    let readAgain = false;
    let timer: number | undefined;

    const readState = async (isInitial: boolean): Promise<void> => {
      if (!isActive) return;
      if (isReading) {
        readAgain = true;
        return;
      }
      if (!isInitial && busyRef.current !== null) return;
      isReading = true;
      const previous = stateRef.current;
      const requestId = nextRequest();
      try {
        const next = await clientRef.current.getState(controller.signal);
        if (!isActive) return;
        if (acceptEnvelope(next, requestId)) {
          setRetryAction(next.state === null ? 'refresh' : null);
          if (isInitial) {
            setAnnouncement(
              next.state === null
                ? 'Inspector unavailable. Previous answers are hidden.'
                : `Inspector state refreshed at revision ${next.state.revision}.`,
            );
          } else if (previous === null && next.state !== null) {
            setError('');
            setAnnouncement(`Inspector connection restored at revision ${next.state.revision}.`);
          } else if (previous !== null && next.state === null) {
            setAnnouncement('Inspector offline. Previous answers are hidden.');
          } else if (
            previous !== null &&
            next.state !== null &&
            previous.advisor.critique.status !== next.state.advisor.critique.status
          ) {
            setAnnouncement(criticAnnouncement(next.state.advisor.critique.status));
          }
        }
      } catch (caught) {
        if (
          !isActive ||
          isAbortError(caught) ||
          requestId < requestSequence.current
        ) {
          return;
        }
        markOffline('The inspector could not be reached.', requestId);
        setRetryAction('refresh');
        if (isInitial) setError('The inspector could not be reached.');
        if (isInitial || previous !== null) {
          setAnnouncement('Inspector offline. Previous answers are hidden.');
        }
      } finally {
        isReading = false;
        if (
          isInitial &&
          isActive &&
          requestId === requestSequence.current
        ) {
          setBusyAction(null);
        }
        if (readAgain && isActive && busyRef.current === null) {
          readAgain = false;
          void readState(false);
        }
        if (isInitial && isActive && timer === undefined) {
          timer = window.setInterval(() => readState(false), 2_200);
        }
      }
    };

    void readState(true);
    return () => {
      isActive = false;
      mountedRef.current = false;
      if (timer !== undefined) window.clearInterval(timer);
      controller.abort();
      exportAbortRef.current?.abort();
    };
  }, [acceptEnvelope, markOffline, nextRequest, setBusyAction]);

  const recoverFailure = useCallback(
    async (
      caught: unknown,
      intendedRetry: RetryAction,
      requestId: number,
    ): Promise<void> => {
      if (!mountedRef.current) return;
      setFinding(null);
      setCaseCredential(null);
      if (caught instanceof InspectStaleRevisionError) {
        setError(caught.message);
        setRetryAction(intendedRetry);
        setAnnouncement(
          `Case changed to revision ${caught.currentRevision}. Refreshing before retry.`,
        );
        await refresh(true);
        return;
      }
      if (
        caught instanceof TypeError ||
        (caught instanceof InspectApiError && caught.code === 'engine_unavailable')
      ) {
        markOffline('The inspector connection was interrupted.', requestId);
      }
      setError(errorMessage(caught));
      setRetryAction(intendedRetry);
      setAnnouncement('The check failed. Review the current case before retrying.');
    },
    [markOffline, refresh],
  );

  const normalizedDigest = digest.trim().toLowerCase();
  const isDigestValid = DIGEST_PATTERN.test(normalizedDigest);

  const visibleFinding = findVisibleFinding(
    state,
    finding,
    mode,
    slot,
    normalizedDigest,
  );

  // A name shows only while the slot still holds the digest it was loaded with.
  const displayNames = useMemo(() => {
    const shown: Partial<Record<InspectSlot, string>> = {};
    for (const key of ['a', 'b'] as const) {
      const held = loadedNames[key];
      if (held !== undefined && state?.slots[key]?.sha256 === held.sha256) {
        shown[key] = held.name;
      }
    }
    return shown;
  }, [loadedNames, state]);

  const loadFile = useCallback(
    async (chosenSlot: InspectSlot, file: File): Promise<boolean> => {
      const current = stateRef.current;
      if (current === null) return false;
      if (chosenSlot === 'b' && current.slots.a === undefined) {
        setError('Choose specimen A before specimen B.');
        return false;
      }
      const name = localBasename(file.name);
      const requestId = nextRequest();
      setBusyAction('upload');
      setError('');
      setRetryAction(null);
      setPendingFile(null);
      setSelection({ slot: chosenSlot, name, phase: 'loading' });
      setAnnouncement(`Loading specimen ${chosenSlot.toUpperCase()}.`);
      let loaded = false;
      try {
        const next = await clientRef.current.upload(
          chosenSlot,
          current.revision,
          file,
        );
        if (acceptState(next, requestId)) {
          const specimen = next.slots[chosenSlot];
          loaded = true;
          setFinding(null);
          setCaseCredential(null);
          setSelection(null);
          setLoadedNames((held) => {
            // Loading A clears B in the inspector, so its local name goes too.
            const kept: LoadedNames = chosenSlot === 'a' ? {} : held;
            return specimen === undefined
              ? kept
              : { ...kept, [chosenSlot]: { name, sha256: specimen.sha256 } };
          });
          setAnnouncement(
            `Specimen ${chosenSlot.toUpperCase()} loaded at revision ${next.revision}.`,
          );
        }
      } catch (caught) {
        await recoverFailure(caught, null, requestId);
        if (mountedRef.current && stateRef.current !== null) {
          setSelection({ slot: chosenSlot, name, phase: 'failed' });
        }
      } finally {
        if (mountedRef.current && requestId === requestSequence.current) {
          setBusyAction(null);
        }
      }
      return loaded;
    },
    [acceptState, nextRequest, recoverFailure, setBusyAction],
  );

  const chooseFile = useCallback(
    async (chosenSlot: InspectSlot, file: File): Promise<void> => {
      const current = stateRef.current;
      if (current === null) return;
      if (chosenSlot === 'b' && current.slots.a === undefined) {
        setError('Choose specimen A before specimen B.');
        return;
      }
      const loss = measureReplacementLoss(current, chosenSlot);
      if (!needsReplacementConfirmation(loss)) {
        await loadFile(chosenSlot, file);
        return;
      }
      const name = localBasename(file.name);
      setError('');
      setRetryAction(null);
      setSelection({ slot: chosenSlot, name, phase: 'confirm' });
      setPendingFile({ slot: chosenSlot, file, name, revision: current.revision });
      setAnnouncement(
        `Specimen ${chosenSlot.toUpperCase()} is not loaded yet. Loading it clears the evidence ledger. Choose whether to keep the case, export first, or replace.`,
      );
    },
    [loadFile],
  );

  const keepCase = useCallback((): void => {
    const held = pendingFile;
    setPendingFile(null);
    setSelection(null);
    setAnnouncement('Kept the current case. The chosen file was not loaded.');
    if (held !== null) pickerRef.current?.focus(held.slot);
  }, [pendingFile]);

  const runCheck = useCallback(async (): Promise<void> => {
    const current = stateRef.current;
    if (current === null) return;
    if (mode !== 'compare_builds' && current.slots[slot] === undefined) return;
    if (mode === 'compare_builds' && (!current.slots.a || !current.slots.b)) return;
    if (mode === 'verify_checksum' && !isDigestValid) return;

    const request =
      mode === 'compare_builds'
        ? { revision: current.revision, tool: mode, arguments: {} as Record<string, never> }
        : mode === 'verify_checksum'
          ? {
            revision: current.revision,
            tool: mode,
            arguments: { slot, sha256: normalizedDigest },
          }
          : { revision: current.revision, tool: mode, arguments: { slot } };
    const requestId = nextRequest();
    setBusyAction('run');
    setError('');
    setRetryAction(null);
    setAnnouncement('Checking the selected specimen metadata.');
    try {
      const response = await clientRef.current.run(request);
      if (acceptState(response.state, requestId)) {
        setFinding({
          revision: response.state.revision,
          tool: mode,
          result: response.result,
          ...(mode === 'compare_builds' ? {} : { slot }),
          ...(mode === 'verify_checksum' ? { digest: normalizedDigest } : {}),
        });
        setAnnouncement(`${MODES.find((item) => item.value === mode)?.label} complete.`);
      }
    } catch (caught) {
      await recoverFailure(caught, 'run', requestId);
      const latest = stateRef.current;
      if (
        latest !== null &&
        (latest.slots.a === undefined ||
          (mode === 'compare_builds' && latest.slots.b === undefined) ||
          (mode !== 'compare_builds' && latest.slots[slot] === undefined))
      ) {
        setRetryAction(null);
      }
    } finally {
      if (mountedRef.current && requestId === requestSequence.current) {
        setBusyAction(null);
      }
    }
  }, [
    acceptState,
    isDigestValid,
    mode,
    nextRequest,
    normalizedDigest,
    recoverFailure,
    setBusyAction,
    slot,
  ]);

  const exportReport = useCallback(async (): Promise<boolean> => {
    if (stateRef.current === null) return false;
    const requestId = nextRequest();
    const controller = new AbortController();
    exportAbortRef.current?.abort();
    exportAbortRef.current = controller;
    setBusyAction('export');
    setError('');
    setRetryAction(null);
    setAnnouncement('Preparing the evidence report.');
    try {
      const report = await clientRef.current.getReport(controller.signal);
      if (!mountedRef.current || requestId !== requestSequence.current) return false;
      const objectUrl = URL.createObjectURL(report);
      try {
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = 'skellywix-re-kit-evidence.json';
        link.click();
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
      setAnnouncement('Evidence report downloaded.');
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        await recoverFailure(caught, 'export', requestId);
      }
      return false;
    } finally {
      if (exportAbortRef.current === controller) exportAbortRef.current = null;
      if (mountedRef.current && requestId === requestSequence.current) {
        setBusyAction(null);
      }
    }
  }, [nextRequest, recoverFailure, setBusyAction]);

  const exportThenReplace = useCallback(async (): Promise<void> => {
    const held = pendingFile;
    if (held === null) return;
    // A failed export leaves the case and the prompt untouched.
    if (!(await exportReport())) return;
    if (!mountedRef.current) return;
    await loadFile(held.slot, held.file);
  }, [exportReport, loadFile, pendingFile]);

  const replaceNow = useCallback((): void => {
    const held = pendingFile;
    if (held !== null) void loadFile(held.slot, held.file);
  }, [loadFile, pendingFile]);

  const applyStateMutation = useCallback(
    async (
      action: Exclude<BusyAction, 'refresh' | 'upload' | 'run' | 'export' | null>,
      request: (current: InspectState) => Promise<InspectState>,
      successMessage: string,
    ): Promise<void> => {
      const current = stateRef.current;
      if (current === null) return;
      const requestId = nextRequest();
      setBusyAction(action);
      setError('');
      setRetryAction(null);
      try {
        const next = await request(current);
        if (acceptState(next, requestId)) {
          setAnnouncement(successMessage);
        }
      } catch (caught) {
        await recoverFailure(caught, null, requestId);
      } finally {
        if (mountedRef.current && requestId === requestSequence.current) {
          setBusyAction(null);
        }
      }
    },
    [acceptState, nextRequest, recoverFailure, setBusyAction],
  );

  const resetCase = useCallback((): void => {
    void applyStateMutation(
      'reset',
      (current) => clientRef.current.reset(current.revision),
      'The inspection case was reset.',
    );
  }, [applyStateMutation]);

  const startReview = useCallback(
    (reference: string | null, critique: boolean): void => {
      void applyStateMutation(
        'review',
        (current) =>
          clientRef.current.review({
            revision: current.revision,
            reference,
            critique,
          }),
        critique
          ? 'Checks logged. No critic connected yet.'
          : 'Checks logged. No model critique requested.',
      );
    },
    [applyStateMutation],
  );

  const pairAgent = useCallback(async (): Promise<void> => {
    const current = stateRef.current;
    if (current === null) return;
    const requestId = nextRequest();
    setBusyAction('pair');
    setError('');
    setRetryAction(null);
    try {
      const response = await clientRef.current.pair(current.revision);
      if (acceptState(response.state, requestId)) {
        setCaseCredential({
          revision: response.state.revision,
          token: response.token,
        });
        setAnnouncement('A read-only case key was issued for this page.');
      }
    } catch (caught) {
      await recoverFailure(caught, null, requestId);
    } finally {
      if (mountedRef.current && requestId === requestSequence.current) {
        setBusyAction(null);
      }
    }
  }, [acceptState, nextRequest, recoverFailure, setBusyAction]);

  const disconnectAgent = useCallback((): void => {
    void applyStateMutation(
      'disconnect',
      (current) => clientRef.current.disconnect(current.revision),
      'The case key was revoked.',
    );
  }, [applyStateMutation]);

  const cancelCritic = useCallback(
    (end: number): void => {
      void applyStateMutation(
        'critic',
        (current) => clientRef.current.cancelCritic(current.revision, end),
        'The pending critique was cancelled.',
      );
    },
    [applyStateMutation],
  );

  const activeCaseKey =
    state &&
      caseCredential?.revision === state.revision &&
      (state.connection === 'ready' || state.connection === 'connected')
      ? caseCredential.token
      : null;

  const modeDetails = MODES.find((item) => item.value === mode)!;
  const missingSelectedSpecimen =
    mode !== 'compare_builds' && state?.slots[slot] === undefined;
  const missingComparisonSpecimen =
    mode === 'compare_builds' && state?.slots.b === undefined;
  const needsDigest = mode === 'verify_checksum' && !isDigestValid;

  let readiness = 'Waiting for the inspector.';
  if (state !== null) {
    if (state.slots.a === undefined) {
      readiness = 'Choose specimen A to start.';
    } else if (missingComparisonSpecimen) {
      readiness = 'Choose file B to compare it with file A.';
    } else if (missingSelectedSpecimen) {
      readiness = `Choose specimen ${slot.toUpperCase()} to run this check.`;
    } else if (needsDigest) {
      readiness = 'Enter a 64-character hexadecimal digest.';
    } else if (mode === 'compare_builds') {
      readiness = 'Ready to compare specimens A and B.';
    } else if (mode === 'verify_checksum') {
      readiness = `Ready to verify specimen ${slot.toUpperCase()}.`;
    } else {
      readiness = `Ready to inspect specimen ${slot.toUpperCase()}.`;
    }
  }

  let primaryLabel = 'Retry check';
  if (busy !== null) {
    primaryLabel =
      busy === 'refresh' && !hasLoaded ? 'Retry check' : busyLabel(busy);
  } else if (retryAction === 'run') {
    primaryLabel = 'Retry check';
  } else if (retryAction === 'export') {
    primaryLabel = 'Retry export';
  } else if (retryAction === 'refresh' || state === null) {
    primaryLabel = 'Retry check';
  } else if (visibleFinding !== null) {
    primaryLabel = 'Export evidence';
  } else if (state.slots.a === undefined) {
    primaryLabel = 'Choose specimen A';
  } else if (missingComparisonSpecimen) {
    primaryLabel = 'Choose specimen B';
  } else if (missingSelectedSpecimen) {
    primaryLabel = `Choose specimen ${slot.toUpperCase()}`;
  } else if (mode === 'compare_builds') {
    primaryLabel = 'Compare specimens';
  } else if (mode === 'verify_checksum') {
    primaryLabel = 'Verify checksum';
  } else {
    primaryLabel = 'Inspect specimen';
  }

  const activatePrimary = (): void => {
    if (busy !== null) return;
    if (retryAction === 'run') {
      if (state === null) void refresh();
      else void runCheck();
      return;
    }
    if (retryAction === 'export') {
      if (state === null) void refresh();
      else void exportReport();
      return;
    }
    if (retryAction === 'refresh' || state === null) {
      void refresh();
      return;
    }
    if (visibleFinding !== null) {
      void exportReport();
      return;
    }
    if (state.slots.a === undefined) {
      pickerRef.current?.choose('a');
      return;
    }
    if (missingComparisonSpecimen) {
      pickerRef.current?.choose('b');
      return;
    }
    if (missingSelectedSpecimen) {
      pickerRef.current?.choose(slot);
      return;
    }
    void runCheck();
  };

  const changeMode = (nextMode: InspectTool): void => {
    setMode(nextMode);
    saveInspectDraft({ mode: nextMode, slot, digest });
    setRetryAction(null);
    setError('');
  };

  const changeSlot = (nextSlot: InspectSlot): void => {
    setSlot(nextSlot);
    saveInspectDraft({ mode, slot: nextSlot, digest });
    setRetryAction(null);
    setError('');
  };

  const changeDigest = (nextDigest: string): void => {
    setDigest(nextDigest);
    saveInspectDraft({ mode, slot, digest: nextDigest });
    setRetryAction(null);
    setError('');
  };

  return (
    <VStack gap={6}>
      <PageHeader
        title="Inspect"
        description="Check a file’s type or checksum, or compare two files. The inspector keeps the results, not the files."
      />

      {hasLoaded && state === null && (
        <Banner
          status="warning"
          title="Inspector is offline"
          description={`${engineDetail} Previous answers are hidden.`}
          container="section"
          collapsible={false}
        />
      )}
      {error && (
        <section role="alert">
          <Banner
            status="error"
            title="Inspection request failed"
            description={error}
            container="section"
            collapsible={false}
          />
        </section>
      )}

      <TabList
        value={mode}
        onChange={(value) => {
          if (isInspectTool(value)) changeMode(value);
        }}
        role="tablist"
        layout="fill"
        hasDivider
        overflow="scroll"
      >
        {MODES.map((item) => (
          <Tab
            key={item.value}
            value={item.value}
            label={item.label}
            panelId="inspect-mode-panel"
          />
        ))}
      </TabList>

      <Grid columns={{ minWidth: 360, max: 2 }} gap={6} align="start">
        <VStack gap={5}>
          <VStack gap={3} as="section">
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="center">
              <Heading level={2}>File intake</Heading>
              <Text type="code" color="secondary" hasTabularNumbers>
                {state ? `Revision ${state.revision}` : 'Revision unavailable'}
              </Text>
            </HStack>
            <Text as="p" type="supporting" color="secondary">
              Load A first. Replacing A clears B, the evidence ledger, review state, and
              any case key. Each specimen is limited to 16 MiB. File names stay in this tab.
            </Text>
            {state === null ? (
              <Text as="p" type="supporting" color="secondary" role="status">
                {hasLoaded
                  ? 'Files unavailable while offline. Nothing is known about the loaded files until the inspector returns.'
                  : 'Loading specimen metadata.'}
              </Text>
            ) : (
              <SpecimenPicker
                ref={pickerRef}
                slots={state.slots}
                loadedNames={displayNames}
                selection={selection}
                isDisabled={busy !== null}
                onChoose={chooseFile}
              />
            )}
            {state !== null && pendingFile !== null && (
              <ReplacementGuard
                slot={pendingFile.slot}
                name={pendingFile.name}
                loss={measureReplacementLoss(state, pendingFile.slot)}
                caseChanged={state.revision !== pendingFile.revision}
                isBusy={busy !== null}
                canExport={state.slots.a !== undefined}
                onExportThenReplace={() => void exportThenReplace()}
                onReplace={replaceNow}
                onKeep={keepCase}
              />
            )}
          </VStack>

          <Section variant="muted" padding={4}>
            <VStack
              id="inspect-mode-panel"
              gap={4}
              as="section"
              role="tabpanel"
              aria-label={`${modeDetails.label} controls`}
            >
              <Heading level={2}>{modeDetails.label}</Heading>
              <Text as="p" type="supporting" color="secondary">
                {modeDetails.description}
              </Text>
              {mode !== 'compare_builds' && (
                <FormLayout>
                  <Selector
                    label="Specimen"
                    value={slot}
                    options={[
                      { value: 'a', label: 'Specimen A' },
                      { value: 'b', label: 'Specimen B' },
                    ]}
                    onChange={(value) => {
                      if (isInspectSlot(value)) changeSlot(value);
                    }}
                    isDisabled={busy !== null || state === null}
                  />
                  {mode === 'verify_checksum' && (
                    <TextInput
                      label="Expected SHA-256"
                      value={digest}
                      onChange={changeDigest}
                      autoComplete="off"
                      hasSpellCheck={false}
                      placeholder="64 hexadecimal characters"
                      isDisabled={busy !== null || state === null}
                      {...(digest.length > 0 && !isDigestValid
                        ? {
                          status: {
                            type: 'error' as const,
                            message: 'Enter 64 hexadecimal characters.',
                          },
                        }
                        : {})}
                    />
                  )}
                </FormLayout>
              )}
              <Text as="p" type="supporting" color="secondary" role="status">
                {readiness}
              </Text>
            </VStack>
          </Section>
        </VStack>

        <VStack gap={5} as="aside">
          <Section variant="muted" padding={4}>
            <VStack gap={3} aria-live="polite">
              <Heading level={2}>Current check</Heading>
              {visibleFinding ? (
                <InspectionResult
                  tool={visibleFinding.tool}
                  result={visibleFinding.result}
                  slots={state?.slots ?? {}}
                  names={displayNames}
                />
              ) : (
                <Text as="p" color="secondary">
                  No current result for this selection.
                </Text>
              )}
            </VStack>
          </Section>
          {state ? (
            <InspectorLedger state={state} names={displayNames} />
          ) : (
            <VStack gap={3} as="section">
              <Heading level={2}>Evidence ledger</Heading>
              <Text as="p" color="secondary" role="status">
                Ledger unavailable while offline. This does not mean it is empty; entries show
                again when the inspector returns.
              </Text>
            </VStack>
          )}
        </VStack>
      </Grid>

      <PrimaryActionScope label="Inspect workflow action">
        <PrimaryAction
          label={primaryLabel}
          isLoading={busy === 'run'}
          isDisabled={
            busy !== null ||
            (state !== null &&
              retryAction === null &&
              state.slots.a !== undefined &&
              !missingComparisonSpecimen &&
              !missingSelectedSpecimen &&
              visibleFinding === null &&
              needsDigest)
          }
          onClick={activatePrimary}
        />
      </PrimaryActionScope>

      <InspectorAdvanced
        state={state}
        caseKey={activeCaseKey}
        isBusy={busy !== null}
        onRefresh={() => void refresh()}
        onExport={() => void exportReport()}
        onReset={resetCase}
        onReview={startReview}
        onPair={() => void pairAgent()}
        onDisconnect={disconnectAgent}
        onCancelCritic={cancelCritic}
      />

      <Text
        as="p"
        type="supporting"
        color="secondary"
        role="status"
        aria-label="Inspection updates"
        aria-live="polite"
        aria-atomic="true"
      >
        {announcement}
      </Text>
    </VStack>
  );
}
