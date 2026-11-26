import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});

Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

Object.defineProperties(HTMLDialogElement.prototype, {
  show: {
    configurable: true,
    value(this: HTMLDialogElement): void {
      this.setAttribute('open', '');
    },
  },
  showModal: {
    configurable: true,
    value(this: HTMLDialogElement): void {
      this.setAttribute('open', '');
    },
  },
  close: {
    configurable: true,
    value(this: HTMLDialogElement): void {
      this.removeAttribute('open');
      this.dispatchEvent(new Event('close'));
    },
  },
});