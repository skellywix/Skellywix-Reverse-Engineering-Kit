import { useId } from 'react';
import { Tab, TabList } from '@astryxdesign/core/TabList';

export type RecordTab = 'overview' | 'evidence' | 'artifacts';

export interface RecordTabsProps {
  value: RecordTab;
  onChange: (value: RecordTab) => void;
  idPrefix?: string;
}

const RECORD_TABS: ReadonlyArray<{ value: RecordTab; label: string }> = [
  { value: 'overview', label: 'Overview' },
  { value: 'evidence', label: 'Evidence' },
  { value: 'artifacts', label: 'Artifacts' },
];

function isRecordTab(value: string): value is RecordTab {
  return RECORD_TABS.some((tab) => tab.value === value);
}

export default function RecordTabs({
  value,
  onChange,
  idPrefix,
}: RecordTabsProps): React.JSX.Element {
  const generatedId = useId();
  const prefix = idPrefix ?? `record-${generatedId}`;
  return (
    <TabList
      value={value}
      onChange={(nextValue) => {
        if (isRecordTab(nextValue)) {
          onChange(nextValue);
        }
      }}
      role="tablist"
      hasDivider
      overflow="scroll"
    >
      {RECORD_TABS.map((tab) => (
        <Tab
          key={tab.value}
          value={tab.value}
          label={tab.label}
          panelId={`${prefix}-${tab.value}-panel`}
        />
      ))}
    </TabList>
  );
}
