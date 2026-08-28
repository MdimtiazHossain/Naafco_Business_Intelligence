/**
 * The review tree: a hierarchy drawn as a table, and the figures it refuses.
 *
 * Two things are pinned. **Expansion is a filter over a flat list** — a child is
 * drawn only when every ancestor above it is open — which is what lets this stay
 * a `DataTable` and keep the column arrangement every other report table has.
 * And **nothing that cannot be computed is drawn as zero**: growth against a
 * year with no sales, achievement before the period has any actuals, and a
 * quantity whose material states no conversion factor all read `n/a`, each with
 * a tooltip saying why.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReviewTree } from '../components/targetmgmt/ReviewTree';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type { TargetReviewResponse, TargetReviewRow } from '../types/api';

function row(overrides: Partial<TargetReviewRow> = {}): TargetReviewRow {
  return {
    level: 'company',
    node_code: 'C001',
    name: 'Demo Industries Ltd.',
    parent_level: null,
    parent_code: null,
    depth: 0,
    has_children: true,
    target_volume: 120000,
    system_volume: 120000,
    adjusted: false,
    target_quantity: 240000,
    target_value: 57600000,
    missing_derivation: [],
    previous_year_volume: 100000,
    growth_percent: 20,
    actual_volume: null,
    achievement_percent: null,
    recon_variance: 0,
    pending_revisions: 0,
    status: 'ALLOCATED',
    ...overrides,
  };
}

/** Country → two regions → one area under the first. */
const TREE: TargetReviewRow[] = [
  row(),
  row({
    level: 'region', node_code: 'REG001', name: 'Dhaka', depth: 1,
    parent_level: 'company', parent_code: 'C001', target_volume: 70000,
    has_children: true,
  }),
  row({
    level: 'area', node_code: 'AR001', name: 'Mirpur', depth: 2,
    parent_level: 'region', parent_code: 'REG001', target_volume: 70000,
    has_children: false,
  }),
  row({
    level: 'region', node_code: 'REG002', name: 'Khulna', depth: 1,
    parent_level: 'company', parent_code: 'C001', target_volume: 50000,
    has_children: false,
  }),
];

function payload(
  overrides: Partial<TargetReviewResponse> = {},
): TargetReviewResponse {
  return {
    plan: {} as never,
    version: {} as never,
    rows: TREE,
    reconciliation: {
      balanced: true, mismatched_nodes: 0, node_count: 4,
      target_volume: 120000,
    },
    scope: 'Full country scope',
    notes: ['Recon variance is the child total minus the node’s own target.'],
    materials: ['SKU001', 'SKU002'],
    material_code: null,
    levels: ['company', 'region', 'area'],
    ...overrides,
  };
}

function show(data = payload(), onMaterialChange = vi.fn()) {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <ReviewTree data={data} onMaterialChange={onMaterialChange} />
      </ThemeProvider>
    </I18nProvider>,
  );
}

function rowFor(code: string): HTMLElement {
  return screen.getByText(new RegExp(code)).closest('tr') as HTMLElement;
}

