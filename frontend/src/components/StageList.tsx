import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { ProgressBar } from '@astryxdesign/core/ProgressBar';
import { Stack } from '@astryxdesign/core/Stack';
import { Heading, Text } from '@astryxdesign/core/Text';
import type { ReconstructStage } from '../pages/reconstructApi';
import { stageLabels, stepResult } from '../pages/reconstructLabels';
import StatusLine from './StatusLine';

export interface StageListProps {
  stages: readonly ReconstructStage[];
  liveStage: string | null;
}

const EVIDENCE_LABELS: Readonly<Record<string, string>> = {
  worker: 'Worker',
  skill: 'Skill',
  image: 'Image',
  tool: 'Tool',
  generation: 'Build number',
  requested_model: 'Requested model',
  requested_effort: 'Requested effort',
  actual_model: 'Actual model',
  actual_effort: 'Actual effort',
  deterministic: 'Runs without a model',
  reason: 'Reason',
  outcome: 'Recorded outcome',
  matched: 'Tests that matched',
};


function visibleEvidence(stage: ReconstructStage): readonly [string, string][] {
  const evidence: [string, string][] = [];
  for (const [key, label] of Object.entries(EVIDENCE_LABELS)) {
    const value = stage.detail[key];
    if (typeof value === 'string' || typeof value === 'number') {
      evidence.push([label, String(value)]);
    } else if (typeof value === 'boolean') {
      evidence.push([label, value ? 'Yes' : 'No']);
    }
  }
  const receipt = stage.detail.evidence;
  if (typeof receipt === 'object' && receipt !== null && !Array.isArray(receipt)) {
    const matched = (receipt as Record<string, unknown>).matched;
    const required = (receipt as Record<string, unknown>).required;
    if (typeof matched === 'number' && typeof required === 'number') {
      evidence.push(['Receipt', `${matched.toLocaleString()} of ${required.toLocaleString()} matched`]);
    }
  }
  return evidence;
}


export default function StageList({ stages, liveStage }: StageListProps): React.JSX.Element {
  return (
    <section id="reconstruct-stages" aria-labelledby="reconstruct-stages-heading">
      <Stack gap={3}>
        <Heading id="reconstruct-stages-heading" level={2}>Rebuild steps</Heading>
        <Stack as="ol" gap={4} aria-label="Reconstruction stages">
          {stages.map((stage, index) => {
            const current = liveStage === stage.name && stage.status === 'running';
            const [result, tone] = stepResult(stage, current);
            const evidence = visibleEvidence(stage);
            const countLabel = stage.name === 'analyze' ? 'Original tests run' :
              stage.name === 'verify' ? 'Rust tests run' : null;
            return (
              <Stack as="li" key={stage.name} gap={2} aria-current={current ? 'step' : undefined}>
                <StatusLine label={`${index + 1}. ${stageLabels[stage.name]}`} value={result} tone={tone} />
                {typeof stage.detail.worker === 'string' ? (
                  <Text as="p" type="supporting">
                    {stage.detail.worker}{typeof stage.detail.generation === 'number' ? ` · Build ${stage.detail.generation}` : ''}
                  </Text>
                ) : null}
                {countLabel !== null && stage.completed !== null && stage.required !== null && stage.required > 0 ? (
                  <ProgressBar
                    label={countLabel}
                    value={stage.completed}
                    max={stage.required}
                    hasValueLabel
                    formatValueLabel={(value, max) => `${value.toLocaleString()} of ${max.toLocaleString()} tests run`}
                    variant={tone === 'fault' ? 'error' : 'accent'}
                  />
                ) : null}
                {evidence.length > 0 ? (
                  <details>
                    <summary>Step details</summary>
                    <MetadataList columns="multi" label={{ position: 'top' }}>
                      {evidence.map(([label, value]) => (
                        <MetadataListItem key={label} label={label}>{value}</MetadataListItem>
                      ))}
                    </MetadataList>
                  </details>
                ) : null}
              </Stack>
            );
          })}
        </Stack>
      </Stack>
    </section>
  );
}
