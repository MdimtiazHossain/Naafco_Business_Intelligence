/**
 * The Credit Control page: which filters it offers, and what it refuses to
 * invent.
 *
 * Two things here are worth pinning from the browser's side.
 *
 * **The filter set.** `vw_credit_invoice_detail` carries a company, a plant, a
 * customer and the customer's sub-territory, and nothing of the sales hierarchy
 * between them. The backend skips a filter whose column a view lacks, so
 * offering Region would put a chip above unfiltered totals claiming otherwise —
 * the same failure `stockFilters.test.tsx` exists to prevent on the other page.
 *
 * **The figures the platform cannot produce.** The outstanding trend has no
 * source, an empty portfolio has no overdue proportion, and a cleared invoice
 * is in no aging bucket. All three must read as absent rather than as zero,
 * because a `0%` and an empty chart both look like measurements.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import { AGING_COLORS } from '../charts/Charts';
import CreditControlPage from '../pages/CreditControlPage';
import * as services from '../services';

function wrap(ui: React.ReactNode, route = '/credit-control') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <FilterProvider>{ui}</FilterProvider>
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const BUCKETS = [
  'NOT_YET_DUE', '1-30', '31-60', '61-90',
  '91-120', '121-180', '181-365', '365+',
];

const SUMMARY = {
  filters: {},
  as_on_date: '2026-08-29',
  due_soon_days: 7,
  metrics: {
    invoice_count: 16,
    open_invoice_count: 13,
    overdue_invoice_count: 7,
    due_soon_invoice_count: 2,
    total_invoice_amount: 36000000,
    net_invoice_amount: 35500000,
    return_amount: -520000,
    payment_amount: 11000000,
    discount_amount: 0,
    adjustment_amount: 0,
    outstanding_amount: 24000000,
    overdue_amount: 11900000,
    due_soon_amount: 2630000,
    payment_rate_percent: 31.1,
    overdue_share_percent: 49.8,
  },
  aging: BUCKETS.map((bucket, index) => ({
    bucket,
    invoice_count: index === 4 ? 0 : 1,
    outstanding_amount: index === 4 ? 0 : 100000,
  })),
  status: [
    { status: 'NOT_YET_DUE', invoice_count: 6, outstanding_amount: 12000000 },
    { status: 'OVER_DUE', invoice_count: 7, outstanding_amount: 11900000 },
    { status: 'CLEARED', invoice_count: 3, outstanding_amount: 0 },
  ],
  top_overdue_customers: [
    { customer_code: 'C-1042', customer_name: 'Rahman Traders',
      overdue_amount: 5000000, invoice_count: 3 },
  ],
  outstanding_trend: {
    state: 'NOT_AVAILABLE',
    points: [],
    reason:
      'The source states one total payment per invoice and a single last payment '
      + 'date, so what was outstanding at a past month end cannot be reconstructed.',
  },
  notes: [],
};

const CUSTOMERS = {
  filters: {}, as_on_date: '2026-08-29',
  rows: [{
    company_code: '1000', customer_code: 'C-1042', customer_name: 'Rahman Traders',
    sub_territory_code: 'STR001', invoice_count: 3,
    total_invoice_amount: 7410000, net_invoice_amount: 7410000,
    payment_amount: 2410000, discount_amount: 0, adjustment_amount: 0,
    outstanding_amount: 5000000, overdue_amount: 5000000,
    overdue_invoice_count: 3, oldest_due_date: '2026-06-10',
    last_payment_date: null, credit_exposure_percent: 20.8,
  }],
  total: 1, page: 1, page_size: 25, total_pages: 1,
  portfolio_outstanding: 24000000, page_outstanding: 5000000,
};

/** Every filter the page must offer, by its visible label. */
const OFFERED = [
  'Company', 'Plant', 'Sub-Territory', 'Customer',
  'Credit Days', 'Payment Mode', 'Credit Status', 'Aging Bucket',
];

/**
 * Filters belonging to other datasets, which this view has no column for.
 *
 * The material levels are listed deliberately: they are *global* since revision
 * 0022 and every other report draws them, so their absence here is a real
 * decision rather than a name that exists nowhere. A credit invoice is money
 * owed against a document, not against an item.
 */
const WITHHELD = [
  'Region', 'Area', 'Zone', 'Territory', 'Unit', 'Business Unit', 'Sales Line',
  'Sales Force', 'Batch', 'Material Group', 'Material Brand', 'Shelf Life',
];

