import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Stack } from '@astryxdesign/core/Stack';
import { Heading } from '@astryxdesign/core/Text';
import { useNavigate, useParams } from 'react-router-dom';
import HandoffGraph from '../components/HandoffGraph';
import { PrimaryAction, PrimaryActionScope } from '../components/PrimaryAction';
import PageHeader from '../components/PageHeader';
import ReleaseControls from '../components/ReleaseControls';
import RunEvidence from '../components/RunEvidence';
import RunSelector from '../components/RunSelector';
import StageList from '../components/StageList';
import StatusLine from '../components/StatusLine';
import { createApiClient } from '../api';
import { WorkspaceEventsClient } from '../events';
import {
  ReconstructApiError,
  createReconstructApi,
} from './reconstructApi';
import type {
  ReconstructActionReceipt,
  ReconstructSelectedRun,
  ReconstructState,
} from './reconstructApi';
import { stageLabel, stepResult } from './reconstructLabels';

const RUN_ID_PATTERN = /^[0-9a-f]{32}$/;
const RETRYABLE_STATUSES = new Set(['failed', 'blocked', 'cancelled']);

interface CancelTarget {
  id: string;
  attempt: number;
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown reconstruction error';
}

function projectionIssue(state: ReconstructState | null, routeRunId: string | undefined): string | null {
  if (state === null || state.engine.state === 'down') {
    return null;
  }
  const selected = state.selectedRun;
  if (routeRunId !== undefined && selected?.id !== routeRunId) {
    return 'Selected run does not match the route. Actions and package access are disabled.';
  }
  if (selected === null) {
    return null;
  }
  const summary = state.runs.find((run) => run.id === selected.id);
  if (summary === undefined || summary.attempt !== selected.attempt) {
    return 'Selected attempt does not match the current run. Actions and package access are disabled.';
  }
  return null;
}

function packageIsVerified(run: ReconstructSelectedRun): boolean {
  const packageStage = run.stages.find((stage) => stage.name === 'package');
  return (
    run.proof.verified &&
    run.package.available &&
    run.proof.matched === run.proof.required &&
    run.proof.required === 2557 &&
    packageStage?.status === 'verified'
  );
}

