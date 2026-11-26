import { Fragment, useEffect, useRef, useState } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { Collapsible } from '@astryxdesign/core/Collapsible';
import { FormLayout } from '@astryxdesign/core/FormLayout';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Heading, Text } from '@astryxdesign/core/Text';
import type { InspectState } from '../pages/inspectApi';

export interface InspectorAdvancedProps {
  state: InspectState | null;
  caseKey: string | null;
  isBusy: boolean;
  onRefresh: () => void;
  onExport: () => void;
  onReset: () => void;
  onReview: (reference: string | null, critique: boolean) => void;
  onPair: () => void;
  onDisconnect: () => void;
  onCancelCritic: (end: number) => void;
}

const CONNECTION_LABELS = {
  off: 'No key issued',
  ready: 'Key issued · awaiting agent',
  connected: 'Agent paired',
  expired: 'Key expired',
  spent: 'Key spent',
} as const;

const CRITIC_LABELS = {
  idle: '',
  not_requested: 'Pass logged. No model critique requested.',
  pending:
    'No critic connected yet. No model note. The request expires after two minutes.',
  reviewing: 'The critic is reading this ledger snapshot. No model note yet.',
  ready: '',
  failed:
    'Critique not completed: the critic declined, was unavailable, or returned an invalid reply. No model note.',
  expired: 'Critique request expired before a note arrived. No note is current.',
  cancelled: 'Critique cancelled.',
  stale: 'The ledger changed. The previous model note no longer applies.',
} as const;

const DIGEST_PATTERN = /^[0-9a-f]{64}$/i;

