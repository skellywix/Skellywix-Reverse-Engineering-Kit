import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import { Button } from '@astryxdesign/core/Button';
import { FileInput } from '@astryxdesign/core/FileInput';
import { Grid } from '@astryxdesign/core/Grid';
import { Section } from '@astryxdesign/core/Section';
import { Heading, Text } from '@astryxdesign/core/Text';
import { HStack, VStack } from '@astryxdesign/core/Stack';
import {
  MAX_INSPECT_FILE_SIZE,
  type InspectSlot,
  type ReplacementLoss,
  type SpecimenMetadata,
} from '../pages/inspectApi';

export interface SpecimenPickerHandle {
  choose(slot: InspectSlot): void;
  /** Return keyboard focus to a slot's file control after a dismissed prompt. */
  focus(slot: InspectSlot): void;
}

/** A file the operator picked in this tab that the inspector has not accepted. */
export interface SpecimenSelection {
  slot: InspectSlot;
  name: string;
  phase: 'loading' | 'confirm' | 'failed';
}

export interface SpecimenPickerProps {
  slots: Partial<Record<InspectSlot, SpecimenMetadata>>;
  /** Local basenames of files this tab loaded. Never sent to the inspector or a model. */
  loadedNames: Partial<Record<InspectSlot, string>>;
  selection: SpecimenSelection | null;
  isDisabled: boolean;
  onChoose: (slot: InspectSlot, file: File) => Promise<void>;
}

function formatBytes(value: number): string {
  return `${value.toLocaleString()} ${value === 1 ? 'byte' : 'bytes'}`;
}

function selectionText(selection: SpecimenSelection): string {
  switch (selection.phase) {
    case 'loading':
      return `Selected: ${selection.name} · loading`;
    case 'confirm':
      return `Selected: ${selection.name} · not loaded yet`;
    case 'failed':
      return `Selected: ${selection.name} · not loaded`;
  }
}

function SlotPicker({
  slot,
  specimen,
  loadedName,
  selection,
  isDisabled,
  disabledMessage,
  onChoose,
  containerRef,
}: {
  slot: InspectSlot;
  specimen: SpecimenMetadata | undefined;
  loadedName: string | undefined;
  selection: SpecimenSelection | null;
  isDisabled: boolean;
  disabledMessage: string | undefined;
  onChoose: (slot: InspectSlot, file: File) => Promise<void>;
  containerRef: React.RefObject<HTMLElement | null>;
}): React.JSX.Element {
  const label = slot.toUpperCase();
  return (
    <section ref={containerRef} aria-labelledby={`inspect-specimen-${slot}-heading`}>
      <Section variant="muted" padding={4}>
        <VStack gap={3}>
          <Heading id={`inspect-specimen-${slot}-heading`} level={3}>
            Specimen {label}
          </Heading>
          <Text as="p" type="supporting" color="secondary">
            {specimen
              ? `${specimen.format} · ${formatBytes(specimen.size)}`
              : `Specimen ${label} is empty.`}
          </Text>
          {specimen && (
            <Text as="p" type="supporting" style={{ overflowWrap: 'anywhere' }}>
              {loadedName === undefined
                ? 'Loaded: file name not recorded in this tab'
                : `Loaded: ${loadedName}`}
            </Text>
          )}
          {selection !== null && (
            <Text
              as="p"
              type="supporting"
              color="secondary"
              style={{ overflowWrap: 'anywhere' }}
            >
              {selectionText(selection)}
            </Text>
          )}
          <FileInput
            label={`Specimen ${label} file`}
            value={null}
            onChange={() => undefined}
            changeAction={async (files) => {
              if (files instanceof File) await onChoose(slot, files);
            }}
            maxSize={MAX_INSPECT_FILE_SIZE}
            description="Binary input is summarized in memory and is not retained. The file name stays in this tab. Maximum 16 MiB."
            placeholder={specimen ? `Replace specimen ${label}` : `Choose specimen ${label}`}
            mode="input"
            isDisabled={isDisabled}
            {...(disabledMessage === undefined ? {} : { disabledMessage })}
          />
        </VStack>
      </Section>
    </section>
  );
}

