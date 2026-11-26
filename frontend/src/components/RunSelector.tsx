import { Selector } from '@astryxdesign/core/Selector';
import type { ReconstructRunSummary } from '../pages/reconstructApi';
import { stageLabel } from '../pages/reconstructLabels';

export interface RunSelectorProps {
  runs: readonly ReconstructRunSummary[];
  selectedRunId: string | null;
  isDisabled?: boolean;
  disabledMessage?: string;
  onSelect: (runId: string) => void;
}

function runLabel(run: ReconstructRunSummary): string {
  const status = run.status === 'completed' ? 'step finished' : run.status === 'running' ? 'reported running' : run.status;
  return `${run.id.slice(0, 8)}… · attempt ${run.attempt} · ${status}`;
}

export default function RunSelector({
  runs,
  selectedRunId,
  isDisabled = false,
  disabledMessage,
  onSelect,
}: RunSelectorProps): React.JSX.Element {
  return (
    <Selector
      label="Run"
      description="Choose a saved run to see its latest results."
      options={runs.map((run) => ({
        value: run.id,
        label: runLabel(run),
        ...(run.stage === run.status ? {} : { description: stageLabel(run.stage) }),
      }))}
      hasClear={false}
      {...(selectedRunId === null ? {} : { value: selectedRunId })}
      placeholder={runs.length === 0 ? 'No runs recorded' : 'Choose a run'}
      onChange={(value) => {
        onSelect(value);
      }}
      isDisabled={isDisabled || runs.length === 0}
      presentation="adaptive"
      width="100%"
      {...(disabledMessage === undefined ? {} : { disabledMessage })}
    />
  );
}