export default function Reconstruct(): React.JSX.Element {
  const { id: routeRunId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const api = useMemo(() => createReconstructApi(), []);
  const [state, setState] = useState<ReconstructState | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadedRouteKey, setLoadedRouteKey] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [isMutating, setIsMutating] = useState(false);
  const [outcomeUnknown, setOutcomeUnknown] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<CancelTarget | null>(null);
  const requestVersion = useRef(0);
  const isMounted = useRef(false);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  const routeIsValid = routeRunId === undefined || RUN_ID_PATTERN.test(routeRunId);
  const routeKey = routeRunId ?? '';

  const refreshState = useCallback(async (): Promise<boolean> => {
    if (!routeIsValid) {
      setLoadError('Run route must contain 32 lowercase hexadecimal characters.');
      setIsLoading(false);
      return false;
    }
    const version = ++requestVersion.current;
    setIsLoading(true);
    setLoadError(null);
    try {
      const next = await api.readState(routeRunId);
      if (!isMounted.current || version !== requestVersion.current) {
        return false;
      }
      setState(next);
      setLoadedRouteKey(routeKey);
      setOutcomeUnknown(false);
      return next.engine.state === 'up' &&
        next.updateError === null &&
        next.builderConnected &&
        projectionIssue(next, routeRunId) === null;
    } catch (error) {
      if (isMounted.current && version === requestVersion.current) {
        setLoadError(messageFrom(error));
      }
      return false;
    } finally {
      if (isMounted.current && version === requestVersion.current) {
        setIsLoading(false);
      }
    }
  }, [api, routeIsValid, routeKey, routeRunId]);

  useEffect(() => {
    if (!routeIsValid) {
      return;
    }
    const version = ++requestVersion.current;
    let isCurrent = true;
    void api
      .readState(routeRunId)
      .then((next) => {
        if (isCurrent && isMounted.current && version === requestVersion.current) {
          setState(next);
          setLoadedRouteKey(routeKey);
          setLoadError(null);
          setOutcomeUnknown(false);
        }
      })
      .catch((error: unknown) => {
        if (isCurrent && isMounted.current && version === requestVersion.current) {
          setLoadError(messageFrom(error));
        }
      })
      .finally(() => {
        if (isCurrent && isMounted.current && version === requestVersion.current) {
          setIsLoading(false);
        }
      });
    return () => {
      isCurrent = false;
    };
  }, [api, routeIsValid, routeKey, routeRunId]);

  useEffect(() => {
    if (!routeIsValid || typeof globalThis.EventSource === 'undefined') {
      return;
    }
    const workspaceApi = createApiClient();
    let isDisposed = false;
    let eventsClient: WorkspaceEventsClient | null = null;
    let reconstructProjectIds = new Set<string>();

    void workspaceApi
      .getSnapshot()
      .then((snapshot) => {
        if (isDisposed) {
          return;
        }
        reconstructProjectIds = new Set(
          snapshot.projects
            .filter((project) => project.kind === 'reconstruct')
            .map((project) => project.id),
        );
        setStreamError(null);
        eventsClient = new WorkspaceEventsClient({
          initialCursor: snapshot.cursor,
          reloadSnapshot: () => workspaceApi.getSnapshot(),
          onSnapshot: (nextSnapshot) => {
            reconstructProjectIds = new Set(
              nextSnapshot.projects
                .filter((project) => project.kind === 'reconstruct')
                .map((project) => project.id),
            );
            setStreamError(null);
            void refreshState();
          },
          onEvent: async (event) => {
            const affectsReconstruct =
              event.type === 'engine.state_changed' ||
              event.type === 'release.changed' ||
              event.type === 'project.created' ||
              (event.project_id !== null && reconstructProjectIds.has(event.project_id));
            if (affectsReconstruct) {
              setStreamError(null);
              await refreshState();
            }
          },
          onError: (error) => {
            if (!isDisposed) {
              setStreamError(`Live updates unavailable: ${error.message}`);
            }
          },
        });
        eventsClient.start();
        void refreshState();
      })
      .catch((error: unknown) => {
        if (!isDisposed) {
          setStreamError(`Live updates unavailable: ${messageFrom(error)}`);
        }
      });

    return () => {
      isDisposed = true;
      eventsClient?.dispose();
    };
  }, [refreshState, routeIsValid]);

  const routeError = routeIsValid
    ? null
    : 'Run route must contain 32 lowercase hexadecimal characters.';
  const routeHasLoaded = loadedRouteKey === routeKey;
  const issue =
    routeError ?? (routeHasLoaded ? projectionIssue(state, routeRunId) : null);
  const selectedRun =
    issue === null && routeHasLoaded ? state?.selectedRun ?? null : null;
  const anotherRunIsActive =
    state?.running != null &&
    (selectedRun === null || state.running !== selectedRun.id);
  const readUnavailableReason =
    issue ??
    loadError ??
    streamError ??
    (state?.engine.state === 'down' ? state.engine.detail : null) ??
    state?.updateError ??
    (outcomeUnknown ? 'The previous action outcome is unknown. Reload state before acting again.' : null);
  const mutationUnavailableReason =
    readUnavailableReason ??
    (state !== null && !state.builderConnected ? 'The reconstruction builder is unavailable.' : null) ??
    (anotherRunIsActive ? 'Another reconstruction run is active.' : null);
  const isReadUnavailable =
    isLoading || !routeHasLoaded || state === null || readUnavailableReason !== null;
  const isMutationUnavailable =
    isLoading || !routeHasLoaded || state === null || mutationUnavailableReason !== null;
  const hasVerifiedPackage =
    selectedRun !== null && !isReadUnavailable && packageIsVerified(selectedRun);
  const ownsLiveRun = !isReadUnavailable && selectedRun !== null &&
    state.running === selectedRun.id && !RETRYABLE_STATUSES.has(selectedRun.status) && selectedRun.status !== 'verified';
  const reportedStage = selectedRun?.stages.find(s => s.name === selectedRun.stage && s.status === selectedRun.status);
  const reportedResult = reportedStage ? stepResult(reportedStage, ownsLiveRun) : null;
  const needsActiveRelease = state?.engine.state === 'up' && state.releases?.active == null;

  const execute = useCallback(
    async (
      successMessage: string,
      action: () => Promise<ReconstructActionReceipt>,
    ): Promise<boolean> => {
      if (isMutationUnavailable || isMutating) {
        return false;
      }
      setIsMutating(true);
      setActionError(null);
      setNotice(null);
      try {
        const receipt = await action();
        if (!isMounted.current) {
          return false;
        }
        if (receipt.run !== null && receipt.run !== routeRunId) {
          setNotice(successMessage);
          navigate(`/reconstruct/runs/${receipt.run}`);
          return true;
        }
        const refreshed = await refreshState();
        if (!isMounted.current) {
          return false;
        }
        if (!refreshed) {
          setOutcomeUnknown(true);
          setActionError(
            'Action accepted, but current state could not be verified. Reload state before trying again.',
          );
          return false;
        }
        setNotice(successMessage);
        return true;
      } catch (error) {
        if (isMounted.current) {
          const knownRejection =
            error instanceof ReconstructApiError && error.outcomeKnown;
          setActionError(
            knownRejection
              ? messageFrom(error)
              : `Action outcome unknown: ${messageFrom(error)}. Reload state before trying again.`,
          );
          setOutcomeUnknown(!knownRejection);
        }
        return false;
      } finally {
        if (isMounted.current) {
          setIsMutating(false);
        }
      }
    },
    [isMutating, isMutationUnavailable, navigate, refreshState, routeRunId],
  );

  const selectRun = (runId: string): void => {
    if (runId !== routeRunId) {
      navigate(`/reconstruct/runs/${runId}`);
    }
  };

  let primaryAction: React.JSX.Element;
  if (issue !== null) {
    primaryAction = <PrimaryAction label="State unavailable" isDisabled />;
  } else if (selectedRun !== null && hasVerifiedPackage) {
    primaryAction = (
      <PrimaryAction
        label="Download verified package"
        href={api.packageUrl(selectedRun.id)}
      />
    );
  } else if (selectedRun !== null && !isReadUnavailable && !RETRYABLE_STATUSES.has(selectedRun.status)) {
    primaryAction = <PrimaryAction label={ownsLiveRun ? 'View current step' : 'View saved steps'} href="#reconstruct-stages" />;
  } else if (isMutationUnavailable) {
    primaryAction = (
      <PrimaryAction
        label={state?.selectedRun === null || state === null ? 'Start reconstruction' : isReadUnavailable ? 'State unavailable' : 'Retry original inputs'}
        isDisabled
      />
    );
  } else if (selectedRun === null) {
    primaryAction = (
      <PrimaryAction
        label="Start reconstruction"
        isDisabled={needsActiveRelease}
        clickAction={async () => {
          await execute('Reconstruction accepted.', () => api.start());
        }}
      />
    );
  } else {
    primaryAction = (
      <PrimaryAction
        label="Retry original inputs"
        clickAction={async () => {
          await execute('Retry accepted.', () => api.retry(selectedRun.id));
        }}
      />
    );
  }

  return (
    <>
      <PageHeader
        title="Reconstruct"
        description="Rebuild Fourmilab Base64 1.5 in Rust. Compare it with the original on 2,557 tests."
      />
      <Stack gap={6}>
        {loadError !== null ? (
          <Banner
            status="error"
            title="Reconstruction state unavailable"
            description={loadError}
            container="section"
          />
        ) : null}
        {streamError !== null ? (
          <Banner
            status="warning"
            title="Live updates unavailable"
            description={streamError}
            container="section"
          />
        ) : null}
        {state?.engine.state === 'down' ? (
          <Banner
            status="error"
            title="Reconstruction engine unavailable"
            description={state.engine.detail}
            container="section"
          />
        ) : null}
        {state?.updateError !== null && state?.updateError !== undefined ? (
          <Banner
            status="warning"
            title="Reconstruction state may be stale"
            description={state.updateError}
            container="section"
          />
        ) : null}
        {state !== null && state.engine.state === 'up' && !state.builderConnected ? (
          <Banner
            status="warning"
            title="Builder unavailable"
            description="You can read saved results. To start a run or change a release, reconnect the builder."
            container="section"
          />
        ) : null}
        {issue !== null ? (
          <Banner
            status="error"
            title="The run details don’t match"
            description={issue}
            container="section"
          />
        ) : null}
        {actionError !== null ? (
          <Banner
            status="error"
            title="Action not confirmed"
            description={actionError}
            container="section"
          />
        ) : null}
        {notice !== null ? (
          <Banner
            status="success"
            title="Request accepted"
            description={notice}
            container="section"
          />
        ) : null}

        <section aria-labelledby="fixed-profile-heading">
          <Stack gap={2}>
            <Heading id="fixed-profile-heading" level={2}>
              App to rebuild
            </Heading>
            <MetadataList columns="multi" label={{ position: 'top' }}>
              <MetadataListItem label="Original app">
                {state?.profile?.name ?? 'Fourmilab Base64 1.5'}
              </MetadataListItem>
              <MetadataListItem label="Required tests">2,557</MetadataListItem>
              <MetadataListItem label="Input">Only this version of Base64 is supported.</MetadataListItem>
            </MetadataList>
          </Stack>
        </section>

        <RunSelector
          runs={state?.runs ?? []}
          selectedRunId={routeRunId ?? state?.selectedRun?.id ?? null}
          isDisabled={state?.engine.state === 'down'}
          onSelect={selectRun}
          {...(state?.engine.state === 'down'
            ? { disabledMessage: state.engine.detail }
            : {})}
        />

        <Stack gap={2}>
          <StatusLine
            label="Engine"
            value={
              state === null
                ? isLoading && loadError === null
                  ? 'Loading'
                  : 'Unavailable'
                : readUnavailableReason !== null ? 'Latest status unavailable' : 'Connected'
            }
            tone={
              state?.engine.state === 'up' && readUnavailableReason === null
                ? 'verified'
                : state === null && isLoading
                  ? 'neutral'
                  : 'fault'
            }
          />
          {selectedRun !== null ? (
            <StatusLine
              label={ownsLiveRun ? 'Current attempt' : 'Last reported step'}
              value={`Attempt ${selectedRun.attempt} · ${stageLabel(selectedRun.stage)} · ${reportedResult?.[0] ?? selectedRun.status}`}
              tone={selectedRun.status === 'verified' ? (hasVerifiedPackage ? 'verified' : 'neutral') : reportedResult?.[1] ?? 'neutral'}
            />
          ) : null}
        </Stack>

        <PrimaryActionScope label="Reconstruction actions">
          {primaryAction}
          {ownsLiveRun && selectedRun !== null ? (
            <Button
              label="Cancel current run"
              variant="destructive"
              isDisabled={isMutationUnavailable || isMutating}
              onClick={() =>
                setCancelTarget({
                  id: selectedRun.id,
                  attempt: selectedRun.attempt,
                })
              }
            />
          ) : null}
        </PrimaryActionScope>

        {selectedRun === null ? (
          !isLoading &&
            routeHasLoaded &&
            issue === null &&
            state?.engine.state === 'up' &&
            loadError === null &&
            state.updateError === null ? (
            <EmptyState
              title={state.runs.length === 0 ? 'No reconstruction runs' : 'Choose a run'}
              description={
                state.runs.length === 0
                  ? needsActiveRelease ? 'No active release. In the Fourmilab operator desk, open Releases and updates. Stage an update, then promote it before starting the first run.'
                    : state.builderConnected ? 'Start a run to test the original and compare the Rust version.' : 'Connect the builder to start the first run.'
                  : 'Choose a saved run to see its latest results.'
              }
              headingLevel={2}
              isCompact
            />
          ) : null
        ) : (
          <>
            <RunEvidence run={selectedRun} />
            <StageList stages={selectedRun.stages} liveStage={ownsLiveRun ? selectedRun.stage : null} />
            <HandoffGraph stages={selectedRun.stages} attempt={selectedRun.attempt} />
          </>
        )}

        {state?.releases !== null && state?.releases !== undefined ? (
          <ReleaseControls
            releases={state.releases}
            canStage={hasVerifiedPackage}
            isUnavailable={isMutationUnavailable}
            isPending={isMutating}
            onStage={() =>
              execute('Stage request accepted; current release state refreshed.', () =>
                api.stageRelease(),
              )
            }
            onPromote={(digest) =>
              execute('Promotion accepted.', () => api.promoteRelease(digest))
            }
            onRollback={(digest) =>
              execute('Rollback accepted.', () => api.rollbackRelease(digest))
            }
            {...(mutationUnavailableReason === null ? {} : { unavailableReason: mutationUnavailableReason })}
          />
        ) : null}
      </Stack>

      <AlertDialog
        isOpen={cancelTarget !== null}
        onOpenChange={(isOpen) => {
          if (!isOpen && !isMutating) {
            setCancelTarget(null);
          }
        }}
        title="Cancel run"
        description={
          cancelTarget === null
            ? 'No current run is available.'
            : `Cancel run ${cancelTarget.id}, attempt ${cancelTarget.attempt}. Recorded evidence remains available.`
        }
        actionLabel="Cancel this run"
        cancelLabel="Keep running"
        onAction={async () => {
          if (cancelTarget === null) {
            return;
          }
          if (
            !ownsLiveRun ||
            selectedRun?.id !== cancelTarget.id ||
            selectedRun.attempt !== cancelTarget.attempt
          ) {
            setActionError('This attempt is no longer current. Nothing was cancelled.');
            setCancelTarget(null);
            return;
          }
          await execute('Cancellation accepted.', () => api.cancel(cancelTarget.id));
          setCancelTarget(null);
        }}
        isActionLoading={isMutating}
      />
    </>
  );
}
