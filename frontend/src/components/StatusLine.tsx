export type StatusTone = 'neutral' | 'signal' | 'verified' | 'fault';

export interface StatusLineProps {
  label: string;
  value: string;
  tone?: StatusTone;
}

const TONE_MARKS: Record<StatusTone, string> = {
  neutral: '—',
  signal: '●',
  verified: '✓',
  fault: '!',
};

export default function StatusLine({
  label,
  value,
  tone = 'neutral',
}: StatusLineProps): React.JSX.Element {
  return (
    <p className="status-line" data-tone={tone} role="status" aria-live="polite">
      <span aria-hidden="true" className="status-line__mark">
        {TONE_MARKS[tone]}
      </span>
      <span className="status-line__label">{label}</span>
      <span className="status-line__value">{value}</span>
    </p>
  );
}
