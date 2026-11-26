import type { StatusTone } from '../components/StatusLine';
import type { ReconstructStage, ReconstructStageName } from './reconstructApi';

export const stageLabels: Readonly<Record<ReconstructStageName, string>> = {
  intake: 'Check the original file',
  analyze: 'Test the original',
  rebuild: 'Build the Rust version',
  verify: 'Compare the results',
  package: 'Prepare the download',
};

export function stageLabel(name: string): string {
  return Object.hasOwn(stageLabels, name) ? stageLabels[name as ReconstructStageName] : name;
}

export function stepResult(stage: ReconstructStage, current: boolean): [string, StatusTone] {
  const { status, detail } = stage;
  if (status === 'completed' && detail.outcome === 'rejected') {
    return [stage.name === 'verify' ? 'Results differed' : 'Build failed', 'fault'];
  }
  if (status === 'completed' && stage.name === 'rebuild' && detail.worker === 'Rust builder') {
    return ['Rust source written', 'neutral'];
  }
  switch (status) {
    case 'pending': return ['Not started', 'neutral'];
    case 'queued': return ['Waiting to start', 'neutral'];
    case 'running': return current ? ['Running now', 'signal'] : ['Last reported as running', 'neutral'];
    case 'completed': return ['Finished', 'verified'];
    case 'verified': return ['Verified', 'verified'];
    case 'failed': return ['Failed', 'fault'];
    case 'blocked': return ['Blocked', 'fault'];
    case 'cancelled': return ['Cancelled', 'neutral'];
    default: return [status.charAt(0).toUpperCase() + status.slice(1).replaceAll('_', ' '), 'neutral'];
  }
}
