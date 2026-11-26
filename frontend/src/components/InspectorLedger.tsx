import { List, ListItem } from '@astryxdesign/core/List';
import { Section } from '@astryxdesign/core/Section';
import { Heading, Text } from '@astryxdesign/core/Text';
import { VStack } from '@astryxdesign/core/Stack';
import {
  isChecksumResult,
  type InspectActor,
  type InspectSlot,
  type InspectState,
} from '../pages/inspectApi';
import InspectionResult from './InspectionResult';

export interface InspectorLedgerProps {
  state: InspectState;
  /** Basenames kept in this tab for the loaded files. Display only. */
  names?: Partial<Record<InspectSlot, string>>;
}

const TOOL_LABELS = {
  identify_file: 'File ID',
  compare_builds: 'Compare files',
  verify_checksum: 'Hash check',
  export_report: 'Evidence export',
} as const;

const ACTOR_LABELS = {
  operator: 'You',
  agent: 'Paired agent',
  skellywix: 'Skellywix',
} as const;

const ACTOR_ORDER: readonly InspectActor[] = ['operator', 'agent', 'skellywix'];

export default function InspectorLedger({
  state,
  names = {},
}: InspectorLedgerProps): React.JSX.Element {
  const retained = [...state.events].reverse();
  const byActor = ACTOR_ORDER.filter((actor) => state.advisor.actors[actor] > 0).map(
    (actor) => `${ACTOR_LABELS[actor]} ${state.advisor.actors[actor].toLocaleString()}`,
  );
  const latestChecksum = retained.find(
    (event) => event.tool === 'verify_checksum' && isChecksumResult(event.result),
  );
  const laterMatch =
    latestChecksum !== undefined &&
    isChecksumResult(latestChecksum.result) &&
    latestChecksum.result.matches;
  return (
    <Section variant="transparent" padding={0}>
      <VStack gap={3} as="section">
        <Heading level={2}>Evidence ledger</Heading>
        <Text as="p" type="supporting" color="secondary" hasTabularNumbers>
          {state.advisor.total === 1
            ? '1 entry recorded.'
            : `${state.advisor.total.toLocaleString()} entries recorded.`}
        </Text>
        {byActor.length > 0 && (
          <Text as="p" type="supporting" color="secondary" hasTabularNumbers>
            {`By actor: ${byActor.join(' · ')}.`}
          </Text>
        )}
        {state.advisor.mismatches > 0 && (
          <Text as="p" color="secondary" hasTabularNumbers>
            {state.advisor.mismatches === 1
              ? '1 checksum mismatch recorded.'
              : `${state.advisor.mismatches.toLocaleString()} checksum mismatches recorded.`}
            {laterMatch
              ? ' A later match does not erase an earlier mismatch in this case.'
              : ''}
          </Text>
        )}
        {state.earlier_events_omitted > 0 && (
          <Text as="p" color="secondary" hasTabularNumbers>
            {state.earlier_events_omitted === 1
              ? '1 earlier entry is counted but no longer retained.'
              : `${state.earlier_events_omitted.toLocaleString()} earlier entries are counted but no longer retained.`}
          </Text>
        )}
        {retained.length === 0 ? (
          <Text as="p" color="secondary">
            No evidence has been recorded.
          </Text>
        ) : (
          <List density="compact" edge="flush" hasDividers>
            {retained.map((event) => (
              <ListItem
                key={event.n}
                itemKey={event.n}
                align="start"
                label={
                  <details id={`inspect-finding-${state.revision}-${event.n}`}>
                    <summary>
                      {TOOL_LABELS[event.tool]}
                      {event.slot ? ` · ${event.slot.toUpperCase()}` : ''}
                      {` · ${ACTOR_LABELS[event.actor]} · #${event.n}`}
                    </summary>
                    {event.tool === 'export_report' ? (
                      <Text as="p" type="supporting" color="secondary">
                        Evidence export recorded.
                      </Text>
                    ) : (
                      <InspectionResult
                        tool={event.tool}
                        result={event.result}
                        slots={state.slots}
                        names={names}
                        showHeading={false}
                      />
                    )}
                  </details>
                }
              />
            ))}
          </List>
        )}
      </VStack>
    </Section>
  );
}
