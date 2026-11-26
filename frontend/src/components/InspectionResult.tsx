import { Code } from '@astryxdesign/core/Code';
import { Icon } from '@astryxdesign/core/Icon';
import {
  MetadataList,
  MetadataListItem,
} from '@astryxdesign/core/MetadataList';
import { Heading, Text } from '@astryxdesign/core/Text';
import { VStack } from '@astryxdesign/core/Stack';
import {
  isBuildComparison,
  isChecksumResult,
  isSpecimenMetadata,
  type InspectResult,
  type InspectSlot,
  type InspectTool,
  type SpecimenMetadata,
} from '../pages/inspectApi';

export interface InspectionResultProps {
  tool: InspectTool;
  result: InspectResult;
  slots: Partial<Record<InspectSlot, SpecimenMetadata>>;
  /** Basenames kept in this tab for the loaded files. Display only. */
  names?: Partial<Record<InspectSlot, string>>;
  showHeading?: boolean;
}

const COMPARISON_SCOPE_NOTE =
  'Compares size, format, architecture, and SHA-256 only. It does not show how either file behaves, where it came from, or whether it is safe.';

function formatAbsoluteBytes(value: number): string {
  return `${value.toLocaleString()} ${value === 1 ? 'byte' : 'bytes'}`;
}

function formatSignedBytes(value: number): string {
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toLocaleString()} ${Math.abs(value) === 1 ? 'byte' : 'bytes'}`;
}

function Digest({ value }: { value: string }): React.JSX.Element {
  return <Code>{value}</Code>;
}

function describeSpecimen(
  specimen: SpecimenMetadata,
  name: string | undefined,
): string {
  const summary = `${specimen.format} · ${formatAbsoluteBytes(specimen.size)}`;
  return name === undefined ? summary : `${name} · ${summary}`;
}

export default function InspectionResult({
  tool,
  result,
  slots,
  names = {},
  showHeading = true,
}: InspectionResultProps): React.JSX.Element {
  if (tool === 'identify_file' && isSpecimenMetadata(result)) {
    return (
      <VStack gap={3}>
        {showHeading && <Heading level={3}>File identity</Heading>}
        <MetadataList columns="single">
          <MetadataListItem label="File format">{result.format}</MetadataListItem>
          <MetadataListItem label="Architecture">{result.architecture}</MetadataListItem>
          <MetadataListItem label="Size">{formatAbsoluteBytes(result.size)}</MetadataListItem>
          <MetadataListItem label="SHA-256">
            <Digest value={result.sha256} />
          </MetadataListItem>
        </MetadataList>
      </VStack>
    );
  }

  if (tool === 'compare_builds' && isBuildComparison(result)) {
    function difference(field: 'architecture' | 'format', changed: boolean): string {
      const a = slots.a?.[field];
      const b = slots.b?.[field];
      return !a || !b || a === 'unknown' || b === 'unknown'
        ? `Cannot compare: ${field} unknown`
        : changed ? 'Changed' : 'Unchanged';
    }
    const verdict = result.identical ? 'Files match by SHA-256' : 'Files differ';
    return (
      <VStack gap={3}>
        {showHeading ? (
          <Heading level={3}>{verdict}</Heading>
        ) : (
          <Text as="p" weight="semibold">
            {verdict}
          </Text>
        )}
        {showHeading && (
          <Text as="p" type="supporting" color="secondary">
            {COMPARISON_SCOPE_NOTE}
          </Text>
        )}
        <MetadataList columns="single">
          <MetadataListItem label="Size change (B − A)">
            {formatSignedBytes(result.size_delta)}
          </MetadataListItem>
          <MetadataListItem label="Format">
            {difference('format', result.format_changed)}
          </MetadataListItem>
          <MetadataListItem label="Architecture">
            {difference('architecture', result.architecture_changed)}
          </MetadataListItem>
          {slots.a && (
            <MetadataListItem label="Specimen A">
              {describeSpecimen(slots.a, names.a)}
            </MetadataListItem>
          )}
          {slots.b && (
            <MetadataListItem label="Specimen B">
              {describeSpecimen(slots.b, names.b)}
            </MetadataListItem>
          )}
        </MetadataList>
      </VStack>
    );
  }

  if (tool === 'verify_checksum' && isChecksumResult(result)) {
    const verdict = result.matches ? 'Checksum matches reference' : 'Checksum mismatch';
    return (
      <VStack gap={3}>
        {showHeading ? (
          <Heading level={3} color={result.matches ? 'accent' : 'primary'}>
            {result.matches && <Icon icon="check" color="success" size="sm" />}
            {result.matches ? ` ${verdict}` : verdict}
          </Heading>
        ) : (
          <Text
            as="p"
            weight="semibold"
            color={result.matches ? 'accent' : 'primary'}
          >
            {result.matches && <Icon icon="check" color="success" size="sm" />}
            {result.matches ? ` ${verdict}` : verdict}
          </Text>
        )}
        <MetadataList columns="single">
          <MetadataListItem label="Reference SHA-256">
            <Digest value={result.expected} />
          </MetadataListItem>
          <MetadataListItem label="Specimen SHA-256">
            <Digest value={result.actual} />
          </MetadataListItem>
        </MetadataList>
      </VStack>
    );
  }

  return (
    <Text as="p" color="secondary">
      The inspector returned an unsupported result for this check.
    </Text>
  );
}
