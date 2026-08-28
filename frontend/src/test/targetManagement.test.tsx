/**
 * Target Management: what the page draws, and what it refuses to draw.
 *
 * Two things are worth pinning. The scope form's selects must **cascade** — a
 * business unit under a different company is not a valid child, and offering it
 * would let a planner build a scope the backend then refuses with a 409 they did
 * nothing to deserve. And the create and workflow controls must be **absent**
 * for a caller whose `actions` say no: that is presentation rather than
 * security, but a button that can only produce a 403 is a lie about what the
 * reader may do.
 *
 * The 409 path is pinned too. A state refusal from this API carries a message
 * worth reading — which plan already holds the scope — and the page shows it
 * verbatim instead of a generic failure.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import TargetManagementPage from '../pages/TargetManagementPage';
import * as services from '../services';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 1, username: 'root', role: 'SUPER_ADMIN', is_admin: true },
    isAdmin: true,
    hasSection: () => true,
  }),
}));

const OPTIONS = {
  companies: [
    { code: 'C001', name: 'Demo Industries Ltd.' },
    { code: 'C002', name: 'Other Ltd.' },
  ],
  business_units: [
    { code: 'BU001', name: 'Crop Protection', company_code: 'C001' },
    { code: 'BU009', name: 'Elsewhere', company_code: 'C002' },
  ],
  sales_lines: [
    { code: 'SL001', name: 'General Trade', bu_code: 'BU001' },
    { code: 'SL009', name: 'Not Here', bu_code: 'BU009' },
  ],
  periods: [
    { code: 'FY', label: 'FY (full year)' },
    { code: 'Q1', label: 'Q1 (Jul – Sep)' },
  ],
  financial_years: ['FY 2026-27', 'FY 2025-26'],
  statuses: ['DRAFT', 'APPROVED', 'LOCKED'],
  actions: { VIEW: true, CREATE: true, EDIT: true, EXPORT: true },
};

const PLANS = {
  total: 1,
  plans: [
    {
      plan_id: 7,
      plan_code: 'TP-2026-001',
      financial_year: 'FY 2026-27',
      target_period: 'Q1',
      period_label: 'FY 2026-27 · Q1 (Jul – Sep)',
      company_code: 'C001',
      bu_code: 'BU001',
      sales_line_code: 'SL001',
      basis_financial_years: null,
      status: 'UNDER_REVIEW',
      created_by: 'root',
      created_at: '2026-08-27T09:00:00',
      current_version_id: 12,
      current_version_no: 3,
      current_version_status: 'UNDER_REVIEW',
    },
  ],
};

const VERSIONS = {
  versions: [
    {
      version_id: 12,
      plan_id: 7,
      version_no: 3,
      label: 'V3',
      status: 'UNDER_REVIEW',
      is_current: true,
      reason: 'Herbicide demand review.',
      created_by: 'root',
      created_at: '2026-08-18T09:12:00',
      submitted_at: null,
      approved_at: null,
      locked_at: null,
      locked_batch_id: null,
      country_line_count: 6,
    },
    {
      version_id: 11,
      plan_id: 7,
      version_no: 2,
      label: 'V2',
      status: 'LOCKED',
      is_current: false,
      reason: 'Raised after Q4 actuals closed.',
      created_by: 'root',
      created_at: '2026-05-03T15:41:00',
      submitted_at: null,
      approved_at: null,
      locked_at: '2026-07-12T12:02:00',
      locked_batch_id: null,
      country_line_count: 6,
    },
  ],
};

function wrap(initialEntry = '/target-management') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[initialEntry]}>
            <TargetManagementPage />
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

type AnyFn = (...args: never[]) => unknown;

/**
 * The three reads every test needs, plus whichever write it is exercising.
 *
 * The write is spied through a loosely-typed view of the service: this is test
 * plumbing standing in for one method at a time, and threading each method's
 * exact signature through would say nothing the test body does not already.
 */
function stubServices(overrides: Record<string, AnyFn> = {}) {
  vi.spyOn(services.targetManagementService, 'options').mockResolvedValue(
    OPTIONS as never,
  );
  vi.spyOn(services.targetManagementService, 'plans').mockResolvedValue(
    PLANS as never,
  );
  vi.spyOn(services.targetManagementService, 'versions').mockResolvedValue(
    VERSIONS as never,
  );
  const loose = services.targetManagementService as unknown as Record<string, AnyFn>;
  for (const [name, value] of Object.entries(overrides)) {
    vi.spyOn(loose, name).mockImplementation(value);
  }
}

/**
 * The select under a label *inside the create form*.
 *
 * Scoped rather than searched page-wide: "Company" and "Sales Line" are also
 * column headers on the plans table below, so an unscoped query matches more
 * than one node and cannot tell a form control from a table heading.
 */
function createForm(): HTMLElement {
  return screen.getByText('Create Target Plan').closest('section') as HTMLElement;
}

function selectFor(label: string): HTMLSelectElement {
  return within(createForm())
    .getByText(label)
    .parentElement!.querySelector('select') as HTMLSelectElement;
}

function optionValues(select: HTMLSelectElement): string[] {
  return Array.from(select.options)
    .map((option) => option.value)
    .filter(Boolean);
}

