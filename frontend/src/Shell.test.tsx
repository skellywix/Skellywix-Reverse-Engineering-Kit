import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Shell from './Shell';

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <output aria-label="Current route">{location.pathname}</output>;
}

function renderShell(initialEntry = '/reconstruct/runs/run-7'): void {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route element={<Shell />}>
          <Route path="*" element={<LocationProbe />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('workspace shell', () => {
  beforeEach(() => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  });

  it('renders the labeled rail and keeps the selected route while the mobile drawer toggles', async () => {
    const user = userEvent.setup();
    renderShell();

    const rail = screen.getByRole('navigation', { name: 'Workspace' });
    for (const label of ['Reconstruct', 'Inspect', 'Convert', 'Runtime', 'Releases']) {
      expect(rail).toHaveTextContent(label);
    }

    const trigger = screen.getByRole('button', { name: 'Open navigation' });
    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Workspace navigation' })).toBeVisible();
    expect(screen.getByRole('status', { name: 'Current route' })).toHaveTextContent('/reconstruct/runs/run-7');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Workspace navigation' })).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Current route' })).toHaveTextContent('/reconstruct/runs/run-7');
  });

  it('restores focus to the mobile drawer trigger after close', async () => {
    const user = userEvent.setup();
    renderShell('/inspect');

    const trigger = screen.getByRole('button', { name: 'Open navigation' });
    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Workspace navigation' })).toBeVisible();

    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
  });

  it('honors reduced-motion preference without removing the drawer', () => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    renderShell();

    expect(screen.getByTestId('workspace-shell')).toHaveAttribute('data-reduced-motion', 'true');
    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument();
  });

  it('keeps Skelly as a quiet static help entry', () => {
    renderShell();

    const image = screen.getByRole('img', { name: 'Skelly help' });
    expect(image).toHaveAttribute('draggable', 'false');
    expect(screen.queryByText(/feed|speak|drag/i)).not.toBeInTheDocument();
  });
});