describe('ReviewTree', () => {
  it('opens the top two levels and leaves the rest closed', () => {
    show();
    expect(screen.getByText(/C001/)).toBeTruthy();
    expect(screen.getByText(/REG001/)).toBeTruthy();
    // Depth 2 sits under an open region, so it is drawn; the seeding opens
    // depth 0 and 1, which is enough to show a region's children.
    expect(screen.getByText(/AR001/)).toBeTruthy();
  });

  it('hides a branch when its parent is collapsed', () => {
    show();
    const region = rowFor('REG001');
    fireEvent.click(within(region).getByRole('button', { name: 'Collapse' }));
    expect(screen.queryByText(/AR001/)).toBeNull();
    // The sibling region is untouched.
    expect(screen.getByText(/REG002/)).toBeTruthy();
  });

  it('collapsing the root hides everything below it', () => {
    show();
    fireEvent.click(within(rowFor('C001')).getByRole('button', { name: 'Collapse' }));
    expect(screen.queryByText(/REG001/)).toBeNull();
    expect(screen.queryByText(/REG002/)).toBeNull();
    expect(screen.getByText(/C001/)).toBeTruthy();
  });

  it('expand all and collapse all reach every node', () => {
    show();
    fireEvent.click(screen.getByText('Collapse all'));
    expect(screen.queryByText(/REG001/)).toBeNull();
    fireEvent.click(screen.getByText('Expand all'));
    expect(screen.getByText(/AR001/)).toBeTruthy();
  });

  it('gives a leaf no expander', () => {
    show();
    const leaf = rowFor('REG002');
    expect(within(leaf).queryByRole('button', { name: 'Collapse' })).toBeNull();
    expect(within(leaf).queryByRole('button', { name: 'Expand' })).toBeNull();
  });

  it('indents by depth so the hierarchy reads as one', () => {
    show();
    const cell = within(rowFor('AR001')).getByText(/AR001/).parentElement!;
    expect((cell as HTMLElement).style.paddingLeft).toBe('32px');
  });

  // -------------------------------------------------------------------------
  // Figures that cannot be computed
  // -------------------------------------------------------------------------

  it('reads n/a for achievement before the period has actuals', () => {
    show();
    const rowEl = rowFor('C001');
    const na = within(rowEl)
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(na).toContain('never shown as 0%');
  });

  it('reads n/a for growth against a year with no sales', () => {
    show(payload({
      rows: [row({ previous_year_volume: null, growth_percent: null })],
    }));
    const titles = screen
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(titles).toContain('undefined');
  });

  it('suppresses quantity and value when a material lacks a factor', () => {
    show(payload({
      rows: [row({
        target_quantity: null, target_value: null,
        missing_derivation: ['SKU001'],
      })],
    }));
    const titles = screen
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .filter(Boolean)
      .join(' ');
    expect(titles).toContain('SKU001');
    // The volume is unaffected — the engine allocates volume, not quantity.
    // It appears in the row and again in the reconciliation strip, which is
    // the same number twice rather than two claims about it.
    expect(screen.getAllByText('1,20,000').length).toBeGreaterThan(0);
  });

  // -------------------------------------------------------------------------
  // Reconciliation
  // -------------------------------------------------------------------------

  it('shows a balanced strip when every level agrees', () => {
    show();
    expect(screen.getByText('Balanced · Reconciled')).toBeTruthy();
    expect(screen.getByText('4 nodes checked')).toBeTruthy();
  });

  it('shows a mismatch and says approval stays blocked', () => {
    show(payload({
      reconciliation: {
        balanced: false, mismatched_nodes: 2, node_count: 4,
        target_volume: 120000,
      },
      rows: [row({ recon_variance: 100 })],
    }));
    expect(screen.getByText('Reconciliation mismatch')).toBeTruthy();
    expect(screen.getByText(/Final approval stays blocked/)).toBeTruthy();
  });

  it('marks a node a management adjustment moved', () => {
    show(payload({
      rows: [row({ adjusted: true, system_volume: 118800 })],
    }));
    const marker = screen.getByText('*');
    expect(marker.getAttribute('title')).toContain('1,18,800');
  });

  // -------------------------------------------------------------------------
  // Scope and filtering
  // -------------------------------------------------------------------------

  it("shows the reader's scope in words", () => {
    show(payload({ scope: 'Scoped to REG001 by your role' }));
    expect(screen.getByText('Scoped to REG001 by your role')).toBeTruthy();
  });

  it('asks for a material filter without deciding it', () => {
    const onMaterialChange = vi.fn();
    show(payload(), onMaterialChange);
    fireEvent.change(screen.getByLabelText('Material'), {
      target: { value: 'SKU002' },
    });
    expect(onMaterialChange).toHaveBeenCalledWith('SKU002');
  });

  it('clears the filter back to every material', () => {
    const onMaterialChange = vi.fn();
    show(payload({ material_code: 'SKU002' }), onMaterialChange);
    fireEvent.change(screen.getByLabelText('Material'), {
      target: { value: '' },
    });
    expect(onMaterialChange).toHaveBeenCalledWith(null);
  });

  it('explains an empty tree rather than showing a blank table', () => {
    const note = 'This version has not been allocated yet.';
    show(payload({
      rows: [], notes: [note],
      reconciliation: {
        balanced: true, mismatched_nodes: 0, node_count: 0, target_volume: 0,
      },
    }));
    expect(screen.getByText(note)).toBeTruthy();
  });
});
