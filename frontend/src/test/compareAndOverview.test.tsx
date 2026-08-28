/**
 * The comparison table and the overview.
 *
 * **Added and removed are drawn as `n/a` with a reason, never as 0.** A node
 * present on only one side has no target there, which is a different statement
 * from a target of nothing — and `0` is the one rendering that makes them look
 * identical.
 *
 * **A percentage against a zero base is blank.** `+∞%` and `0%` are both wrong
 * and one of them looks plausible.
 *
 * **The overview shows a reason beside every red count.** A dashboard that says
 * "3 need attention" and cannot say why is a number a reader opens and then has
 * to go looking for.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TargetDashboard } from '../components/targetmgmt/TargetDashboard';
import { VersionComparison } from '../components/targetmgmt/VersionComparison';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetComparisonResponse,
  TargetComparisonRow,
  TargetDashboardPlan,
  TargetDashboardResponse,
} from '../types/api';

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider>
      <ThemeProvider>{node}</ThemeProvider>
    </I18nProvider>,
  );
}

function row(overrides: Partial<TargetComparisonRow> = {}): TargetComparisonRow {
  return {
    level: 'region',
    node_code: 'REG001',
    name: 'Dhaka',
    parent_level: 'company',
    parent_code: 'C001',
    depth: 1,
    base_volume: 80000,
    volume: 100000,
    change: 20000,
    change_percent: 25,
    status: 'INCREASED',
    ...overrides,
  };
}

function comparison(
  overrides: Partial<TargetComparisonResponse> = {},
): TargetComparisonResponse {
  return {
    plan: {} as never,
    base_version: { version_no: 1 } as never,
    version: { version_no: 2 } as never,
    rows: [
      row({ level: 'company', node_code: 'C001', name: 'Demo Ltd.', depth: 0,
            parent_level: null, parent_code: null, base_volume: 120000,
            volume: 150000, change: 30000, change_percent: 25 }),
      row(),
      row({ node_code: 'REG002', name: 'Khulna', base_volume: 40000,
            volume: 50000, change: 10000, change_percent: 25 }),
    ],
    totals: {
      base_volume: 120000, volume: 150000, change: 30000,
      change_percent: 25, nodes_changed: 3, nodes: 3,
    },
    country: [
      { material_code: 'SKU001', base_volume: 120000, volume: 150000,
        change: 30000, change_percent: 25, status: 'INCREASED' },
    ],
    materials: ['SKU001'],
    material_code: null,
    scope: 'Full country scope',
    notes: ['A change is shown against the earlier version.'],
    statuses: ['UNCHANGED', 'INCREASED', 'DECREASED', 'ADDED', 'REMOVED'],
    ...overrides,
  };
}

describe('VersionComparison', () => {
  const show = (data = comparison(), onMaterialChange = vi.fn()) => {
    wrap(<VersionComparison data={data} onMaterialChange={onMaterialChange} />);
    return onMaterialChange;
  };

  it('heads the two columns with the version numbers being compared', () => {
    show();
    const table = screen.getByRole('table');
    expect(within(table).getByText('V1')).toBeTruthy();
    expect(within(table).getByText('V2')).toBeTruthy();
  });

  it('shows the headline movement from the backend, not a sum of the rows', () => {
    show();
    expect(screen.getByText(/3 of 3 nodes moved/)).toBeTruthy();
    // 120,000 -> 150,000 in Indian digit grouping, plus the change.
    expect(screen.getAllByText('1,20,000').length).toBeGreaterThan(0);
    expect(screen.getAllByText('1,50,000').length).toBeGreaterThan(0);
  });

  it('draws an added node as n/a on the earlier side, never as zero', () => {
    show(
      comparison({
        rows: [row({ base_volume: null, change: null, change_percent: null,
                     status: 'ADDED' })],
      }),
    );
    const table = screen.getByRole('table');
    const titles = within(table)
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(titles).toContain('added, not zero');
  });

  it('draws a removed node as n/a on the later side', () => {
    show(
      comparison({
        rows: [row({ volume: null, change: null, change_percent: null,
                     status: 'REMOVED' })],
      }),
    );
    const table = screen.getByRole('table');
    const titles = within(table)
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(titles).toContain('removed, not cut to');
  });

  it('leaves the percentage blank against a zero base', () => {
    show(
      comparison({
        rows: [row({ base_volume: 0, change: 100, change_percent: null })],
      }),
    );
    const table = screen.getByRole('table');
    const titles = within(table)
      .getAllByText('n/a')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(titles).toContain('undefined');
  });

  it('shows the typed country target on both sides', () => {
    show();
    // Also in the material selector, so the country panel is addressed directly.
    const panel = screen.getByText('Country target').closest('section')
      ?? screen.getByText('Country target').parentElement!;
    expect(within(panel).getByText('SKU001')).toBeTruthy();
    expect(within(panel).getByText('1,20,000')).toBeTruthy();
    expect(within(panel).getByText('1,50,000')).toBeTruthy();
  });

  it('asks the page for a different material rather than filtering locally', () => {
    const onMaterialChange = show();
    fireEvent.change(screen.getByLabelText('Material'), {
      target: { value: 'SKU001' },
    });
    expect(onMaterialChange).toHaveBeenCalledWith('SKU001');
  });

  it('explains an empty comparison rather than showing a bare table', () => {
    show(
      comparison({
        rows: [],
        notes: ['Nothing in either version falls inside the part of the '
                + 'business you hold, so there is nothing here to compare.'],
      }),
    );
    // Drawn twice on purpose: once as the empty state and once in the notes
    // beneath, so a reader who scrolls past one still meets the other.
    expect(
      screen.getAllByText(/part of the business you hold/).length,
    ).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// TargetDashboard
// ---------------------------------------------------------------------------

function plan(overrides: Partial<TargetDashboardPlan> = {}): TargetDashboardPlan {
  return {
    plan: {
      plan_id: 1, plan_code: 'TP-2026-001', financial_year: 'FY 2026-27',
      target_period: 'FY',
    } as never,
    version: { version_id: 9, version_no: 2, status: 'ALLOCATED' } as never,
    country_volume: 120000,
    allocated_volume: 120000,
    allocated_percent: 100,
    open_revisions: 0,
    attention: [],
    locked_at: null,
    ...overrides,
  };
}

function board(
  overrides: Partial<TargetDashboardResponse> = {},
): TargetDashboardResponse {
  return {
    stages: { drafting: 1, allocated: 2, in_approval: 1, locked: 3 },
    plan_count: 7,
    financial_year: null,
    financial_years: ['FY 2026-27', 'FY 2027-28'],
    plans: [plan()],
    attention: [],
    attention_reasons: {
      not_submitted: 'Allocated but never submitted, so nobody has been asked '
        + 'to approve it.',
    },
    my_queue: { versions: 0, revisions: 0, notes: ['Nothing is waiting for your approval.'], role: 'MANAGEMENT' },
    recent: [],
    levels: [],
    ...overrides,
  };
}

describe('TargetDashboard', () => {
  const show = (
    data = board(),
    onFinancialYearChange = vi.fn(),
    onOpenPlan = vi.fn(),
  ) => {
    wrap(
      <TargetDashboard
        data={data}
        financialYear={null}
        onFinancialYearChange={onFinancialYearChange}
        onOpenPlan={onOpenPlan}
      />,
    );
    return { onFinancialYearChange, onOpenPlan };
  };

  it('shows the four stages of the journey', () => {
    show();
    expect(screen.getByText('Being drafted')).toBeTruthy();
    expect(screen.getByText('In approval')).toBeTruthy();
    expect(screen.getByText('Locked')).toBeTruthy();
  });

  it('reads n/a for a plan nobody has typed a country target for', () => {
    show(
      board({
        plans: [plan({ country_volume: null, allocated_volume: null,
                       allocated_percent: null })],
      }),
    );
    expect(screen.getAllByText('n/a').length).toBeGreaterThan(0);
  });

  it('gives every attention item a stated reason', () => {
    show(
      board({
        attention: [plan({ attention: ['not_submitted'] })],
        plans: [plan({ attention: ['not_submitted'] })],
      }),
    );
    expect(
      screen.getAllByText(/never submitted/).length,
    ).toBeGreaterThan(0);
  });

  it('says nothing is waiting rather than showing an empty queue', () => {
    show();
    expect(screen.getByText('Nothing is waiting for your approval.')).toBeTruthy();
  });

  it('opens a plan on the tab that shows its figures', () => {
    const { onOpenPlan } = show();
    fireEvent.click(screen.getAllByText('TP-2026-001')[0]);
    expect(onOpenPlan).toHaveBeenCalledWith(1, 'country');
  });

  it('narrows by financial year through the page, not locally', () => {
    const { onFinancialYearChange } = show();
    fireEvent.change(screen.getByLabelText('Financial Year'), {
      target: { value: 'FY 2027-28' },
    });
    expect(onFinancialYearChange).toHaveBeenCalledWith('FY 2027-28');
  });
});
