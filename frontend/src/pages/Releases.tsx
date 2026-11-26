import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Banner } from '@astryxdesign/core/Banner';
import { Code } from '@astryxdesign/core/Code';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Stack } from '@astryxdesign/core/Stack';
import { Heading, Text } from '@astryxdesign/core/Text';
import { createApiClient } from '../api';
import PageHeader from '../components/PageHeader';
import ReleaseControls from '../components/ReleaseControls';
import StatusLine from '../components/StatusLine';
import type { WorkspaceSnapshot } from '../types';
import {
  ReconstructApiError,
  createReconstructApi,
  decodeReconstructState,
} from './reconstructApi';
import type {
  ReconstructReleases,
  ReconstructSelectedRun,
  ReconstructState,
} from './reconstructApi';
import {
  parseEngineReleasePresentation,
  redactPrivatePaths,
  workspaceIdentity,
} from './releasePresentation';
import type {
  EngineReleasePresentation,
  EngineReleaseReceipt,
  WorkspaceIdentity,
} from './releasePresentation';

const RECONSTRUCT_STATE_URL = '/api/v1/reconstruct/state';

interface EngineReleaseData {
  state: ReconstructState;
  presentation: EngineReleasePresentation;
}

interface PageMessage {
  title: string;
  description: string;
}

