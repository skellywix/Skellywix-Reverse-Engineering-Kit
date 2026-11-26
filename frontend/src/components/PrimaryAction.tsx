import { Children, isValidElement } from 'react';
import type { ComponentProps, ReactNode } from 'react';
import { Button } from '@astryxdesign/core/Button';

type AstryxButtonProps = ComponentProps<typeof Button>;
export type PrimaryActionProps = Omit<AstryxButtonProps, 'variant'>;

export function PrimaryAction(props: PrimaryActionProps): React.JSX.Element {
  return <Button {...props} variant="primary" data-primary-action="true" />;
}

export interface PrimaryActionScopeProps {
  children: ReactNode;
  label?: string;
}

export function PrimaryActionScope({
  children,
  label = 'Primary action',
}: PrimaryActionScopeProps): React.JSX.Element {
  const count = Children.toArray(children).filter(
    (child) => isValidElement(child) && child.type === PrimaryAction,
  ).length;
  if (count !== 1) {
    throw new Error(`PrimaryActionScope requires exactly one primary action; received ${count}`);
  }
  return (
    <section className="primary-action-scope" aria-label={label}>
      {children}
    </section>
  );
}

export default PrimaryAction;