export default function InspectorAdvanced({
  state,
  caseKey,
  isBusy,
  onRefresh,
  onExport,
  onReset,
  onReview,
  onPair,
  onDisconnect,
  onCancelCritic,
}: InspectorAdvancedProps): React.JSX.Element {
  const [confirmResetRevision, setConfirmResetRevision] = useState<number | null>(null);
  const [reference, setReference] = useState('');
  const [withCritic, setWithCritic] = useState(false);
  const [revealKey, setRevealKey] = useState(false);
  const [isAdvancedOpen, setIsAdvancedOpen] = useState(false);
  const revision = state?.revision;
  const confirmReset = revision !== undefined && confirmResetRevision === revision;
  const resetButtonRef = useRef<HTMLButtonElement>(null);
  const refreshButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const pendingFocus = useRef<'confirm' | 'reset' | 'refresh' | null>(null);

  useEffect(() => {
    const target = pendingFocus.current;
    if (target === 'refresh' && isBusy) return;
    if (target === 'confirm') confirmButtonRef.current?.focus();
    if (target === 'reset') resetButtonRef.current?.focus();
    if (target === 'refresh') refreshButtonRef.current?.focus();
    pendingFocus.current = null;
  }, [confirmReset, isBusy]);

  const normalizedReference = reference.trim().toLowerCase();
  const isReferenceValid =
    normalizedReference.length === 0 || DIGEST_PATTERN.test(normalizedReference);
  const critique = state?.advisor.critique;
  const isCriticPending = critique?.status === 'pending' || critique?.status === 'reviewing';
  const hasActiveKey = state?.connection === 'ready' || state?.connection === 'connected';

  return (
    <Collapsible
      trigger={<Text weight="semibold">Advanced inspection controls</Text>}
      isOpen={isAdvancedOpen}
      onOpenChange={setIsAdvancedOpen}
    >
      <VStack gap={6}>
        <VStack gap={3} as="section">
          <Heading level={3}>Case maintenance</Heading>
          <HStack gap={3} wrap="wrap">
            <Button
              ref={refreshButtonRef}
              label="Refresh inspector"
              variant="secondary"
              isDisabled={isBusy}
              onClick={onRefresh}
            />
            <Button
              label="Export evidence (JSON)"
              variant="secondary"
              isDisabled={isBusy || state?.slots.a === undefined}
              onClick={onExport}
            />
            {!confirmReset ? (
              <Button
                ref={resetButtonRef}
                label="Reset case"
                variant="secondary"
                isDisabled={isBusy || state?.slots.a === undefined}
                onClick={() => {
                  pendingFocus.current = 'confirm';
                  setConfirmResetRevision(revision ?? null);
                }}
              />
            ) : (
              <>
                <Text as="p">Clear both specimens and the evidence ledger?</Text>
                <Button
                  ref={confirmButtonRef}
                  label="Confirm reset"
                  variant="destructive"
                  isDisabled={isBusy}
                  onClick={() => {
                    pendingFocus.current = 'refresh';
                    onReset();
                    setConfirmResetRevision(null);
                  }}
                />
                <Button
                  label="Keep case"
                  variant="ghost"
                  isDisabled={isBusy}
                  onClick={() => {
                    pendingFocus.current = 'reset';
                    setConfirmResetRevision(null);
                  }}
                />
              </>
            )}
          </HStack>
        </VStack>

        <VStack gap={3} as="section">
          <Heading level={3}>Review files</Heading>
          <Text as="p" type="supporting" color="secondary">
            Check each file, compare the pair, and check A’s hash if you provide a reference.
            Local findings go in the evidence ledger. A model critique is optional and separate: it
            receives check names, who ran them, slot letters (A or B), results, and counts. It
            never receives files, file names, paths, hashes, or keys.
          </Text>
          <FormLayout>
            <TextInput
              label="Reference SHA-256 for A"
              value={reference}
              onChange={setReference}
              isOptional
              autoComplete="off"
              hasSpellCheck={false}
              {...(isReferenceValid
                ? {}
                : {
                  status: {
                    type: 'error' as const,
                    message: 'Enter 64 hexadecimal characters or leave blank.',
                  },
                })}
            />
            <CheckboxInput
              label="Request a model critique"
              value={withCritic}
              onChange={setWithCritic}
              size="sm"
            />
          </FormLayout>
          <HStack gap={3} wrap="wrap">
            <Button
              label="Run file checks"
              variant="secondary"
              isDisabled={
                isBusy ||
                state?.slots.a === undefined ||
                !isReferenceValid ||
                isCriticPending
              }
              onClick={() =>
                onReview(normalizedReference.length === 0 ? null : normalizedReference, withCritic)
              }
            />
            {isCriticPending && critique && (
              <Button
                label="Cancel critique"
                variant="secondary"
                isDisabled={isBusy}
                onClick={() => onCancelCritic(critique.end)}
              />
            )}
          </HStack>
          {state && state.advisor.skipped.length > 0 && (
            <Text as="p" type="supporting" color="secondary">
              Last pass skipped:{' '}
              {state.advisor.skipped
                .map((tool) =>
                  tool === 'compare_builds'
                    ? 'comparison (no B)'
                    : tool === 'verify_checksum'
                      ? 'checksum (no reference)'
                      : 'file identity',
                )
                .join('; ')}
              .
            </Text>
          )}
          {critique && critique.status !== 'idle' && critique.status !== 'ready' && (
            <section aria-label="Model critique status">
              <Text as="p" color="secondary">
                {CRITIC_LABELS[critique.status]}
              </Text>
            </section>
          )}
          {state !== null && critique?.status === 'ready' && critique.text && critique.cites && (
            <section aria-label="Model critique">
              <VStack gap={2}>
                <Heading level={4}>Model critique</Heading>
                <Text as="p">{critique.text}</Text>
                <Text as="p" type="supporting" color="secondary">
                  Ledger citations:{' '}
                  {critique.cites.map((citation, index) => (
                    <Fragment key={citation}>
                      {index > 0 ? ', ' : ''}
                      <a href={`#inspect-finding-${state.revision}-${citation}`}>#{citation}</a>
                    </Fragment>
                  ))}
                  . Second opinion, not evidence.
                </Text>
              </VStack>
            </section>
          )}
        </VStack>

        <VStack gap={3} as="section">
          <Heading level={3}>Read-only agent key</Heading>
          <Text as="p" color="secondary" role="status">
            {state ? CONNECTION_LABELS[state.connection] : 'Key status unavailable'}
          </Text>
          {state && (
            <Text as="p" type="supporting" color="secondary" hasTabularNumbers>
              {hasActiveKey
                ? `${state.remaining.toLocaleString()} tool calls remaining.`
                : 'One key connects one metadata-only MCP agent.'}
            </Text>
          )}
          <HStack gap={3} wrap="wrap">
            {hasActiveKey ? (
              <Button
                label="Revoke case key"
                variant="secondary"
                isDisabled={isBusy}
                onClick={() => {
                  setRevealKey(false);
                  onDisconnect();
                }}
              />
            ) : (
              <Button
                label="Pair read-only agent"
                variant="secondary"
                isDisabled={isBusy || state?.slots.a === undefined}
                onClick={() => {
                  setRevealKey(false);
                  onPair();
                }}
              />
            )}
            {caseKey && (
              <Button
                label={revealKey ? 'Hide case key' : 'Reveal case key'}
                variant="ghost"
                isDisabled={isBusy}
                onClick={() => setRevealKey((current) => !current)}
              />
            )}
          </HStack>
          {hasActiveKey && caseKey === null && (
            <Text as="p" type="supporting" color="secondary">
              This page no longer holds the key. Revoke it and pair again to issue a new one.
            </Text>
          )}
          {caseKey && revealKey && (
            <TextInput
              label="Private case key"
              value={caseKey}
              isReadOnly
              autoComplete="off"
              hasSpellCheck={false}
            />
          )}
          {caseKey && (
            <Text as="p" type="supporting" color="secondary">
              The key exists only in this page memory. Do not place it in chat, a URL, or storage.
            </Text>
          )}
        </VStack>
      </VStack>
    </Collapsible>
  );
}
