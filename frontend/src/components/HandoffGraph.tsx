import { Grid } from '@astryxdesign/core/Grid';
import { List, ListItem } from '@astryxdesign/core/List';
import { Stack } from '@astryxdesign/core/Stack';
import { Table } from '@astryxdesign/core/Table';
import { Heading } from '@astryxdesign/core/Text';
import type { ReconstructStage, ReconstructStageName } from '../pages/reconstructApi';
import { stageLabels } from '../pages/reconstructLabels';

export interface HandoffGraphProps {
  stages: readonly ReconstructStage[];
  attempt: number;
}

type HandoffState = 'recorded' | 'blocked' | 'pending' | 'earlier build';

interface HandoffRow extends Record<string, unknown> {
  id: string;
  from: string;
  to: string;
  state: HandoffState;
  evidence: string;
}

const EDGES: readonly [ReconstructStageName, ReconstructStageName][] = [
  ['intake', 'analyze'],
  ['analyze', 'rebuild'],
  ['rebuild', 'verify'],
  ['verify', 'package'],
];
const STOPPED = new Set(['blocked', 'failed', 'cancelled']);

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function buildHandoffs(stages: readonly ReconstructStage[], attempt: number): HandoffRow[] {
  const byName = new Map(stages.map((stage) => [stage.name, stage]));
  return EDGES.map(([fromName, toName]) => {
    const from = byName.get(fromName);
    const to = byName.get(toName);
    const fromLabel = stageLabels[fromName];
    const toLabel = stageLabels[toName];
    if (fromName === 'rebuild' && typeof from?.detail.generation === 'number' &&
        typeof to?.detail.generation === 'number' && to.detail.generation < from.detail.generation) {
      return {
        id: `${fromName}-${toName}`, from: fromLabel, to: toLabel, state: 'earlier build',
        evidence: `The comparison is from build ${to.detail.generation}, not build ${from.detail.generation}.`,
      };
    }
    if (to !== undefined && to.status !== 'pending') {
      return {
        id: `${fromName}-${toName}`,
        from: fromLabel,
        to: toLabel,
        state: STOPPED.has(to.status) ? 'blocked' : 'recorded',
        evidence: `Attempt ${attempt}: ${to.detail.outcome === 'rejected' ? 'the result was rejected' : `last reported as ${to.status}`}.`,
      };
    }
    if (from !== undefined && STOPPED.has(from.status)) {
      return {
        id: `${fromName}-${toName}`,
        from: fromLabel,
        to: toLabel,
        state: 'blocked',
        evidence: `The previous step stopped (${from.status}). The next step has no report.`,
      };
    }
    return {
      id: `${fromName}-${toName}`,
      from: fromLabel,
      to: toLabel,
      state: 'pending',
      evidence: `The next step has no report for attempt ${attempt}.`,
    };
  });
}

export default function HandoffGraph({ stages, attempt }: HandoffGraphProps): React.JSX.Element {
  const handoffs = buildHandoffs(stages, attempt);
  const columns = [
    { key: 'from', header: 'From' },
    { key: 'to', header: 'To' },
    {
      key: 'state',
      header: 'State',
      renderCell: (handoff: HandoffRow) => titleCase(handoff.state),
    },
    { key: 'evidence', header: 'Evidence' },
  ];

  return (
    <section aria-labelledby="handoffs-heading">
      <Stack gap={3}>
        <Heading id="handoffs-heading" level={2}>
          Recorded handoffs
        </Heading>
        <Grid columns={{ minWidth: 280, max: 2 }} gap={4}>
          <section aria-labelledby="handoff-graph-heading">
            <Stack gap={2}>
              <Heading id="handoff-graph-heading" level={3}>
                Graph
              </Heading>
              <List density="compact" edge="flush" hasDividers>
                {handoffs.map((handoff) => (
                  <ListItem
                    key={handoff.id}
                    itemKey={handoff.id}
                    label={`${handoff.from} → ${handoff.to}`}
                    description={`${titleCase(handoff.state)} · ${handoff.evidence}`}
                  />
                ))}
              </List>
            </Stack>
          </section>
          <section aria-labelledby="handoff-table-heading">
            <Stack gap={2}>
              <Heading id="handoff-table-heading" level={3}>
                Handoff details
              </Heading>
              <Table<HandoffRow>
                aria-label="Recorded handoffs"
                data={handoffs}
                columns={columns}
                idKey="id"
                density="compact"
                dividers="rows"
              />
            </Stack>
          </section>
        </Grid>
      </Stack>
    </section>
  );
}
