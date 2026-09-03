/**
 * The readiness gate and the adjustment panel: telling the truth about data.
 *
 * The gate's whole reason for existing is that a limitation should never be a
 * surprise, so these tests are about what the screen *says* rather than what it
 * computes — it computes nothing. Three things matter:
 *
 * - each of the five data states is drawn as itself, so "no sales loaded" and
 *   "sales loaded but missing a column" do not read the same;
 * - a check can be absent-but-not-blocking, and is drawn as advice rather than
 *   as a barrier;
 * - the level a run would reach is stated **before** the run, and a
 *   sub-territory allocation is never dressed up as a customer-level one.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  AdjustmentPanel,
  AllocationRuns,
} from '../components/targetmgmt/AllocationRuns';
import {
  ReadinessGate,
  ReadinessSummary,
} from '../components/targetmgmt/ReadinessGate';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetAdjustment,
  TargetAllocationRun,
  TargetReadiness,
  TargetReadinessCheck,
} from '../types/api';

function check(overrides: Partial<TargetReadinessCheck> = {}): TargetReadinessCheck {
  return {
    key: 'sales_history',
    label: 'Historical Sales available',
    state: 'AVAILABLE',
    tone: 'ok',
    ok: true,
    blocking: false,
    advisory: false,
    detail: '1,200 sales rows across FY 2024-25 and FY 2025-26.',
    action: null,
    facts: {},
    ...overrides,
  };
}

function readiness(overrides: Partial<TargetReadiness> = {}): TargetReadiness {
  return {
    plan: {} as never,
    version: {} as never,
    checks: [check()],
    ready: true,
    blocking: [],
    allocation_level: 'customer',
    allocation_level_is_customer: true,
    customer_mapping: {
      total_customers: 2091,
      mapped_to_sub_territory: 2091,
      unmapped: 0,
      mapped_to_unknown_sub_territory: 0,
      usable: 2091,
    },
    hierarchy: { counts: {}, broken_parent_links: {} },
    projection: {
      projected_rows: 1840,
      maximum_rows: 1500000,
      available_rows: 1498160,
      capacity_used_percent: 0.1,
      estimated_memory_mb: 0.7,
      exceeds: false,
      financial_year: 'FY 2026-27',
      target_period: 'FY',
      months: 12,
      materials: 6,
      nodes: 25,
      customers: 2091,
      allocation_level: 'customer',
      narrowing_options: ['target_period', 'company'],
    },
    basis_years: ['FY 2024-25', 'FY 2025-26'],
    factors: [],
    ...overrides,
  };
}

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider>
      <ThemeProvider>{node}</ThemeProvider>
    </I18nProvider>,
  );
}

describe('ReadinessGate', () => {
  it('reports ready when nothing blocks', () => {
    wrap(<ReadinessGate readiness={readiness()} />);
    expect(screen.getByText('Ready')).toBeTruthy();
  });

  it('draws each data state as itself', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          ready: false,
          blocking: ['sales_history'],
          checks: [
            check({ key: 'a', label: 'Country Target exists' }),
            check({
              key: 'b', label: 'Historical Sales available', state: 'NO_DATA',
              tone: 'blocked', ok: false, blocking: true,
              detail: 'No sales rows exist for the basis years.',
            }),
            check({
              key: 'c', label: 'Customer to Sub-Territory mapping',
              state: 'INSUFFICIENT_DATA', tone: 'blocked', ok: false,
              blocking: true, detail: '0 of 2,091 customers are mapped.',
            }),
            check({
              key: 'd', label: 'Organisational hierarchy consistent',
              state: 'INVALID_DATA', tone: 'error', ok: false, blocking: true,
              detail: 'Some records name a parent the master does not have.',
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText('Available')).toBeTruthy();
    expect(screen.getByText('No Data')).toBeTruthy();
    expect(screen.getByText('Insufficient Data')).toBeTruthy();
    expect(screen.getByText('Invalid Data')).toBeTruthy();
    // Four different labels, not four ticks and crosses.
    expect(screen.getByText('Not Ready')).toBeTruthy();
  });

  it('marks an advisory check as not blocking the run', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          checks: [
            check({
              key: 'conversion_factor', label: 'Conversion Factor available',
              state: 'NO_DATA', tone: 'blocked', ok: false,
              blocking: false, advisory: true,
              detail: '3 materials state no Conversion Factor. Volume '
                + 'allocation is unaffected.',
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText('does not block the run')).toBeTruthy();
    expect(screen.getByText(/Volume allocation is unaffected/)).toBeTruthy();
  });

  it('shows what to do about a failing check', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          checks: [check({
            state: 'NO_DATA', tone: 'blocked', ok: false, blocking: true,
            action: 'Load the sales history through the Data Upload centre.',
          })],
        })}
      />,
    );
    expect(
      screen.getByText('Load the sales history through the Data Upload centre.'),
    ).toBeTruthy();
  });

  it('states the level a run would reach', () => {
    wrap(<ReadinessGate readiness={readiness()} />);
    expect(screen.getByText('Allocation would reach: Customer')).toBeTruthy();
  });

  it('says customer-level allocation is impossible when it is', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          allocation_level: 'sub_territory',
          allocation_level_is_customer: false,
          customer_mapping: {
            total_customers: 2091, mapped_to_sub_territory: 0, unmapped: 2091,
            mapped_to_unknown_sub_territory: 0, usable: 0,
          },
        })}
      />,
    );
    expect(screen.getByText('Allocation would reach: Sub-Territory')).toBeTruthy();
    expect(
      screen.getByText(/Cannot perform customer-level allocation/),
    ).toBeTruthy();
  });

  it('shows the customer mapping health as counts', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          customer_mapping: {
            total_customers: 2091, mapped_to_sub_territory: 0, unmapped: 2091,
            mapped_to_unknown_sub_territory: 0, usable: 0,
          },
        })}
      />,
    );
    const total = screen.getByText('Total Customers').parentElement!;
    expect(within(total).getByText('2,091')).toBeTruthy();
    const unmapped = screen.getByText('Unmapped').parentElement!;
    expect(within(unmapped).getByText('2,091')).toBeTruthy();
  });

  it('names what to narrow when the projection exceeds the ceiling', () => {
    wrap(
      <ReadinessGate
        readiness={readiness({
          ready: false,
          projection: {
            ...readiness().projection,
            projected_rows: 400000,
            // Stated rather than inherited: this scenario is about a plan over
            // its ceiling, so it carries its own ceiling and the capacity that
            // follows from it, and stays true when the default moves.
            maximum_rows: 250000,
            available_rows: -150000,
            capacity_used_percent: 160,
            exceeds: true,
            narrowing_options: ['target_period', 'business_unit'],
          },
        })}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Allocation exceeds the configured maximum row limit.',
    );
    expect(screen.getByText(/Target Period, Business Unit/)).toBeTruthy();
    // Indian digit grouping, which is what `formatQuantity` uses everywhere
    // in this app: 4,00,000 rather than 400,000.
    expect(screen.getByText('4,00,000')).toBeTruthy();
    expect(screen.getByText('2,50,000')).toBeTruthy();
  });

  it('summarises a blocked gate above the panel', () => {
    wrap(
      <ReadinessSummary
        readiness={readiness({ ready: false, blocking: ['a', 'b'] })}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'This allocation cannot run yet',
    );
    expect(screen.getByText(/2 condition\(s\)/)).toBeTruthy();
  });

  it('says nothing when the gate is clear', () => {
    const { container } = wrap(<ReadinessSummary readiness={readiness()} />);
    expect(container.textContent).toBe('');
  });
});

// ---------------------------------------------------------------------------

function run(overrides: Partial<TargetAllocationRun> = {}): TargetAllocationRun {
  return {
    job_id: 'abcdef12-3456-7890-abcd-ef1234567890',
    plan_id: 1,
    version_id: 1,
    status: 'COMPLETED',
    started_by: 'ceo',
    started_at: '2026-08-27T09:00:00',
    completed_at: '2026-08-27T09:02:00',
    created_at: '2026-08-27T09:00:00',
    projected_rows: 1840,
    generated_rows: 1840,
    allocation_level: 'customer',
    sales_rows_found: 1200,
    sales_data_available: true,
    error_count: 0,
    warning_count: 0,
    error_message: null,
    ...overrides,
  };
}

describe('AllocationRuns', () => {
  it('lists a failed run beside a successful one', () => {
    wrap(
      <AllocationRuns
        runs={[
          run(),
          run({ job_id: 'ffff0000-1111', status: 'FAILED', generated_rows: 0,
                error_count: 1, sales_data_available: null,
                sales_rows_found: null }),
        ]}
      />,
    );
    expect(screen.getByText('Allocation complete')).toBeTruthy();
    expect(screen.getByText('Allocation failed')).toBeTruthy();
  });

  it('shows sales availability as three-valued', () => {
    wrap(
      <AllocationRuns
        runs={[run({ sales_data_available: null, sales_rows_found: null })]}
      />,
    );
    // Never looked is n/a, not 0 — 0 would claim it looked and found nothing.
    const cell = screen.getByText('n/a');
    expect(cell.getAttribute('title')).toContain('stopped before it read');
  });

  it('names the level a run reached', () => {
    wrap(<AllocationRuns runs={[run({ allocation_level: 'sub_territory' })]} />);
    expect(screen.getByText('Sub-Territory')).toBeTruthy();
  });

  it('distinguishes completed-with-warnings from completed', () => {
    wrap(<AllocationRuns runs={[run({ status: 'COMPLETED_WITH_WARNINGS',
                                      warning_count: 2 })]} />);
    expect(screen.getByText('Completed with warnings')).toBeTruthy();
    expect(screen.getByText('2W')).toBeTruthy();
  });

  it('says so when no run has happened', () => {
    wrap(<AllocationRuns runs={[]} />);
    expect(
      screen.getByText(/No allocation has been run for this version yet/),
    ).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------

function adjustment(
  overrides: Partial<TargetAdjustment> = {},
): TargetAdjustment {
  return {
    adjustment_id: 1,
    level: 'region',
    node_code: 'REG001',
    material_code: null,
    adjustment_volume: '500',
    reason: 'Herbicide demand review.',
    adjusted_by: 'ceo',
    adjusted_at: '2026-08-27T09:00:00',
    ...overrides,
  };
}

function showAdjustments(props: Record<string, unknown> = {}) {
  return wrap(
    <AdjustmentPanel
      stored={[adjustment()]}
      applied={[]}
      canEdit
      editable
      saving={false}
      onAdd={vi.fn()}
      onRemove={vi.fn()}
      {...props}
    />,
  );
}

describe('AdjustmentPanel', () => {
  it('shows all six columns the specification asks for', () => {
    showAdjustments({
      applied: [{
        level: 'region', node_code: 'REG001', material_code: null,
        system_volume: '10000', adjustment_volume: '500',
        final_volume: '10500', adjustment_percent: 5,
        reason: 'Herbicide demand review.', adjusted_by: 'ceo',
        adjusted_at: '2026-08-27T09:00:00',
      }],
    });
    expect(screen.getByText('System Suggested')).toBeTruthy();
    expect(screen.getByText('Adjustment')).toBeTruthy();
    expect(screen.getByText('Final Target')).toBeTruthy();
    expect(screen.getByText('Adjustment %')).toBeTruthy();
    expect(screen.getByText('Reason')).toBeTruthy();
    expect(screen.getByText('Adjusted By')).toBeTruthy();
    expect(screen.getByText('Adjusted Date')).toBeTruthy();

    expect(screen.getByText('10,000')).toBeTruthy();
    expect(screen.getByText('+500')).toBeTruthy();
    expect(screen.getByText('10,500')).toBeTruthy();
    expect(screen.getByText('5.0%')).toBeTruthy();
  });

  it('says an unapplied adjustment has not moved anything yet', () => {
    showAdjustments();
    // Two cells read n/a on an unapplied row — System Suggested and
    // Adjustment % — so the one with the explanation is the one with a title.
    const explained = screen
      .getAllByText('n/a')
      .filter((node) => node.getAttribute('title'));
    expect(explained).toHaveLength(1);
    expect(explained[0].getAttribute('title')).toContain('next allocation run');
  });

  it('sends an absolute signed volume, never a percentage', () => {
    const onAdd = vi.fn();
    showAdjustments({ stored: [], onAdd });

    fireEvent.change(screen.getByLabelText('Node code'), {
      target: { value: 'REG002' },
    });
    fireEvent.change(screen.getByLabelText('Volume (+/-)'), {
      target: { value: '-1200' },
    });
    fireEvent.change(screen.getByLabelText('Reason'), {
      target: { value: 'Two dealers closed.' },
    });
    fireEvent.click(screen.getByText('Add'));

    expect(onAdd).toHaveBeenCalledWith({
      level: 'region',
      node_code: 'REG002',
      adjustment_volume: -1200,
      reason: 'Two dealers closed.',
    });
  });

  it('will not add an adjustment without a reason', () => {
    const onAdd = vi.fn();
    showAdjustments({ stored: [], onAdd });
    fireEvent.change(screen.getByLabelText('Node code'), {
      target: { value: 'REG002' },
    });
    fireEvent.change(screen.getByLabelText('Volume (+/-)'), {
      target: { value: '500' },
    });
    const button = screen.getByText('Add').closest('button') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(onAdd).not.toHaveBeenCalled();
  });

  it('draws no form on a frozen version', () => {
    showAdjustments({ editable: false });
    expect(screen.queryByLabelText('Node code')).toBeNull();
  });

  it('draws no form for a reader who may not edit', () => {
    showAdjustments({ canEdit: false });
    expect(screen.queryByLabelText('Node code')).toBeNull();
  });
});
