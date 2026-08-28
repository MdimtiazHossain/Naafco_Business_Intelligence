/**
 * Allocation: what the screen says when the engine correctly does nothing.
 *
 * The specification is emphatic about one case, and so is this file: with an
 * empty `fact_sales` the panel must **not** say "Allocation Completed" and must
 * not present zero-based customer targets. It has to read as an engine waiting
 * for transactional data — the years it checked, the rows it found, and the
 * factors it could not calculate.
 *
 * The rest is the progress interface the specification asks for: the stage
 * list, the percentage, the counts, and a prominent balanced indicator once
 * allocated volume equals country target volume.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AllocationPanel } from '../components/targetmgmt/AllocationPanel';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetAllocationJob,
  TargetAllocationState,
  TargetReconciliation,
} from '../types/api';

const STAGES = [
  { key: 'HISTORICAL_ANALYSIS', label: 'Historical Analysis' },
  { key: 'MATERIAL_ALLOCATION', label: 'Brand / SKU Allocation' },
  { key: 'MONTHLY_ALLOCATION', label: 'Monthly Allocation' },
  { key: 'CUSTOMER_ALLOCATION', label: 'Customer Allocation' },
  { key: 'RECONCILIATION', label: 'Reconciliation' },
  { key: 'FINALIZATION', label: 'Finalization' },
];

function reconciliation(
  overrides: Partial<TargetReconciliation> = {},
): TargetReconciliation {
  return {
    balanced: true,
    country_target_volume: '120000.0000',
    allocated_volume: '120000.0000',
    difference: '0.0000',
    allocation_percent: 100,
    mismatches: [],
    mismatch_count: 0,
    levels_checked: ['company', 'zone', 'region'],
    node_count: 25,
    ...overrides,
  };
}

function job(overrides: Partial<TargetAllocationJob> = {}): TargetAllocationJob {
  return {
    job_id: 'job-1',
    plan_id: 1,
    version_id: 1,
    status: 'COMPLETED',
    current_stage: 'FINALIZATION',
    current_stage_label: 'Finalization',
    stages: STAGES,
    progress_percent: 100,
    rows_processed: 1840,
    total_rows: 1840,
    error_count: 0,
    error_message: null,
    settings: { weights: {}, enabled: {}, management_adjustment: {} },
    result: {
      reconciliation: reconciliation(),
      months: ['2026-07', '2026-08'],
      materials: ['SKU001'],
      material_count: 1,
      node_count: 25,
      customer_count: 5,
      row_count: 1840,
      factors: [],
      warnings: [],
    },
    requested_by: 'ceo',
    started_at: '2026-08-27T09:00:00',
    completed_at: '2026-08-27T09:02:00',
    created_at: '2026-08-27T09:00:00',
    ...overrides,
  };
}

function state(
  overrides: Partial<TargetAllocationState> = {},
): TargetAllocationState {
  return {
    plan: {} as never,
    version: {} as never,
    reconciliation: reconciliation(),
    job: null,
    has_allocation: true,
    editable: true,
    ...overrides,
  };
}

function show(props: Partial<Parameters<typeof AllocationPanel>[0]> = {}) {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <AllocationPanel
          state={state()}
          job={null}
          canEdit
          starting={false}
          onStart={vi.fn()}
          {...props}
        />
      </ThemeProvider>
    </I18nProvider>,
  );
}

describe('AllocationPanel', () => {
  it('offers to generate an allocation on an editable version', () => {
    show();
    expect(screen.getByText('Generate Allocation')).toBeTruthy();
    expect(screen.getByText(/has not been allocated yet/)).toBeTruthy();
  });

  it('draws no generate button on a frozen version', () => {
    show({ state: state({ editable: false }) });
    expect(screen.queryByText('Generate Allocation')).toBeNull();
  });

  it('draws no generate button for a reader who may not edit', () => {
    show({ canEdit: false });
    expect(screen.queryByText('Generate Allocation')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // A run in flight
  // -------------------------------------------------------------------------

  it('shows the stage list and the percentage while running', () => {
    show({
      job: job({
        status: 'PROCESSING',
        progress_percent: 67,
        current_stage: 'CUSTOMER_ALLOCATION',
        current_stage_label: 'Customer Allocation',
        result: null,
      }),
    });
    expect(screen.getByText('67%')).toBeTruthy();
    expect(screen.getByText(/Allocation in progress/)).toBeTruthy();
    for (const stage of STAGES) {
      expect(screen.getByText(stage.label)).toBeTruthy();
    }
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('67');
  });

  it('disables the button while a run is in flight', () => {
    show({ job: job({ status: 'PROCESSING', progress_percent: 30 }) });
    const button = screen.getByText('Allocating…').closest('button');
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('draws the stage list from the job, not from a copy', () => {
    show({
      job: job({
        status: 'PROCESSING',
        stages: [{ key: 'NEW_STAGE', label: 'A Stage Added Later' }],
        current_stage: 'NEW_STAGE',
        result: null,
      }),
    });
    expect(screen.getByText('A Stage Added Later')).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Completion
  // -------------------------------------------------------------------------

  it('shows a prominent balanced indicator when the totals agree', () => {
    show({ job: job() });
    expect(screen.getByText('Balanced · Reconciled')).toBeTruthy();
    expect(screen.getByText('1,840')).toBeTruthy();
  });

  it('reports the counts the specification asks for', () => {
    show({ job: job() });
    expect(screen.getByText('Customers')).toBeTruthy();
    expect(screen.getByText('Materials')).toBeTruthy();
    expect(screen.getByText('Rows Generated')).toBeTruthy();
    expect(screen.getByText('Allocated Volume')).toBeTruthy();
    expect(screen.getByText('Remaining Volume')).toBeTruthy();
  });

  it('shows a mismatch rather than a balanced indicator when it does not add up', () => {
    const broken = reconciliation({
      balanced: false,
      allocated_volume: '119999.0000',
      difference: '-1.0000',
      mismatch_count: 1,
      mismatches: [
        {
          kind: 'PARENT_CHILD',
          level: 'region',
          node_code: 'REG001',
          material_code: 'SKU001',
          target_month: '2026-07',
          expected: '5000.0000',
          actual: '4999.0000',
          difference: '-1.0000',
        },
      ],
    });
    show({ state: state({ reconciliation: broken }), job: job() });
    expect(screen.getByText('Reconciliation mismatch')).toBeTruthy();
    expect(screen.queryByText('Balanced · Reconciled')).toBeNull();
    expect(screen.getByText(/REG001/)).toBeTruthy();
  });

  it('shows the failure message and no counts on a failed run', () => {
    show({
      job: job({
        status: 'FAILED',
        progress_percent: 40,
        error_message: 'The allocation stopped unexpectedly.',
        result: null,
      }),
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The allocation stopped unexpectedly.',
    );
    expect(screen.queryByText('Balanced · Reconciled')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // The empty-warehouse case, which is the one that matters most
  // -------------------------------------------------------------------------

  const noHistoryJob = job({
    status: 'NO_HISTORY',
    progress_percent: 100,
    rows_processed: 0,
    total_rows: 0,
    error_message:
      'No sales were recorded in FY 2024-25 or FY 2025-26 for this plan’s scope.',
    result: {
      reconciliation: null,
      sales_rows_found: 0,
      basis_years: ['FY 2024-25', 'FY 2025-26'],
      months: [],
      materials: ['SKU001'],
      material_count: 1,
      node_count: 25,
      customer_count: 5,
      row_count: 0,
      factors: [
        {
          key: 'historical_contribution',
          label: 'Historical Sales Contribution',
          enabled: true,
          weight: 40,
          available: false,
          reason: 'No sales were recorded in the basis years for this scope.',
        },
        {
          key: 'customer_potential',
          label: 'Customer Potential',
          enabled: false,
          weight: 8,
          available: false,
          reason: 'The Customer Master states no potential.',
        },
      ],
      warnings: [],
    },
  });

  it('never says completed when there is no sales history', () => {
    show({ job: noHistoryJob });
    expect(screen.queryByText(/Allocation complete/)).toBeNull();
    expect(screen.getByText('Historical Sales Data Unavailable')).toBeTruthy();
  });

  it('reports the rows found, the period checked and the status', () => {
    show({ job: noHistoryJob });
    expect(screen.getByText('Sales rows found')).toBeTruthy();
    expect(screen.getByText('0')).toBeTruthy();
    expect(screen.getByText('FY 2024-25 · FY 2025-26')).toBeTruthy();
    expect(screen.getByText('Not Allocated')).toBeTruthy();
  });

  it('shows no balanced indicator and no generated rows', () => {
    show({ job: noHistoryJob });
    expect(screen.queryByText('Balanced · Reconciled')).toBeNull();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('names the factors that could not be calculated, and why', () => {
    show({ job: noHistoryJob });
    expect(
      screen.getByText('No sales were recorded in the basis years for this scope.'),
    ).toBeTruthy();
    expect(
      screen.getByText('The Customer Master states no potential.'),
    ).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Factors and warnings
  // -------------------------------------------------------------------------

  it('marks an active factor with its weight', () => {
    show({
      job: job({
        result: {
          ...job().result!,
          factors: [
            {
              key: 'historical_contribution',
              label: 'Historical Sales Contribution',
              enabled: true,
              weight: 40,
              available: true,
              reason: null,
            },
          ],
        },
      }),
    });
    const row = screen.getByText('Historical Sales Contribution').closest('li')!;
    expect(within(row).getByText('40')).toBeTruthy();
    expect(within(row).getByText('✓')).toBeTruthy();
  });

  it('shows a fallback warning rather than hiding it', () => {
    const warning = 'SKU001: monthly split used the equal seasonal pattern.';
    show({ job: job({ result: { ...job().result!, warnings: [warning] } }) });
    expect(screen.getByText(warning)).toBeTruthy();
  });
});
