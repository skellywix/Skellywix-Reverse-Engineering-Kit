import { List, ListItem } from '@astryxdesign/core/List';
import { Heading, Text } from '@astryxdesign/core/Text';

export interface EvidenceRecord {
  id: string;
  label: string;
  detail: string;
  recordedAt: string;
}

export interface EvidenceListProps {
  evidence: readonly EvidenceRecord[];
}

export default function EvidenceList({ evidence }: EvidenceListProps): React.JSX.Element {
  return (
    <section aria-labelledby="evidence-list-heading" className="record-list">
      <Heading id="evidence-list-heading" level={2}>
        Evidence
      </Heading>
      {evidence.length === 0 ? (
        <Text as="p" color="secondary">
          No evidence has been recorded.
        </Text>
      ) : (
        <List density="compact" edge="flush" hasDividers>
          {evidence.map((record) => (
            <ListItem
              key={record.id}
              itemKey={record.id}
              label={record.label}
              description={
                <Text type="supporting" color="secondary">
                  {record.detail}
                </Text>
              }
              endContent={<time dateTime={record.recordedAt}>{record.recordedAt}</time>}
            />
          ))}
        </List>
      )}
    </section>
  );
}