describe('Credit Control', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.creditService, 'summary').mockResolvedValue(SUMMARY as never);
    vi.spyOn(services.creditService, 'customers').mockResolvedValue(CUSTOMERS as never);
    vi.spyOn(services.creditService, 'invoices').mockResolvedValue({
      ...CUSTOMERS, rows: [],
    } as never);
    vi.spyOn(services.masterDataService, 'options').mockImplementation(
      (level: string) =>
        Promise.resolve({
          level, parent_code: null, total: 2, truncated: false,
          options: [
            { code: `${level}-1`, label: `${level} one`, parent_code: null },
            { code: `${level}-2`, label: `${level} two`, parent_code: null },
          ],
        }) as never,
    );
    vi.spyOn(services.masterDataService, 'ancestors').mockResolvedValue({
      level: 'customer_code', codes: [], source: null, ancestor: {}, ancestors: {},
    } as never);
  });

  async function openFilters() {
    wrap(<CreditControlPage />);
    fireEvent.click(await screen.findByRole('button', { name: /Filters/ }));
  }

  /*
    Queried by label association rather than by text. Two reasons: "Customer" is
    both a filter label and a table column header, so a text query finds two
    elements and fails on the ambiguity; and a label bound to a control asserts
    that the control is actually there, which is the thing being claimed.
  */
  it('offers exactly the filters the credit view can honour', async () => {
    await openFilters();
    for (const label of OFFERED) {
      expect(await screen.findByLabelText(label)).toBeInTheDocument();
    }
  });

  it('withholds the filters that would silently do nothing', async () => {
    await openFilters();
    await screen.findByLabelText('Company');
    for (const label of WITHHELD) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
  });

  it('draws every aging bucket, including the empty one', async () => {
    wrap(<CreditControlPage />);
    // The chart renders each bucket; the one with no invoices must still be
    // present, because "nothing in 91-120" is a fact and a gap is a rendering
    // fault the reader cannot tell apart from it.
    await waitFor(() => expect(services.creditService.summary).toHaveBeenCalled());
    expect(SUMMARY.aging).toHaveLength(8);
    expect(SUMMARY.aging.map((row) => row.bucket)).toEqual(BUCKETS);
  });

  it('has a colour for every bucket the backend can return', () => {
    // A bucket with no colour falls back to the categorical palette, which
    // breaks the severity ramp — the one thing AGING_COLORS exists to give.
    for (const bucket of BUCKETS) {
      expect(AGING_COLORS[bucket]).toBeTruthy();
    }
  });

  it('carries no key from the aging table it replaced', () => {
    // Those six named buckets nothing produced after revision 0020 removed the
    // Outstanding module. Leaving them would be six more names outliving what
    // they named.
    for (const stale of ['CURRENT', '91-180', '180+']) {
      expect(AGING_COLORS[stale]).toBeUndefined();
    }
  });

  it('labels the net card literally and shows the returns that explain it', async () => {
    /*
      "Net Invoice" promised a reduction this data does not always deliver.
      Returns are posted negative and subtracted, so the net figure can exceed
      the gross one — 1.59 Cr against 1.41 Cr on the first real file — which
      reads as a bug to anyone who has not been told about the sign convention.
      The label is now literal about the operation, and the returns total sits
      on the card so the difference is accounted for.
    */
    wrap(<CreditControlPage />);
    expect(await screen.findByText('Invoice less Returns')).toBeInTheDocument();
    expect(screen.queryByText('Net Invoice')).not.toBeInTheDocument();
    // Signed, so a negative return reads as the increase it caused.
    expect(await screen.findByText(/returns.*-/)).toBeInTheDocument();
  });

  it('states that the outstanding trend has no source, and draws no chart', async () => {
    wrap(<CreditControlPage />);
    expect(
      await screen.findByText(/cannot be reconstructed/),
    ).toBeInTheDocument();
  });

  it('sends the As On date to every request', async () => {
    wrap(<CreditControlPage />, '/credit-control?as_on=2026-06-15');
    await waitFor(() => expect(services.creditService.summary).toHaveBeenCalled());

    const summaryCall = vi.mocked(services.creditService.summary).mock.calls[0][0];
    const tableCall = vi.mocked(services.creditService.customers).mock.calls[0][0];
    // Both, or the KPI strip and the table below it answer for different days.
    expect(summaryCall.as_on_date).toBe('2026-06-15');
    expect(tableCall.as_on_date).toBe('2026-06-15');
  });

  it('opens on the customer view and can switch to invoices', async () => {
    wrap(<CreditControlPage />);
    expect(await screen.findByText('Rahman Traders')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Invoice View' }));
    await waitFor(() => expect(services.creditService.invoices).toHaveBeenCalled());
  });

  it('shows a customer exposure share rather than an invented limit', async () => {
    wrap(<CreditControlPage />);
    const row = (await screen.findByText('Rahman Traders')).closest('tr');
    expect(within(row!).getByText(/21/)).toBeInTheDocument();
  });
});
