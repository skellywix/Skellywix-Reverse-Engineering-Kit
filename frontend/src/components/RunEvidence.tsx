import { Code } from '@astryxdesign/core/Code';
import { Grid } from '@astryxdesign/core/Grid';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { Stack } from '@astryxdesign/core/Stack';
import { Heading, Text } from '@astryxdesign/core/Text';
import type { ReconstructSelectedRun } from '../pages/reconstructApi';

export interface RunEvidenceProps {
  run: ReconstructSelectedRun;
}

export default function RunEvidence({ run }: RunEvidenceProps): React.JSX.Element {
  const { matched, required } = run.proof;
  const verify = run.stages.find((stage) => stage.name === 'verify');
  const last = verify?.detail;
  let comparison = 'No comparison result yet.';
  if (matched !== null) {
    comparison = `${matched.toLocaleString()} of ${required.toLocaleString()} tests matched`;
  } else if (last?.outcome === 'rejected') {
    const prefix = typeof last.generation === 'number' ? `Build ${last.generation}: ` : '';
    comparison = typeof last.matched === 'number' && verify?.required != null
      ? `${prefix}${last.matched.toLocaleString()} of ${verify.required.toLocaleString()} tests matched.`
      : prefix ? `${prefix}the comparison found differences.` : 'The comparison found differences.';
  }

  return (
    <section aria-labelledby="run-evidence-heading">
      <Stack gap={3}>
        <Heading id="run-evidence-heading" level={2}>
          Results for this run
        </Heading>
        <Grid columns={{ minWidth: 260, max: 2 }} gap={4}>
          <section aria-labelledby="proof-evidence-heading">
            <Stack gap={2}>
              <Heading id="proof-evidence-heading" level={3}>
                Test results
              </Heading>
              <MetadataList label={{ position: 'top' }}>
                <MetadataListItem label="Attempt">Attempt {run.attempt}</MetadataListItem>
                <MetadataListItem label="Original and Rust results">{comparison}</MetadataListItem>
                <MetadataListItem label="Verdict">
                  {run.proof.verified && matched === run.proof.required && matched === 2557 ? 'Verified' : 'Not verified'}
                </MetadataListItem>
                <MetadataListItem label="Download fingerprint (SHA-256)">
                  {run.package.sha256 === null ? (
                    'No download fingerprint yet'
                  ) : (
                    <Code>{run.package.sha256}</Code>
                  )}
                </MetadataListItem>
              </MetadataList>
            </Stack>
          </section>
          <section aria-labelledby="model-evidence-heading">
            <Stack gap={2}>
              <Heading id="model-evidence-heading" level={3}>
                Rust builder
              </Heading>
              <MetadataList label={{ position: 'top' }}>
                <MetadataListItem label="Requested model">
                  <Code>{run.model.requested}</Code>
                </MetadataListItem>
                <MetadataListItem label="Actual model">
                  {run.model.actual === null ? (
                    <Text as="span" color="secondary">
                      Unknown — no model receipt recorded
                    </Text>
                  ) : (
                    <Code>{run.model.actual}</Code>
                  )}
                </MetadataListItem>
              </MetadataList>
            </Stack>
          </section>
        </Grid>
      </Stack>
    </section>
  );
}
