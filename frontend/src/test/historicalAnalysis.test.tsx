/**
 * Historical Analysis: how the screen draws sales history that isn't there.
 *
 * The basis table is read by a planner deciding whether a target is achievable,
 * so the thing that matters most is that it never overstates its own evidence.
 * A material with no sales in a year must not read `0`, growth against such a
 * year must not read `-100%`, and a year whose figure is understated by blank
 * volumes must say so rather than presenting a clean-looking total.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HistoricalAnalysis } from '../components/targetmgmt/HistoricalAnalysis';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type { TargetHistoryResponse, TargetHistoryRow } from '../types/api';

const YEARS = ['FY 2024-25', 'FY 2025-26'];

function row(overrides: Partial<TargetHistoryRow> = {}): TargetHistoryRow {
  return {
    material_code: 'TH-GROW',
    material_description: 'Growing steadily',
    material_brand: 'Example Brand',
    material_group_name: 'Tea',
    years: YEARS.map((financial_year, index) => ({
      financial_year,
      volume: index === 0 ? 1000 : 1500,
      rows: 4,
      rows_without_volume: 0,
      complete: true,
    })),
    volumes: [1000, 1500],
    growth_percent: 50,
    average_volume: 1250,
    contribution_percent: 100,
    current_target_volume: 2000,
    target_growth_percent: 33.3,
    basis: 'GROWTH_WEIGHTED',
    complete: true,
    ...overrides,
  };
}

const NO_HISTORY = row({
  material_code: 'TH-NEW',
  material_description: 'Never traded',
  years: YEARS.map((financial_year) => ({
    financial_year,
    volume: null,
    rows: 0,
    rows_without_volume: 0,
    complete: true,
  })),
  volumes: [null, null],
  growth_percent: null,
  average_volume: null,
  contribution_percent: null,
  current_target_volume: 5000,
  target_growth_percent: null,
  basis: 'NO_HISTORY',
});

const UNDERSTATED = row({
  material_code: 'TH-BLANK',
  material_description: 'Some rows state no volume',
  years: YEARS.map((financial_year, index) => ({
    financial_year,
    volume: index === 0 ? 1000 : 900,
    rows: 6,
    rows_without_volume: index === 1 ? 2 : 0,
    complete: index !== 1,
  })),
  volumes: [1000, 900],
  complete: false,
});

function payload(
  rows: TargetHistoryRow[],
  overrides: Partial<TargetHistoryResponse> = {},
): TargetHistoryResponse {
  return {
    plan: {} as never,
    version: {} as never,
    basis_years: YEARS,
    rows,
    totals: {
      material_count: rows.length,
      years: YEARS.map((financial_year, index) => ({
        financial_year,
        volume: rows.reduce((sum, r) => sum + (r.volumes[index] ?? 0), 0) || null,
        materials_with_volume: rows.filter((r) => r.volumes[index] !== null).length,
      })),
      target_volume: rows.reduce((s, r) => s + (r.current_target_volume ?? 0), 0),
      with_history: rows.filter((r) => r.basis !== 'NO_HISTORY').length,
      incomplete_materials: rows.filter((r) => !r.complete).map((r) => r.material_code),
      without_history: rows
        .filter((r) => r.basis === 'NO_HISTORY')
        .map((r) => r.material_code),
    },
    notes: [],
    growth_guidance_percent: 15,
    ...overrides,
  };
}

function show(data: TargetHistoryResponse) {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <HistoricalAnalysis data={data} />
      </ThemeProvider>
    </I18nProvider>,
  );
}

function rowFor(code: string): HTMLElement {
  return screen.getByText(code).closest('tr') as HTMLElement;
}

describe('HistoricalAnalysis', () => {
  it('draws one volume column per basis year, named for the year', () => {
    show(payload([row()]));
    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent);
    expect(headers.some((h) => h?.includes('FY 2024-25'))).toBe(true);
    expect(headers.some((h) => h?.includes('FY 2025-26'))).toBe(true);
  });

  it('shows the two years, the growth and the basis', () => {
    show(payload([row()]));
    const line = rowFor('TH-GROW');
    expect(within(line).getByText('1,000')).toBeTruthy();
    expect(within(line).getByText('1,500')).toBeTruthy();
    expect(within(line).getByText('+50%')).toBeTruthy();
    expect(within(line).getByText('Growth-weighted')).toBeTruthy();
  });

  it('reads n/a, not zero, for a year with no sales', () => {
    show(payload([NO_HISTORY]));
    const line = rowFor('TH-NEW');
    // Both years, plus growth, average and contribution — five undefined cells.
    expect(within(line).getAllByText('n/a').length).toBeGreaterThanOrEqual(5);
    expect(within(line).queryByText('0')).toBeNull();
  });

  it('explains why a growth cell is undefined', () => {
    show(payload([NO_HISTORY]));
    const line = rowFor('TH-NEW');
    const titles = within(line)
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .filter(Boolean)
      .join(' ');
    expect(titles).toContain('undefined');
  });

  it('marks a year understated by blank volumes rather than hiding it', () => {
    show(payload([UNDERSTATED]));
    const line = rowFor('TH-BLANK');
    // The figure is still shown — it is the best that exists — with a marker.
    expect(within(line).getByText('900')).toBeTruthy();
    const marker = within(line).getByText('*');
    expect(marker.getAttribute('title')).toContain('state no volume');
  });

  it('flags a material with no history as the case to act on', () => {
    show(payload([row(), NO_HISTORY]));
    expect(within(rowFor('TH-NEW')).getByText('No history')).toBeTruthy();
  });

  it('still lists a material that has a target but no sales', () => {
    show(payload([NO_HISTORY]));
    const line = rowFor('TH-NEW');
    expect(within(line).getByText('5,000')).toBeTruthy();
  });

  it('says how many materials the allocation can actually distribute', () => {
    show(payload([row(), NO_HISTORY]));
    const card = screen
      .getAllByText('With History')
      .find((node) => node.tagName === 'P')!.parentElement as HTMLElement;
    expect(within(card).getByText('1 / 2')).toBeTruthy();
  });

  it('shows an empty table and the reason when nothing has been loaded', () => {
    const note =
      'No sales history and no country target yet for this plan’s scope.';
    show(payload([], { notes: [note] }));
    expect(screen.getByText(note)).toBeTruthy();
  });

  it("shows the backend's note about understated years", () => {
    const note = '1 material(s) have sales rows that state no volume: TH-BLANK.';
    show(payload([UNDERSTATED], { notes: [note] }));
    expect(screen.getByText(note)).toBeTruthy();
  });
});