const SpecimenPicker = forwardRef<SpecimenPickerHandle, SpecimenPickerProps>(
  function SpecimenPicker(
    { slots, loadedNames, selection, isDisabled, onChoose },
    ref,
  ): React.JSX.Element {
    const aRef = useRef<HTMLElement>(null);
    const bRef = useRef<HTMLElement>(null);

    useImperativeHandle(ref, () => ({
      choose(slot): void {
        const container = slot === 'a' ? aRef.current : bRef.current;
        container?.querySelector<HTMLInputElement>('input[type="file"]')?.click();
      },
      focus(slot): void {
        const container = slot === 'a' ? aRef.current : bRef.current;
        container?.querySelector<HTMLElement>('button, input')?.focus();
      },
    }));

    return (
      <Grid columns={{ minWidth: 280, max: 2 }} gap={4}>
        <SlotPicker
          slot="a"
          specimen={slots.a}
          loadedName={loadedNames.a}
          selection={selection?.slot === 'a' ? selection : null}
          isDisabled={isDisabled}
          disabledMessage={undefined}
          onChoose={onChoose}
          containerRef={aRef}
        />
        <SlotPicker
          slot="b"
          specimen={slots.b}
          loadedName={loadedNames.b}
          selection={selection?.slot === 'b' ? selection : null}
          isDisabled={isDisabled || slots.a === undefined}
          disabledMessage={
            slots.a === undefined ? 'Choose specimen A before specimen B.' : undefined
          }
          onChoose={onChoose}
          containerRef={bRef}
        />
      </Grid>
    );
  },
);

function lossItems(loss: ReplacementLoss): string[] {
  const items: string[] = [];
  if (loss.entries > 0) {
    items.push(
      loss.entries === 1
        ? '1 ledger entry'
        : `${loss.entries.toLocaleString()} ledger entries`,
    );
  }
  if (loss.mismatches > 0) {
    items.push(
      loss.mismatches === 1
        ? '1 recorded checksum mismatch'
        : `${loss.mismatches.toLocaleString()} recorded checksum mismatches`,
    );
  }
  if (loss.clearsSpecimenB) items.push('specimen B');
  if (loss.discardsCritique) items.push('the model critique state');
  if (loss.revokesKey) items.push('the read-only agent key');
  if (items.length === 0) items.push('nothing; the ledger is now empty');
  return items;
}

export interface ReplacementGuardProps {
  slot: InspectSlot;
  name: string;
  loss: ReplacementLoss;
  /** The case changed after the operator chose the file. The loss shown is current. */
  caseChanged: boolean;
  isBusy: boolean;
  /** Export is available only while specimen A is loaded. */
  canExport: boolean;
  onExportThenReplace: () => void;
  onReplace: () => void;
  onKeep: () => void;
}

/** Shown only when loading a file would discard a populated ledger. */
export function ReplacementGuard({
  slot,
  name,
  loss,
  caseChanged,
  isBusy,
  canExport,
  onExportThenReplace,
  onReplace,
  onKeep,
}: ReplacementGuardProps): React.JSX.Element {
  const keepRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    keepRef.current?.focus();
  }, []);
  const label = slot.toUpperCase();
  return (
    <section
      aria-labelledby="inspect-replace-heading"
      aria-describedby="inspect-replace-detail"
    >
      <Section variant="muted" padding={4}>
        <VStack gap={3}>
          <Heading id="inspect-replace-heading" level={3}>
            Replace specimen {label}?
          </Heading>
          <Text
            id="inspect-replace-detail"
            as="p"
            style={{ overflowWrap: 'anywhere' }}
          >
            {`Loading ${name} as specimen ${label} will clear: ${lossItems(loss).join(', ')}.`}
          </Text>
          {loss.notExportable > 0 && (
            <Text as="p" type="supporting" color="secondary">
              {loss.notExportable === 1
                ? '1 earlier entry is counted but not retained, so an export cannot include it.'
                : `${loss.notExportable.toLocaleString()} earlier entries are counted but not retained, so an export cannot include them.`}
            </Text>
          )}
          {caseChanged && (
            <Text as="p" type="supporting" color="secondary" role="status">
              The case changed after you chose this file. Review what it clears before loading.
            </Text>
          )}
          <HStack gap={3} wrap="wrap">
            <Button
              ref={keepRef}
              label="Keep case"
              variant="secondary"
              isDisabled={isBusy}
              onClick={onKeep}
            />
            <Button
              label="Export evidence, then replace"
              variant="secondary"
              isDisabled={isBusy || !canExport}
              onClick={onExportThenReplace}
            />
            <Button
              label={`Replace ${label} and clear`}
              variant="destructive"
              isDisabled={isBusy}
              onClick={onReplace}
            />
          </HStack>
        </VStack>
      </Section>
    </section>
  );
}

export default SpecimenPicker;
