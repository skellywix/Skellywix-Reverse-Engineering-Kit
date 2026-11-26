import { Heading, Text } from '@astryxdesign/core/Text';

export interface PageHeaderProps {
  title: string;
  description: string;
}

export default function PageHeader({ title, description }: PageHeaderProps): React.JSX.Element {
  return (
    <header className="page-header">
      <Heading level={1}>{title}</Heading>
      <Text as="p" color="secondary">
        {description}
      </Text>
    </header>
  );
}
