/**
 * The Country Target header renders its actions even with nothing in the grid.
 *
 * The upload buttons moved into this header, and the plan they are most needed
 * on is a brand new one — which has no country lines at all. If the empty list
 * short-circuited the section, the buttons would vanish exactly when somebody
 * needs them to load the first figures.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CountryTargetGrid } from '../components/targetmgmt/CountryTargetGrid';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';

function show(lines: never[] = []) {
  render(
    <I18nProvider>
      <ThemeProvider>
        <CountryTargetGrid
          lines={lines}
          totals={{
            line_count: lines.length, target_volume: 0, quantity: null,
            value: null, derivable_count: 0, missing_conversion_factor: [],
            missing_transfer_price: [],
          } as never}
          notes={[]}
          editable
          canEdit
          available={[]}
          saving={false}
          onSave={vi.fn()}
          actions={<button type="button">Choose a file</button>}
        />
      </ThemeProvider>
    </I18nProvider>,
  );
}

describe('CountryTargetGrid header actions', () => {
  it('renders header actions when the grid has no lines at all', () => {
    show();
    expect(screen.getByText('Choose a file')).toBeTruthy();
  });
});