describe('TargetManagementPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('lists existing plans with their current version', async () => {
    stubServices();
    wrap();
    expect(await screen.findByText('TP-2026-001')).toBeTruthy();
    expect(screen.getByText('FY 2026-27 · Q1 (Jul – Sep)')).toBeTruthy();
    expect(screen.getByText('V3')).toBeTruthy();
    expect(screen.getByText('UNDER REVIEW')).toBeTruthy();
  });

  it('narrows business unit by company, and sales line by business unit', async () => {
    stubServices();
    wrap();
    await screen.findByText('TP-2026-001');

    const company = selectFor('Company');
    const unit = selectFor('Business Unit');
    const line = selectFor('Sales Line');

    // Nothing chosen: the dependent selects are disabled rather than offering
    // a child of a parent nobody has picked.
    expect(unit.disabled).toBe(true);
    expect(line.disabled).toBe(true);

    fireEvent.change(company, { target: { value: 'C001' } });
    expect(selectFor('Business Unit').disabled).toBe(false);
    expect(optionValues(selectFor('Business Unit'))).toEqual(['BU001']);

    fireEvent.change(selectFor('Business Unit'), { target: { value: 'BU001' } });
    expect(optionValues(selectFor('Sales Line'))).toEqual(['SL001']);
  });

  it('clears the children when the company changes', async () => {
    stubServices();
    wrap();
    await screen.findByText('TP-2026-001');

    fireEvent.change(selectFor('Company'), { target: { value: 'C001' } });
    fireEvent.change(selectFor('Business Unit'), { target: { value: 'BU001' } });
    fireEvent.change(selectFor('Sales Line'), { target: { value: 'SL001' } });
    expect(selectFor('Sales Line').value).toBe('SL001');

    // A business unit under the previous company is not a valid child of this
    // one, so both selections go rather than silently becoming invalid.
    fireEvent.change(selectFor('Company'), { target: { value: 'C002' } });
    expect(selectFor('Business Unit').value).toBe('');
    expect(selectFor('Sales Line').value).toBe('');
  });

  it('creates a plan from the chosen scope', async () => {
    const createPlan = vi.fn().mockResolvedValue({ plan: PLANS.plans[0] });
    stubServices({ createPlan });
    wrap();
    await screen.findByText('TP-2026-001');

    fireEvent.change(selectFor('Company'), { target: { value: 'C001' } });
    fireEvent.change(selectFor('Business Unit'), { target: { value: 'BU001' } });
    fireEvent.change(selectFor('Sales Line'), { target: { value: 'SL001' } });
    fireEvent.click(screen.getByText('Create Plan'));

    await waitFor(() => expect(createPlan).toHaveBeenCalled());
    expect(createPlan.mock.calls[0][0]).toMatchObject({
      financial_year: 'FY 2026-27',
      company_code: 'C001',
      bu_code: 'BU001',
      sales_line_code: 'SL001',
    });
  });

  it('shows the reason a state refusal gives, not a generic failure', async () => {
    const message =
      'A target plan for this financial year, period and sales line already ' +
      'exists (TP-2026-001).';
    const createPlan = vi.fn().mockRejectedValue({
      detail: { error_code: 'TARGET_PLAN_SCOPE_CONFLICT', message },
    });
    stubServices({ createPlan });
    wrap();
    await screen.findByText('TP-2026-001');

    fireEvent.change(selectFor('Company'), { target: { value: 'C001' } });
    fireEvent.change(selectFor('Business Unit'), { target: { value: 'BU001' } });
    fireEvent.change(selectFor('Sales Line'), { target: { value: 'SL001' } });
    fireEvent.click(screen.getByText('Create Plan'));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
  });

  it('hides the create form from a caller who may not create', async () => {
    vi.spyOn(services.targetManagementService, 'options').mockResolvedValue({
      ...OPTIONS,
      actions: { VIEW: true, EXPORT: true },
    } as never);
    vi.spyOn(services.targetManagementService, 'plans').mockResolvedValue(
      PLANS as never,
    );
    vi.spyOn(services.targetManagementService, 'versions').mockResolvedValue(
      VERSIONS as never,
    );
    wrap();
    await screen.findByText('TP-2026-001');
    expect(screen.queryByText('Create Plan')).toBeNull();
  });

  it('shows a version chain, marking the current one', async () => {
    stubServices();
    wrap('/target-management?tab=versions&plan=7');
    expect(await screen.findByText('V3')).toBeTruthy();
    expect(screen.getByText('V2')).toBeTruthy();
    expect(screen.getByText('CURRENT')).toBeTruthy();
    expect(screen.getByText('Herbicide demand review.')).toBeTruthy();
  });

  it('offers no transition out of a locked version', async () => {
    stubServices();
    wrap('/target-management?tab=versions&plan=7');
    await screen.findByText('V2');

    // V3 is under review, so it offers the three decisions. V2 is locked and
    // offers nothing — a locked version is changed only by creating the next.
    expect(screen.getByText('APPROVED')).toBeTruthy();
    expect(screen.getByText('REJECTED')).toBeTruthy();
    // "LOCKED" appears as V2's status pill; there must be no *button* for it,
    // because nothing under review may jump straight to locked either.
    const lockedButtons = screen
      .getAllByText('LOCKED')
      .filter((node) => node.tagName === 'BUTTON');
    expect(lockedButtons).toHaveLength(0);
  });

  it('asks for a plan before showing versions', async () => {
    stubServices();
    wrap('/target-management?tab=versions');
    expect(
      await screen.findByText(
        'Choose a plan from Target Planning to see its versions.',
      ),
    ).toBeTruthy();
  });
});
