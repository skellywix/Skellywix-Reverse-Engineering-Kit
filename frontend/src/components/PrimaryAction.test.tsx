import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PrimaryAction, PrimaryActionScope } from './PrimaryAction';

describe('PrimaryActionScope', () => {
  it('renders exactly one primary-styled action', () => {
    render(
      <PrimaryActionScope>
        <PrimaryAction label="Start reconstruction" onClick={vi.fn()} />
      </PrimaryActionScope>,
    );

    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Start reconstruction' })).toHaveAttribute(
      'data-primary-action',
      'true',
    );
  });

  it('rejects a scope with more than one primary action', () => {
    expect(() =>
      render(
        <PrimaryActionScope>
          <PrimaryAction label="First" onClick={vi.fn()} />
          <PrimaryAction label="Second" onClick={vi.fn()} />
        </PrimaryActionScope>,
      ),
    ).toThrow(/exactly one primary action/i);
  });
});
