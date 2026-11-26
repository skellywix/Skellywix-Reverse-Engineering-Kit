import { useState } from 'react';
import { AlertDialog } from '@astryxdesign/core/AlertDialog';
import { Button } from '@astryxdesign/core/Button';
import { Code } from '@astryxdesign/core/Code';
import { Collapsible } from '@astryxdesign/core/Collapsible';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Stack } from '@astryxdesign/core/Stack';
import { Text } from '@astryxdesign/core/Text';
import type { ReconstructReleases } from '../pages/reconstructApi';

export interface ReleaseControlsProps {
  releases: ReconstructReleases;
  canStage: boolean;
  isUnavailable: boolean;
  isPending: boolean;
  unavailableReason?: string;
  onStage: () => Promise<boolean>;
  onPromote: (digest: string) => Promise<boolean>;
  onRollback: (digest: string) => Promise<boolean>;
}

interface Confirmation {
  kind: 'promote' | 'rollback';
  target: string;
}

function digestValue(value: string | null): React.ReactNode {
  return value === null ? 'None recorded' : <Code>{value}</Code>;
}

export default function ReleaseControls({
  releases,
  canStage,
  isUnavailable,
  isPending,
  unavailableReason,
  onStage,
  onPromote,
  onRollback,
}: ReleaseControlsProps): React.JSX.Element {
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [isConfirming, setIsConfirming] = useState(false);

  const unavailableTooltip = isUnavailable
    ? (unavailableReason ?? 'Reconstruction state is refreshing.')
    : undefined;
  const stageTooltip = isUnavailable
    ? unavailableTooltip
    : canStage
      ? undefined
      : 'The current run and attempt do not have a verified package.';
  const promoteTarget =
    confirmation?.kind === 'promote' ? confirmation.target : 'unavailable';
  const rollbackTarget =
    confirmation?.kind === 'rollback' ? confirmation.target : 'unavailable';

  const confirm = async (): Promise<void> => {
    if (confirmation === null) {
      return;
    }
    setIsConfirming(true);
    try {
      if (confirmation.kind === 'promote') {
        await onPromote(confirmation.target);
      } else {
        await onRollback(confirmation.target);
      }
      setConfirmation(null);
    } finally {
      setIsConfirming(false);
    }
  };

  return (
    <section aria-label="Release administration">
      <Collapsible trigger="Release controls" defaultIsOpen={false}>
        <Stack gap={3} paddingBlockStart={2}>
          <Text as="p" color="secondary">
            Staging prepares an engine update. It does not publish a Rust download. Promotion selects the version for new runs.
          </Text>
          <MetadataList columns="multi" label={{ position: 'top' }}>
            <MetadataListItem label="Active">{digestValue(releases.active)}</MetadataListItem>
            <MetadataListItem label="Staged">{digestValue(releases.staged)}</MetadataListItem>
            <MetadataListItem label="Previous">{digestValue(releases.previous)}</MetadataListItem>
          </MetadataList>
          <Stack direction="horizontal" gap={2} wrap="wrap">
            <Button
              label="Stage engine update"
              variant="secondary"
              isDisabled={isUnavailable || !canStage || isPending}
              isLoading={isPending}
              {...(stageTooltip === undefined ? {} : { tooltip: stageTooltip })}
              clickAction={async () => {
                await onStage();
              }}
            />
            <Button
              label="Promote staged release"
              variant="secondary"
              isDisabled={isUnavailable || releases.staged === null || isPending}
              {...(unavailableTooltip === undefined ? {} : { tooltip: unavailableTooltip })}
              onClick={() => {
                if (releases.staged !== null) {
                  setConfirmation({ kind: 'promote', target: releases.staged });
                }
              }}
            />
            <Button
              label="Rollback to previous release"
              variant="destructive"
              isDisabled={isUnavailable || releases.previous === null || isPending}
              {...(unavailableTooltip === undefined ? {} : { tooltip: unavailableTooltip })}
              onClick={() => {
                if (releases.previous !== null) {
                  setConfirmation({ kind: 'rollback', target: releases.previous });
                }
              }}
            />
          </Stack>
        </Stack>
      </Collapsible>
      <AlertDialog
        isOpen={confirmation?.kind === 'promote'}
        onOpenChange={(isOpen) => {
          if (!isOpen && !isConfirming) {
            setConfirmation(null);
          }
        }}
        title="Promote staged release"
        description={`Make the exact staged digest active: ${promoteTarget}`}
        actionLabel="Promote this digest"
        cancelLabel="Keep current release"
        onAction={confirm}
        isActionLoading={isConfirming}
      />
      <AlertDialog
        isOpen={confirmation?.kind === 'rollback'}
        onOpenChange={(isOpen) => {
          if (!isOpen && !isConfirming) {
            setConfirmation(null);
          }
        }}
        title="Rollback release"
        description={`Previous version at last check: ${rollbackTarget}. Do not use this on a live engine: the previous version can change before the engine acts.`}
        actionLabel="Roll back"
        cancelLabel="Keep current release"
        onAction={confirm}
        isActionLoading={isConfirming}
      />
    </section>
  );
}