type ReleaseAction = 'stage' | 'promote' | 'rollback';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function messageFrom(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.length > 0
    ? redactPrivatePaths(error.message)
    : fallback;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

async function readEngineReleaseData(signal?: AbortSignal): Promise<EngineReleaseData> {
  const response = await globalThis.fetch(RECONSTRUCT_STATE_URL, {
    headers: { Accept: 'application/json' },
    ...(signal === undefined ? {} : { signal }),
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ReconstructApiError(
      'The reconstruction service returned invalid JSON',
      response.status,
    );
  }
  if (!response.ok) {
    const body = isRecord(payload) ? payload : {};
    const rawMessage =
      typeof body.message === 'string'
        ? body.message
        : typeof body.error === 'string'
          ? body.error
          : `Reconstruction request failed with status ${response.status}`;
    throw new ReconstructApiError(
      rawMessage,
      response.status,
      typeof body.error === 'string' ? body.error : null,
      true,
    );
  }
  return {
    state: decodeReconstructState(payload),
    presentation: parseEngineReleasePresentation(payload),
  };
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

function releaseStateIssue(state: ReconstructState): string | null {
  if (state.engine.state === 'down') {
    return state.engine.detail;
  }
  if (state.updateError !== null) {
    return state.updateError;
  }
  if (state.busy) {
    return 'The engine is busy.';
  }
  if (!state.builderConnected) {
    return 'The reconstruction builder is unavailable.';
  }
  return null;
}

function selectorRoles(receipt: EngineReleaseReceipt, releases: ReconstructReleases): string {
  const roles: string[] = [];
  if (releases.active === receipt.digest) {
    roles.push('Active');
  }
  if (releases.staged === receipt.digest) {
    roles.push('Staged');
  }
  if (releases.previous === receipt.digest) {
    roles.push('Previous');
  }
  return roles.length === 0 ? 'Catalogued only' : roles.join(' · ');
}

function ReleaseReceiptView({
  receipt,
  releases,
  index,
}: {
  receipt: EngineReleaseReceipt;
  releases: ReconstructReleases;
  index: number;
}): React.JSX.Element {
  const headingId = `fourmilab-release-receipt-${index}`;
  return (
    <section aria-labelledby={headingId}>
      <Stack gap={2}>
        <Heading id={headingId} level={4}>
          Release receipt {index + 1}
        </Heading>
        <MetadataList label={{ position: 'top' }}>
          <MetadataListItem label="Selector state">
            {selectorRoles(receipt, releases)}
          </MetadataListItem>
          <MetadataListItem label="Receipt identity">
            <Code>{receipt.digest}</Code>
          </MetadataListItem>
          {receipt.kitCommit === null ? null : (
            <MetadataListItem label="Kit source commit">
              <Code>{receipt.kitCommit}</Code>
            </MetadataListItem>
          )}
          {receipt.reaVersion === null ? null : (
            <MetadataListItem label="REA version">{receipt.reaVersion}</MetadataListItem>
          )}
          {receipt.reaCommit === null ? null : (
            <MetadataListItem label="REA source commit">
              <Code>{receipt.reaCommit}</Code>
            </MetadataListItem>
          )}
          {receipt.sourceSha256 === null ? null : (
            <MetadataListItem label="REA source archive SHA-256">
              <Code>{receipt.sourceSha256}</Code>
            </MetadataListItem>
          )}
          {receipt.files.map((file) => (
            <MetadataListItem key={file.name} label={`${file.name} SHA-256`}>
              <Code>{file.sha256}</Code>
            </MetadataListItem>
          ))}
          <MetadataListItem label="Image identity">
            {receipt.imageIdentity === null ? (
              'Image identity is not recorded by this Fourmilab release receipt.'
            ) : (
              <Code>{receipt.imageIdentity}</Code>
            )}
          </MetadataListItem>
          <MetadataListItem label="Probe receipt">
            {receipt.probeReceipt === null ? (
              'Probe receipt is not recorded by this Fourmilab release receipt.'
            ) : (
              <Code>{receipt.probeReceipt}</Code>
            )}
          </MetadataListItem>
        </MetadataList>
      </Stack>
    </section>
  );
}

function WorkspaceIdentityValue({
  identity,
}: {
  identity: WorkspaceIdentity;
}): React.JSX.Element | string {
  if (identity.kind === 'absent') {
    return 'None recorded';
  }
  if (identity.kind === 'unavailable') {
    return 'Recorded identity unavailable';
  }
  return <Code>{identity.digest}</Code>;
}

export default function Releases(): React.JSX.Element {
  const engineApi = useMemo(() => createReconstructApi(), []);
  const workspaceApi = useMemo(() => createApiClient(), []);
  const [engineData, setEngineData] = useState<EngineReleaseData | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceSnapshot | null>(null);
  const [engineLoading, setEngineLoading] = useState(true);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<PageMessage | null>(null);
  const [notice, setNotice] = useState<PageMessage | null>(null);
  const [isMutating, setIsMutating] = useState(false);
  const [outcomeUnknown, setOutcomeUnknown] = useState(false);
  const isMounted = useRef(false);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let isCurrent = true;
    void readEngineReleaseData(controller.signal)
      .then((next) => {
        if (isCurrent) {
          setEngineData(next);
          setEngineError(null);
        }
      })
      .catch((error: unknown) => {
        if (isCurrent && !isAbort(error)) {
          setEngineError(messageFrom(error, 'Fourmilab release data could not be read.'));
        }
      })
      .finally(() => {
        if (isCurrent) {
          setEngineLoading(false);
        }
      });
    void workspaceApi
      .getSnapshot(controller.signal)
      .then((next) => {
        if (isCurrent) {
          setWorkspace(next);
          setWorkspaceError(null);
        }
      })
      .catch((error: unknown) => {
        if (isCurrent && !isAbort(error)) {
          setWorkspaceError(messageFrom(error, 'Workspace snapshot could not be read.'));
        }
      })
      .finally(() => {
        if (isCurrent) {
          setWorkspaceLoading(false);
        }
      });
    return () => {
      isCurrent = false;
      controller.abort();
    };
  }, [workspaceApi]);

  const issue = engineData === null ? null : releaseStateIssue(engineData.state);
  const reportStale = engineError !== null || engineData?.state.updateError != null || outcomeUnknown;
  const unavailableReason =
    engineError ??
    issue ??
    (outcomeUnknown
      ? 'Current release state was not verified. Reload before acting again.'
      : null);
  const actionsUnavailable =
    engineLoading ||
    engineData === null ||
    unavailableReason !== null;
  const canStage =
    !actionsUnavailable &&
    engineData?.state.selectedRun !== null &&
    engineData?.state.selectedRun !== undefined &&
    packageIsVerified(engineData.state.selectedRun);

  const runAction = useCallback(
    async (kind: ReleaseAction, confirmedTarget: string | null): Promise<boolean> => {
      if (actionsUnavailable || isMutating) {
        return false;
      }
      setIsMutating(true);
      setActionError(null);
      setNotice(null);
      let actionStarted = false;
      let actionAccepted = false;
      try {
        const current = await readEngineReleaseData();
        if (!isMounted.current) {
          return false;
        }
        setEngineData(current);
        setEngineError(null);
        const currentIssue = releaseStateIssue(current.state);
        if (currentIssue !== null || current.state.releases === null) {
          setActionError({
            title: 'Current release data unavailable',
            description: `${redactPrivatePaths(currentIssue ?? 'No release selectors were returned.')} No action was sent.`,
          });
          return false;
        }
        if (kind === 'stage') {
          if (
            current.state.selectedRun === null ||
            !packageIsVerified(current.state.selectedRun)
          ) {
            setActionError({
              title: 'Verified package changed',
              description:
                'The current run no longer has a verified package. No stage action was sent.',
            });
            return false;
          }
        } else {
          const currentTarget =
            kind === 'promote'
              ? current.state.releases.staged
              : current.state.releases.previous;
          if (confirmedTarget === null || currentTarget !== confirmedTarget) {
            setActionError({
              title: 'Release target changed',
              description:
                kind === 'promote'
                  ? 'The staged target changed before promotion. Review current data; no action was sent.'
                  : 'The previous target changed before rollback. Review current data; no action was sent.',
            });
            return false;
          }
        }

        actionStarted = true;
        if (kind === 'stage') {
          await engineApi.stageRelease();
        } else if (kind === 'promote') {
          await engineApi.promoteRelease(confirmedTarget!);
        } else {
          await engineApi.rollbackRelease(confirmedTarget!);
        }
        actionAccepted = true;

        const refreshed = await readEngineReleaseData();
        if (!isMounted.current) {
          return false;
        }
        setEngineData(refreshed);
        setEngineError(null);
        const refreshedReleases = refreshed.state.releases;
        const refreshedIssue = releaseStateIssue(refreshed.state);
        const currentStateVerified =
          refreshed.state.engine.state === 'up' &&
          refreshedReleases !== null &&
          refreshedIssue === null;
        const resultVerified =
          currentStateVerified &&
          (kind === 'stage' || refreshedReleases?.active === confirmedTarget);
        if (!resultVerified) {
          setOutcomeUnknown(true);
          setActionError({
            title: 'Release result not verified',
            description:
              'The action was accepted, but current release state could not verify its expected result. Further release actions are disabled.',
          });
          return false;
        }

        setOutcomeUnknown(false);
        if (kind === 'stage') {
          setNotice({
            title: 'Stage request accepted',
            description: `The response did not identify a staged target. Current staged selector: ${refreshedReleases?.staged ?? 'None recorded'}.`,
          });
        } else {
          setNotice({
            title: 'Release action verified',
            description:
              kind === 'promote'
                ? 'Promotion verified in current engine state.'
                : 'Rollback verified in current engine state.',
          });
        }
        return true;
      } catch (error) {
        if (!isMounted.current) {
          return false;
        }
        if (!actionStarted) {
          const recheckError = messageFrom(
            error,
            'Current release data could not be rechecked.',
          );
          setEngineError(recheckError);
          setOutcomeUnknown(true);
          setActionError({
            title: 'Current release data unavailable',
            description: `${recheckError} No action was sent.`,
          });
        } else if (
          !actionAccepted &&
          error instanceof ReconstructApiError &&
          error.outcomeKnown
        ) {
          setActionError({
            title: 'Release action rejected',
            description: messageFrom(error, 'The release action was rejected.'),
          });
        } else {
          const resultError = messageFrom(error, 'current engine state could not be read');
          setEngineError(resultError);
          setOutcomeUnknown(true);
          setActionError({
            title: 'Release result not verified',
            description: `Action outcome unknown: ${resultError}. Further release actions are disabled.`,
          });
        }
        return false;
      } finally {
        if (isMounted.current) {
          setIsMutating(false);
        }
      }
    },
    [actionsUnavailable, engineApi, isMutating],
  );

  const releases = engineData?.state.releases ?? null;
  const presentation = engineData?.presentation ?? null;
  const activeIdentity =
    workspace === null ? null : workspaceIdentity(workspace.releases.active);
  const previousIdentity =
    workspace === null ? null : workspaceIdentity(workspace.releases.previous);
  const stagedIdentity =
    workspace === null ? null : workspaceIdentity(workspace.releases.staged);
  const workspaceIsEmpty =
    activeIdentity?.kind === 'absent' &&
    previousIdentity?.kind === 'absent' &&
    stagedIdentity?.kind === 'absent';

  return (
    <>
      <PageHeader
        title="Releases"
        description="See which engine version is selected for new runs. Workspace versions are listed separately."
      />
      <Stack gap={6}>
        {engineError === null ? null : (
          <Banner
            status="error"
            title="Fourmilab release data unavailable"
            description={engineError}
            container="section"
          />
        )}
        {engineData?.state.engine.state === 'down' ? (
          <Banner
            status="error"
            title="Fourmilab engine unavailable"
            description={redactPrivatePaths(engineData.state.engine.detail)}
            container="section"
          />
        ) : null}
        {engineData?.state.updateError === null ||
          engineData?.state.updateError === undefined ? null : (
          <Banner
            status="warning"
            title="Fourmilab release data may be stale"
            description={redactPrivatePaths(engineData.state.updateError)}
            container="section"
          />
        )}
        {actionError === null ? null : (
          <Banner
            status="error"
            title={actionError.title}
            description={actionError.description}
            container="section"
          />
        )}
        {notice === null ? null : (
          <Banner
            status="success"
            title={notice.title}
            description={notice.description}
            container="section"
          />
        )}

        <section aria-labelledby="fourmilab-releases-heading">
          <Stack gap={3}>
            <Heading id="fourmilab-releases-heading" level={2}>
              Fixed Fourmilab engine releases
            </Heading>
            <Text as="p" color="secondary">
              These are the versions the Base64 engine reported. A version can be listed without being active.
            </Text>
            <StatusLine
              label="Engine release data"
              value={
                engineLoading
                  ? 'Loading'
                  : engineData === null || engineData.state.engine.state === 'down'
                    ? 'Unavailable'
                    : reportStale
                      ? 'Stale or unavailable'
                      : 'Current snapshot'
              }
              tone={
                engineData?.state.engine.state === 'up' &&
                  !reportStale
                  ? 'verified'
                  : engineLoading
                    ? 'neutral'
                    : 'fault'
              }
            />
            {!reportStale && engineData?.state.engine.state === 'up' && issue !== null ? (
              <Text as="p">Release changes are paused. {issue}</Text>
            ) : null}

            {releases === null || presentation === null ? null : (
              <>
                <ReleaseControls
                  releases={releases}
                  canStage={canStage}
                  isUnavailable={actionsUnavailable}
                  isPending={isMutating}
                  onStage={() => runAction('stage', null)}
                  onPromote={(target) => runAction('promote', target)}
                  onRollback={(target) => runAction('rollback', target)}
                  {...(unavailableReason === null
                    ? {}
                    : { unavailableReason: redactPrivatePaths(unavailableReason) })}
                />

                <section aria-labelledby="recorded-engine-receipts-heading">
                  <Stack gap={3}>
                    <Heading id="recorded-engine-receipts-heading" level={3}>
                      Recorded release receipts
                    </Heading>
                    {presentation.recordedReceiptCount === 0 ? (
                      <EmptyState
                        title="No engine release receipts recorded"
                        description="The engine is available, but its release catalog contains no receipt items."
                        headingLevel={4}
                        isCompact
                      />
                    ) : presentation.receipts.length === 0 ? (
                      <Banner
                        status="error"
                        title="Engine release receipts unavailable"
                        description="Recorded receipt items could not be safely decoded."
                        container="section"
                      />
                    ) : (
                      presentation.receipts.map((receipt, index) => (
                        <ReleaseReceiptView
                          key={receipt.digest}
                          receipt={receipt}
                          releases={releases}
                          index={index}
                        />
                      ))
                    )}
                    {presentation.recordedReceiptCount > presentation.receipts.length &&
                      presentation.receipts.length > 0 ? (
                      <Banner
                        status="warning"
                        title="Some engine receipts unavailable"
                        description="One or more recorded receipt items could not be safely decoded and are not displayed."
                        container="section"
                      />
                    ) : null}
                  </Stack>
                </section>

                <section aria-labelledby="worker-catalog-heading">
                  <Stack gap={3}>
                    <Heading id="worker-catalog-heading" level={3}>
                      Declared worker catalog
                    </Heading>
                    {presentation.recordedCatalogCount === 0 ? (
                      <EmptyState
                        title="No declared worker catalog"
                        description="The engine returned no declared worker references."
                        headingLevel={4}
                        isCompact
                      />
                    ) : presentation.catalog.length === 0 ? (
                      <Banner
                        status="error"
                        title="Declared worker catalog unavailable"
                        description="Recorded worker entries could not be safely decoded."
                        container="section"
                      />
                    ) : (
                      <MetadataList label={{ position: 'top' }}>
                        {presentation.catalog.map((entry) => (
                          <MetadataListItem
                            key={`${entry.role}:${entry.skill}`}
                            label={entry.role}
                          >
                            <Stack gap={1}>
                              <Text as="p">{entry.skill}</Text>
                              <Text as="p" color="secondary">
                                {entry.scope}
                              </Text>
                            </Stack>
                          </MetadataListItem>
                        ))}
                      </MetadataList>
                    )}
                    {presentation.recordedCatalogCount > presentation.catalog.length &&
                      presentation.catalog.length > 0 ? (
                      <Banner
                        status="warning"
                        title="Some worker entries unavailable"
                        description="One or more recorded worker entries could not be safely decoded and are not displayed."
                        container="section"
                      />
                    ) : null}
                  </Stack>
                </section>
              </>
            )}
          </Stack>
        </section>

        <section aria-labelledby="workspace-shell-releases-heading">
          <Stack gap={3}>
            <Heading id="workspace-shell-releases-heading" level={2}>
              Workspace shell snapshot
            </Heading>
            <Banner
              status="warning"
              title="Saved versions, not live status"
              description="These are saved version numbers, not proof of what is running. Only the release command can switch the workspace version."
              container="section"
            />
            <StatusLine
              label="Workspace snapshot"
              value={
                workspaceLoading
                  ? 'Loading'
                  : workspace === null
                    ? 'Unavailable'
                    : `Recorded at ${workspace.generated_at}`
              }
              tone={workspace === null ? (workspaceLoading ? 'neutral' : 'fault') : 'neutral'}
            />
            {workspaceError === null ? null : (
              <Banner
                status="error"
                title="Workspace shell snapshot unavailable"
                description={workspaceError}
                container="section"
              />
            )}
            {workspace === null ||
              activeIdentity === null ||
              previousIdentity === null ||
              stagedIdentity === null ? null : (
              <>
                <MetadataList columns="multi" label={{ position: 'top' }}>
                  <MetadataListItem label="Active identity">
                    <WorkspaceIdentityValue identity={activeIdentity} />
                  </MetadataListItem>
                  <MetadataListItem label="Previous identity">
                    <WorkspaceIdentityValue identity={previousIdentity} />
                  </MetadataListItem>
                  <MetadataListItem label="Staged identity">
                    <WorkspaceIdentityValue identity={stagedIdentity} />
                  </MetadataListItem>
                </MetadataList>
                {workspaceIsEmpty ? (
                  <Text as="p" color="secondary">
                    No workspace-shell target identities are recorded in this snapshot.
                  </Text>
                ) : null}
              </>
            )}
          </Stack>
        </section>
      </Stack>
    </>
  );
}
