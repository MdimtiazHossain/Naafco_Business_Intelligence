/**
 * Country Target: what the grid shows when a figure cannot be derived.
 *
 * This is the screen's sharpest edge, and the one worth pinning. Revision 0027
 * added Conversion Factor and Transfer Price nullable with no back-fill, so on
 * a fresh deployment *no* material has either — and the grid has to say so
 * rather than show a plausible number.
 *
 * Three rules:
 *
 * - a derived cell with a missing input reads `n/a`, never `0` and never a dash
 *   that could equally mean "no value";
 * - the **country total** is suppressed entirely unless every line derived,
 *   because a partial sum labelled "Calculated Value" is a wrong number rather
 *   than a small one;
 * - the volume total is always real, because it is what somebody typed.
 *
 * The backend decides all three and explains the suppression in `notes`; these
 * tests check the grid shows what it was told rather than re-deriving the rule.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CountryTargetGrid } from '../components/targetmgmt/CountryTargetGrid';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type { TargetCountryLine, TargetCountryTotals } from '../types/api';

function line(overrides: Partial<TargetCountryLine> = {}): TargetCountryLine {
  return {
    material_code: 'TM-FULL',
    material_description: 'Both inputs stated',
    material_brand: 'Example Brand',
    material_group_name: 'Tea',
    company_code: 'C001',
    conversion_factor: 0.5,
    transfer_price: 240,
    target_volume: 1000,
    quantity: 2000,
    value: 480000,
    missing: [],
    ...overrides,
  };
}

const UNDERIVABLE = line({
  material_code: 'TM-NOFACTOR',
  material_description: 'No conversion factor',
  conversion_factor: null,
  target_volume: 500,
  quantity: null,
  value: null,
  missing: ['conversion_factor'],
});

const COMPLETE_TOTALS: TargetCountryTotals = {
  line_count: 1,
  target_volume: 1000,
  quantity: 2000,
  value: 480000,
  derivable_count: 1,
  missing_conversion_factor: [],
  missing_transfer_price: [],
};

const SUPPRESSED_TOTALS: TargetCountryTotals = {
  line_count: 2,
  target_volume: 1500,
  quantity: null,
  value: null,
  derivable_count: 1,
  missing_conversion_factor: ['TM-NOFACTOR'],
  missing_transfer_price: [],
};

function show(props: Partial<Parameters<typeof CountryTargetGrid>[0]> = {}) {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <CountryTargetGrid
          lines={[line()]}
          totals={COMPLETE_TOTALS}
          notes={[]}
          editable
          canEdit
          available={[]}
          saving={false}
          onSave={vi.fn()}
          {...props}
        />
      </ThemeProvider>
    </I18nProvider>,
  );
}

/**
 * The total card whose label matches, so a total reads without its neighbours.
 *
 * "Calculated Quantity" is both a column heading and a card label, so the match
 * is narrowed by element: a heading is a `<button>` inside a `<th>`, a card
 * label is a `<p>`.
 */
function totalCard(label: string): HTMLElement {
  const heading = screen
    .getAllByText(label)
    .find((node) => node.tagName === 'P');
  if (!heading) throw new Error(`no total card labelled ${label}`);
  return heading.parentElement as HTMLElement;
}

describe('CountryTargetGrid', () => {
  it('shows the derived quantity and value when both inputs exist', () => {
    show();
    const row = screen.getByText('TM-FULL').closest('tr') as HTMLElement;
    expect(within(row).getByText('2,000')).toBeTruthy();
    expect(within(row).queryByText('n/a')).toBeNull();
  });

  it('reads n/a, not zero, where the conversion factor is missing', () => {
    show({ lines: [UNDERIVABLE], totals: SUPPRESSED_TOTALS });
    const row = screen.getByText('TM-NOFACTOR').closest('tr') as HTMLElement;
    // The factor cell itself, plus the quantity and value it would have fed.
    expect(within(row).getAllByText('n/a').length).toBe(3);
    expect(within(row).queryByText('0')).toBeNull();
  });

  it('explains an n/a cell rather than only marking it', () => {
    show({ lines: [UNDERIVABLE], totals: SUPPRESSED_TOTALS });
    const row = screen.getByText('TM-NOFACTOR').closest('tr') as HTMLElement;
    const [first] = within(row).getAllByText('n/a');
    expect(first.getAttribute('title')).toContain('Conversion Factor');
    expect(first.getAttribute('title')).toContain('Material Master');
  });

  it('totals volume even when nothing else can be totalled', () => {
    show({ lines: [line(), UNDERIVABLE], totals: SUPPRESSED_TOTALS });
    expect(
      within(totalCard('Country Target Volume')).getByText('1,500'),
    ).toBeTruthy();
  });

  it('suppresses the quantity and value totals when any line is underivable', () => {
    show({ lines: [line(), UNDERIVABLE], totals: SUPPRESSED_TOTALS });
    expect(within(totalCard('Calculated Quantity')).getByText('n/a')).toBeTruthy();
    expect(within(totalCard('Calculated Value')).getByText('n/a')).toBeTruthy();
  });

  it('says how much of the target could be derived', () => {
    show({ lines: [line(), UNDERIVABLE], totals: SUPPRESSED_TOTALS });
    expect(
      within(totalCard('Calculated Quantity')).getByText(/1 of 2 materials/),
    ).toBeTruthy();
  });

  it('shows the totals when every line derives', () => {
    show();
    expect(within(totalCard('Calculated Quantity')).getByText('2,000')).toBeTruthy();
    expect(
      within(totalCard('Calculated Value')).queryByText('n/a'),
    ).toBeNull();
  });

  it("shows the backend's explanation verbatim", () => {
    const note = '1 material(s) state no Conversion Factor: TM-NOFACTOR.';
    show({ lines: [UNDERIVABLE], totals: SUPPRESSED_TOTALS, notes: [note] });
    expect(screen.getByText(note)).toBeTruthy();
  });

  it('lets a volume be typed and saves only what changed', () => {
    const onSave = vi.fn();
    show({ lines: [line(), UNDERIVABLE], totals: SUPPRESSED_TOTALS, onSave });

    fireEvent.change(screen.getByLabelText('Target Volume TM-FULL'), {
      target: { value: '2500' },
    });
    fireEvent.click(screen.getByText('Save Country Target'));

    expect(onSave).toHaveBeenCalledWith([
      { material_code: 'TM-FULL', target_volume: '2500' },
    ]);
  });

  it('sends the raw text, so the backend decides what is unreadable', () => {
    const onSave = vi.fn();
    show({ onSave });
    fireEvent.change(screen.getByLabelText('Target Volume TM-FULL'), {
      target: { value: '12,5OO' },
    });
    fireEvent.click(screen.getByText('Save Country Target'));

    // Not 12, not 0, not NaN — the browser does not decide what this means.
    expect(onSave).toHaveBeenCalledWith([
      { material_code: 'TM-FULL', target_volume: '12,5OO' },
    ]);
  });

  it('draws no inputs and no save on a frozen version', () => {
    show({ editable: false });
    expect(screen.queryByLabelText('Target Volume TM-FULL')).toBeNull();
    expect(screen.queryByText('Save Country Target')).toBeNull();
    expect(
      screen.getByText(/approved or locked/),
    ).toBeTruthy();
  });

  it('draws no inputs for a reader who may not edit', () => {
    show({ canEdit: false });
    expect(screen.queryByLabelText('Target Volume TM-FULL')).toBeNull();
    expect(screen.getByText(/may view this target but not change it/)).toBeTruthy();
  });
});
