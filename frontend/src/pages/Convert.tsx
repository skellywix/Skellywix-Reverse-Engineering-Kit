import { useCallback, useEffect, useRef, useState } from 'react';
import type { ComponentProps } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { List, ListItem } from '@astryxdesign/core/List';
import { LinkProvider } from '@astryxdesign/core/Link';
import { Stack } from '@astryxdesign/core/Stack';
import { Heading, Text } from '@astryxdesign/core/Text';
import { TextArea } from '@astryxdesign/core/TextArea';
import { ApiError, StaleRevisionError } from '../api';
import PageHeader from '../components/PageHeader';
import { PrimaryAction, PrimaryActionScope } from '../components/PrimaryAction';
import {
  IntentKeys,
  NetworkError,
  countLineEndings,
  escapeExactText,
  isExportable,
  reconcileDetail,
  createSourcePortClient,
  deriveConvertView,
  describeError,
  draftStore,
  sha256Hex,
  useReadResource,
  utf8ByteLength,
} from './sourcePortApi';
import type {
  ConvertView,
  DownloadedPacket,
  Integrity,
  Phase,
  ProjectDetail,
  ProjectSummary,
  ProposalPacket,
  ReadResource,
  SourcePortClient,
} from './sourcePortApi';

const defaultClient = createSourcePortClient();
/** Idempotency keys outlive a project selection so a retry after an unknown outcome reuses its key. */
const intentKeys = new IntentKeys();

const PHASE_LABELS: Record<Phase, string> = {
  registered: 'Ready for a Rust proposal',
  awaiting_review: 'Waiting for review',
  approved: 'Approved',
  queued: 'Comparison queued',
  comparing: 'Running the comparison',
  mismatch: 'Mismatch',
  blocked: 'Blocked',
  verified: 'Verified against every required test',
};

interface Notice {
  kind: 'status' | 'error';
  text: string;
}

export interface ConvertProps {
  client?: SourcePortClient;
}

/** Astryx rows render plain anchors; this keeps selection inside the router so drafts survive. */
function RouterAnchor({ href, to, ...rest }: ComponentProps<'a'> & { to?: string }): React.JSX.Element {
  return <Link {...rest} to={to ?? href ?? ''} />;
}

function projectHref(projectId: string): string {
  return `/convert/projects/${encodeURIComponent(projectId)}`;
}

