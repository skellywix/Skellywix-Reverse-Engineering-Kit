import { Text } from '@astryxdesign/core/Text';
import brandMark from '../skellywix-mark.svg?no-inline';

export default function SkellyHelp(): React.JSX.Element {
  return (
    <details className="skelly-help">
      <summary aria-label="Skelly help">
        <img
          className="skelly-help__mark"
          src={brandMark}
          alt="Skelly help"
          draggable={false}
          width="32"
          height="32"
        />
        <span>Skelly help</span>
      </summary>
      <Text as="p" type="supporting" color="secondary">
        Use Inspect to compare files without running them. Convert accepts Rust proposals
        for registered source projects. Check Runtime for recorded build and comparison results.
      </Text>
    </details>
  );
}
