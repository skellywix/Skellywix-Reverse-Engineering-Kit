import { List, ListItem } from '@astryxdesign/core/List';
import { Heading, Text } from '@astryxdesign/core/Text';

export interface ArtifactRecord {
  id: string;
  name: string;
  digest: string;
  href: string;
}

export interface ArtifactListProps {
  artifacts: readonly ArtifactRecord[];
}

export default function ArtifactList({ artifacts }: ArtifactListProps): React.JSX.Element {
  return (
    <section aria-labelledby="artifact-list-heading" className="record-list">
      <Heading id="artifact-list-heading" level={2}>
        Artifacts
      </Heading>
      {artifacts.length === 0 ? (
        <Text as="p" color="secondary">
          No artifacts have been published.
        </Text>
      ) : (
        <List density="compact" edge="flush" hasDividers>
          {artifacts.map((artifact) => (
            <ListItem
              key={artifact.id}
              itemKey={artifact.id}
              label={artifact.name}
              description={
                <Text type="code" color="secondary">
                  {artifact.digest}
                </Text>
              }
              href={artifact.href}
            />
          ))}
        </List>
      )}
    </section>
  );
}