function saveBytes(bytes: Uint8Array<ArrayBuffer>, filename: string, type: string): void {
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

function failureText(error: unknown): string {
  if (error instanceof NetworkError) {
    return 'The workspace server cannot be reached. The outcome of this request is unknown; repeating it reuses the same idempotency key.';
  }
  return describeError(error);
}

/** A 4xx answer means the server decided; anything else leaves the outcome unknown. */
function isDefinitiveRejection(error: unknown): boolean {
  return error instanceof ApiError && error.status < 500;
}

function useDigest(text: string): { text: string; digest: string | null } | null {
  const [result, setResult] = useState<{ text: string; digest: string | null } | null>(null);
  useEffect(() => {
    let cancelled = false;
    sha256Hex(text).then(
      (digest) => {
        if (!cancelled) {
          setResult({ text, digest });
        }
      },
      () => {
        if (!cancelled) {
          setResult({ text, digest: null });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [text]);
  return result !== null && result.text === text ? result : null;
}

function useIntegrity(detail: ProjectDetail | null): Integrity {
  const attempt = detail?.attempt ?? null;
  const measured = useDigest(attempt?.rust_source ?? '');
  if (attempt === null) {
    return 'ok';
  }
  if (measured === null) {
    return 'checking';
  }
  if (measured.digest === null) {
    return 'unavailable';
  }
  return measured.digest === attempt.candidate_sha256 ? 'ok' : 'mismatch';
}

interface ExactTextProps {
  text: string;
}

/** An escaped, unambiguous view of the exact frozen text. It is a review aid, never an export. */
function ExactText({ text }: ExactTextProps): React.JSX.Element {
  const counts = countLineEndings(text);
  return (
    <Stack gap={2}>
      <Text as="p" type="supporting">
        {`${String(counts.crlf)} CRLF, ${String(counts.lf)} LF, ${String(counts.cr)} CR. ${String(utf8ByteLength(text))} bytes.`}
      </Text>
      <TextArea
        label="Exact candidate text (escaped; line breaks after \n are display only)"
        value={escapeExactText(text)}
        isReadOnly
        rows={8}
        hasSpellCheck={false}
        autoComplete="off"
      />
    </Stack>
  );
}

interface IdentityProps {
  label: string;
  value: string;
}

function Identity({ label, value }: IdentityProps): React.JSX.Element {
  return (
    <Stack direction="horizontal" gap={2} wrap="wrap" align="start">
      <Text type="supporting">{label}</Text>
      <code className="workspace-identity-value">{value}</code>
    </Stack>
  );
}

function reviewText(view: ConvertView): string | null {
  switch (view.review) {
    case 'none':
      return null;
    case 'pending':
      return 'Review pending. An operator must approve this version outside the workspace before it can be tested.';
    case 'approved':
      return 'Approved outside this page. This page cannot approve code.';
    case 'conflict':
      return 'Review state conflicts with the project phase. No comparison or download is offered until it is resolved.';
  }
}

function integrityText(integrity: Integrity): string | null {
  switch (integrity) {
    case 'ok':
      return null;
    case 'checking':
      return 'Checking that the saved code is unchanged.';
    case 'mismatch':
      return 'The candidate text does not match its recorded SHA-256 digest. Comparison and download stay unavailable.';
    case 'unavailable':
      return 'SHA-256 is unavailable in this browser context, so the candidate cannot be checked.';
  }
}

function measurementText(detail: ProjectDetail): string {
  if (detail.matched === null) {
    return `No measurements recorded. ${String(detail.required)} cases are required.`;
  }
  return `${String(detail.matched)} of ${String(detail.required)} required cases matched.`;
}

interface WorkspaceProps {
  client: SourcePortClient;
  projectId: string;
  listed: ProjectSummary | null;
  listFailed: boolean;
  onReloadList: () => void;
}

function ProjectWorkspace({
  client,
  projectId,
  listed,
  listFailed,
  onReloadList,
}: WorkspaceProps): React.JSX.Element {
  const load = useCallback(
    (signal: AbortSignal) => client.getProject(projectId, signal),
    [client, projectId],
  );
  const resource: ReadResource<ProjectDetail> = useReadResource(load, reconcileDetail);
  const [draft, setDraft] = useState(() => draftStore.get(projectId) ?? '');
  const draftRef = useRef(draft);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const activeRef = useRef(true);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
    };
  }, []);

  const detail = resource.state.value;
  const detailRef = useRef(detail);
  /** Counts losses of the project read; a packet request begun before one never reveals contents. */
  const outageRef = useRef(0);
  useEffect(() => {
    detailRef.current = detail;
    if (detail === null) {
      outageRef.current += 1;
    }
  }, [detail]);
  const [packetView, setPacketView] = useState<ProposalPacket | null>(null);
  const [exactOpen, setExactOpen] = useState(false);
  const detailId = detail?.project.id ?? null;
  const detailSource = detail?.project.source_sha256 ?? null;
  // A packet is shown only for the identity it was verified against; any other state hides it.
  const shownPacket =
    packetView !== null &&
      detailId === packetView.project_id &&
      detailSource === packetView.source_sha256
      ? packetView
      : null;
  if (detail === null && (packetView !== null || exactOpen)) {
    // Nothing stays revealed across an outage or a failed read; it is requested again on demand.
    setPacketView(null);
    setExactOpen(false);
  }
  const integrity = useIntegrity(detail);
  const draftDigest = useDigest(draft);
  const view =
    detail === null
      ? null
      : deriveConvertView({ detail, integrity, listed, draft, isBusy: busy });

  function editDraft(value: string): void {
    draftRef.current = value;
    setDraft(value);
    if (value === '') {
      draftStore.discard(projectId);
    } else {
      draftStore.set(projectId, value);
    }
    setNotice(null);
  }

  async function guarded(task: () => Promise<void>): Promise<void> {
    if (busyRef.current) {
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setNotice(null);
    try {
      await task();
    } catch (error) {
      if (activeRef.current) {
        setNotice({ kind: 'error', text: failureText(error) });
      }
      if (error instanceof StaleRevisionError && activeRef.current) {
        resource.reload();
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function fail(text: string): void {
    setNotice({ kind: 'error', text });
  }

  async function submitProposal(): Promise<void> {
    if (detail === null || view === null || !view.submit.available) {
      return;
    }
    const text = draft;
    await guarded(async () => {
      const candidate = await sha256Hex(text);
      const fingerprint = `proposal|${projectId}|${String(detail.revision)}|${detail.project.source_sha256}|${candidate}`;
      const idempotencyKey = intentKeys.keyFor(fingerprint);
      let result: ProjectDetail;
      try {
        result = await client.submitProposal(
          projectId,
          {
            expected_revision: detail.revision,
            source_sha256: detail.project.source_sha256,
            rust_source: text,
          },
          { idempotencyKey },
        );
      } catch (error) {
        if (isDefinitiveRejection(error)) {
          intentKeys.settle(fingerprint);
        }
        throw error;
      }
      intentKeys.settle(fingerprint);
      if (!activeRef.current) {
        return;
      }
      if (!resource.replace(result)) {
        fail('The project state conflicts with the one shown. Read it again before continuing.');
        return;
      }
      const frozen = result.attempt;
      if (frozen === null || frozen.rust_source !== text || frozen.candidate_sha256 !== candidate) {
        fail('The attempt the server recorded does not match what was submitted. Review it before continuing.');
        return;
      }
      if (draftRef.current === text) {
        draftRef.current = '';
        setDraft('');
        draftStore.discard(projectId);
      }
      setNotice({
        kind: 'status',
        text: `Proposal frozen as attempt ${frozen.id} at revision ${String(result.revision)}.`,
      });
    });
  }

  async function requestComparison(): Promise<void> {
    const attempt = detail?.attempt ?? null;
    if (
      detail === null ||
      attempt === null ||
      view === null ||
      view.primary.kind !== 'compare' ||
      view.primary.disabledReason !== null
    ) {
      return;
    }
    await guarded(async () => {
      const fingerprint = `compare|${projectId}|${String(detail.revision)}|${attempt.id}|${attempt.candidate_sha256}`;
      const idempotencyKey = intentKeys.keyFor(fingerprint);
      let result: ProjectDetail;
      try {
        result = await client.requestComparison(
          projectId,
          {
            expected_revision: detail.revision,
            attempt_id: attempt.id,
            candidate_sha256: attempt.candidate_sha256,
          },
          { idempotencyKey },
        );
      } catch (error) {
        if (isDefinitiveRejection(error)) {
          intentKeys.settle(fingerprint);
        }
        throw error;
      }
      intentKeys.settle(fingerprint);
      if (!activeRef.current) {
        return;
      }
      if (!resource.replace(result)) {
        fail('The project state conflicts with the one shown. Read it again before continuing.');
        return;
      }
      if (
        result.attempt?.id !== attempt.id ||
        result.attempt.candidate_sha256 !== attempt.candidate_sha256
      ) {
        fail('The comparison was recorded for a different attempt than the one approved. Review the project before continuing.');
        return;
      }
      setNotice({
        kind: 'status',
        text: `Comparison requested at revision ${String(result.revision)}.`,
      });
    });
  }

  async function downloadSource(): Promise<void> {
    const attempt = detail?.attempt ?? null;
    if (detail === null || attempt === null || view === null || !view.canDownload) {
      return;
    }
    await guarded(async () => {
      const bytes = await client.downloadSource(projectId, attempt.id);
      if ((await sha256Hex(bytes)) !== attempt.candidate_sha256) {
        fail('The downloaded bytes do not match the verified candidate. Nothing was saved.');
        return;
      }
      const fresh = await client.getProject(projectId);
      if (!activeRef.current) {
        return;
      }
      if (!resource.replace(fresh)) {
        fail('The project state conflicts with the one shown. Nothing was saved.');
        return;
      }
      if (
        fresh.revision !== detail.revision ||
        !isExportable(fresh) ||
        fresh.attempt?.id !== attempt.id ||
        fresh.attempt.candidate_sha256 !== attempt.candidate_sha256
      ) {
        fail('The project changed while downloading. Nothing was saved.');
        return;
      }
      saveBytes(bytes, 'port.rs', 'text/plain;charset=utf-8');
      setNotice({ kind: 'status', text: 'Saved port.rs.' });
    });
  }

  /** Fetches the public packet and returns it only if it is the current project's, digest-checked. */
  async function fetchVerifiedPacket(): Promise<DownloadedPacket | null> {
    if (detail === null) {
      return null;
    }
    const identity = detail.project;
    const outage = outageRef.current;
    const result = await client.getPacket(projectId);
    const actual = await sha256Hex(result.packet.source);
    const latest = detailRef.current;
    if (
      !activeRef.current ||
      outageRef.current !== outage ||
      latest === null ||
      latest.project.id !== identity.id ||
      latest.project.source_sha256 !== identity.source_sha256
    ) {
      return null;
    }
    if (
      result.packet.project_id !== identity.id ||
      result.packet.source_sha256 !== identity.source_sha256 ||
      actual !== result.packet.source_sha256
    ) {
      fail('The proposal packet does not match the registered source identity. Nothing was shown or saved.');
      return null;
    }
    return result;
  }

  async function togglePacket(): Promise<void> {
    if (shownPacket !== null) {
      setPacketView(null);
      return;
    }
    await guarded(async () => {
      const result = await fetchVerifiedPacket();
      if (result !== null) {
        setPacketView(result.packet);
      }
    });
  }

  async function downloadPacket(): Promise<void> {
    if (detail === null) {
      return;
    }
    const project = detail.project;
    await guarded(async () => {
      const result = await fetchVerifiedPacket();
      if (result === null) {
        return;
      }
      const filename = `${project.id.replace(/[^A-Za-z0-9._-]/g, '_')}-proposal-packet.json`;
      saveBytes(result.bytes, filename, 'application/json');
      setNotice({ kind: 'status', text: `Saved ${filename}.` });
    });
  }

  function runPrimary(): void {
    if (view === null) {
      return;
    }
    switch (view.primary.kind) {
      case 'submit':
        void submitProposal();
        break;
      case 'compare':
        void requestComparison();
        break;
      case 'download':
        void downloadSource();
        break;
      case 'refresh':
        resource.reload();
        break;
    }
  }

  function retry(): void {
    resource.reload();
    if (listFailed) {
      onReloadList();
    }
  }

  const failed = resource.state.phase === 'offline' || resource.state.phase === 'error';
  const draftShown = draft !== '' && draftDigest !== null;
  const notes = view === null ? [] : [reviewText(view), integrityText(integrity)];
  const helper =
    view !== null && !view.identityConflict && integrity === 'ok' ? view.primary.disabledReason : null;

  return (
    <Stack as="section" gap={4}>
      {detail === null && resource.state.phase === 'loading' ? (
        <Text color="secondary">Loading project.</Text>
      ) : null}
      {failed ? (
        <Stack gap={2} align="start">
          <Banner status="error" role="alert" title={resource.state.message ?? 'The request failed.'} />
          <Button label="Retry" onClick={retry} />
        </Stack>
      ) : null}
      {detail !== null && view !== null ? (
        <Stack gap={3}>
          <Heading level={2}>{detail.project.name}</Heading>
          <Identity label="Original source SHA-256" value={detail.project.source_sha256} />
          <Text weight="semibold">
            {detail.phase === 'verified' && !isExportable(detail)
              ? 'Verification not confirmed'
              : PHASE_LABELS[detail.phase]}
          </Text>
          {detail.attempt === null ? (
            <Text>No proposal has been submitted. Paste Rust code below and submit it for review. Submitting does not run it.</Text>
          ) : null}
          {notes.map((note) =>
            note === null ? null : (
              <Text key={note} as="p">
                {note}
              </Text>
            ),
          )}
          {view.identityConflict ? (
            <Text as="p">
              The project identity differs between the registry list and its detail. Reload before
              continuing.
            </Text>
          ) : null}
          <Text as="p">{measurementText(detail)}</Text>
          {detail.blocker !== null ? <Text as="p">{detail.blocker}</Text> : null}
          {detail.attempt !== null ? (
            <Stack gap={2}>
              <Identity label="Candidate SHA-256" value={detail.attempt.candidate_sha256} />
              <TextArea
                label="Frozen candidate (reading view; line endings are normalized and it is not the exact text)"
                value={detail.attempt.rust_source}
                isReadOnly
                rows={8}
                hasSpellCheck={false}
                autoComplete="off"
              />
              <Stack direction="horizontal" gap={2} wrap="wrap">
                <Button
                  label={exactOpen ? 'Hide exact line endings' : 'Show exact line endings'}
                  onClick={() => {
                    setExactOpen((open) => !open);
                  }}
                />
              </Stack>
              {exactOpen ? <ExactText text={detail.attempt.rust_source} /> : null}
            </Stack>
          ) : null}
        </Stack>
      ) : null}
      <Stack gap={2}>
        <TextArea
          label="Rust proposal (untrusted plain text)"
          description="Held in this tab only. It is sent to this workspace when you submit and is never run here."
          value={draft}
          onChange={editDraft}
          rows={12}
          hasSpellCheck={false}
          autoComplete="off"
        />
        {draftShown ? <Identity label="Draft SHA-256" value={draftDigest.digest ?? ''} /> : null}
        {draftShown && draftDigest.digest === null ? (
          <Text as="p">SHA-256 is unavailable in this browser context.</Text>
        ) : null}
      </Stack>
      {detail !== null && view !== null ? (
        <Stack gap={2} align="start">
          <PrimaryActionScope>
            <PrimaryAction
              label={view.primary.label}
              isDisabled={view.primary.disabledReason !== null}
              onClick={runPrimary}
            />
          </PrimaryActionScope>
          {helper === null ? null : <Text type="supporting">{helper}</Text>}
          <Stack direction="horizontal" gap={2} wrap="wrap">
            {view.editable && view.primary.kind !== 'submit' ? (
              <Button
                label={view.submit.label}
                isDisabled={!view.submit.available}
                onClick={() => {
                  void submitProposal();
                }}
              />
            ) : null}
            {view.primary.kind !== 'refresh' ? (
              <Button label="Refresh status" isDisabled={busy} onClick={resource.reload} />
            ) : null}
            <Button
              label={shownPacket === null ? 'View proposal packet' : 'Hide proposal packet'}
              isDisabled={busy}
              onClick={() => {
                void togglePacket();
              }}
            />
            <Button
              label="Download proposal packet"
              isDisabled={busy}
              onClick={() => {
                void downloadPacket();
              }}
            />
          </Stack>
          {shownPacket === null ? null : (
            <Stack gap={2}>
              <Text as="p" type="supporting">
                Public proposal packet for {shownPacket.project_id} ({shownPacket.language}). Shown as plain
                text, checked against the registered source SHA-256, and never run or sent to a provider.
              </Text>
              <TextArea
                label="Packet source (public, untrusted text)"
                value={shownPacket.source}
                isReadOnly
                rows={8}
                hasSpellCheck={false}
                autoComplete="off"
              />
              <TextArea
                label="Packet brief (public, untrusted text)"
                value={shownPacket.brief}
                isReadOnly
                rows={4}
                hasSpellCheck={false}
                autoComplete="off"
              />
            </Stack>
          )}
        </Stack>
      ) : null}
      {detail !== null && notice?.kind === 'error' ? (
        <Banner status="error" role="alert" title={notice.text} />
      ) : null}
      <output>{detail !== null && notice?.kind === 'status' ? notice.text : ''}</output>
    </Stack>
  );
}

export default function Convert({ client = defaultClient }: ConvertProps): React.JSX.Element {
  const { id } = useParams();
  const loadList = useCallback((signal: AbortSignal) => client.listProjects(signal), [client]);
  const list = useReadResource<ProjectSummary[]>(loadList);
  const projects = list.state.value;
  const selected = id ?? null;
  const listed =
    selected === null || projects === null
      ? null
      : (projects.find((project) => project.id === selected) ?? null);
  const listFailed = list.state.phase === 'offline' || list.state.phase === 'error';

  return (
    <>
      <PageHeader
        title="Convert"
        description="Compare a proposed Rust version with the original app. An operator must approve the code before testing."
      />
      <Stack gap={6}>
        <section aria-label="Registered source projects">
          {list.state.phase === 'loading' ? <Text color="secondary">Loading projects.</Text> : null}
          {listFailed && selected === null ? (
            <Stack gap={2} align="start">
              <Banner
                status="error"
                role="alert"
                title={list.state.message ?? 'The request failed.'}
              />
              <Button label="Retry" onClick={list.reload} />
            </Stack>
          ) : null}
          {listFailed && selected !== null ? (
            <Stack direction="horizontal" gap={2} align="center" wrap="wrap">
              <Text>Project list unavailable.</Text>
              <Button label="Reload list" onClick={list.reload} />
            </Stack>
          ) : null}
          {projects !== null && projects.length === 0 ? (
            <Text>No source projects are registered.</Text>
          ) : null}
          {projects !== null && projects.length > 0 ? (
            <LinkProvider component={RouterAnchor}>
              <List>
                {projects.map((project) => (
                  <ListItem
                    key={project.id}
                    label={project.name}
                    description={<Text type="supporting">{project.source_sha256.slice(0, 12)}</Text>}
                    href={projectHref(project.id)}
                    isSelected={project.id === selected}
                  />
                ))}
              </List>
            </LinkProvider>
          ) : null}
        </section>
        {selected === null ? null : (
          <ProjectWorkspace
            key={selected}
            client={client}
            projectId={selected}
            listed={listed}
            listFailed={listFailed}
            onReloadList={list.reload}
          />
        )}
      </Stack>
    </>
  );
}
