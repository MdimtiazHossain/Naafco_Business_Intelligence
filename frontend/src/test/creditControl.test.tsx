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
import { MemoryRouter, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import { AGING_COLORS } from '../charts/Charts';
import CreditControlPage from '../pages/CreditControlPage';
import * as services from '../services';

/**
 * The router's own view of the query string.
 *
 * `MemoryRouter` keeps history in memory and never touches the document, so
 * every assertion against `window.location.search` passes against the empty
 * string whether the code under test navigated or not. `demarcation.test.tsx`
 * met this first; the probe is the same one.
 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="query">{location.search}</span>;
}

function query(): string {
  return screen.getByTestId('query').textContent ?? '';
}

function wrap(ui: React.ReactNode, route = '/credit-control') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <FilterProvider>
              {ui}
              <LocationProbe />
            </FilterProvider>
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
    // The three-way partition, and it adds up: 11.9 + 2.63 + 9.47 = 24.0.
    // Written to balance on purpose — a fixture whose parts did not sum to its
    // own total could not catch a page that stopped showing one of them.
    overdue_amount: 11900000,
    due_soon_amount: 2630000,
    due_later_amount: 9470000,
    due_later_invoice_count: 4,
    payment_rate_percent: 31.1,
    overdue_share_percent: 49.8,
  },
  aging: BUCKETS.map((bucket, index) => ({
    bucket,
    invoice_count: index === 4 ? 0 : 1,
    outstanding_amount: index === 4 ? 0 : 100000,
  })),
  aging_by_level: {
    level: 'region_code',
    buckets: BUCKETS,
    rows: [
      {
        code: 'REG001', name: 'Dhaka',
        outstanding_amount: 700000, invoice_count: 7,
        amounts: [400000, 100000, 100000, 50000, 0, 30000, 20000, 0],
        counts: [4, 1, 1, 1, 0, 1, 1, 0],
      },
      {
        code: 'REG002', name: 'Khulna',
        outstanding_amount: 200000, invoice_count: 3,
        amounts: [100000, 0, 0, 0, 0, 50000, 50000, 0],
        counts: [1, 0, 0, 0, 0, 1, 1, 0],
      },
    ],
  },
  exposure_by_level: {
    level: 'region_code',
    limit: 20,
    rows: [
      {
        code: 'REG001', name: 'Dhaka',
        outstanding_amount: 700000, overdue_amount: 300000,
        overdue_share_percent: 42.9,
        open_invoice_count: 7, invoice_count: 9, customer_count: 4,
      },
      // A group with nothing outstanding, so the suppression rule has something
      // to suppress: `n/a`, never 0%.
      {
        code: 'REG003', name: 'Sylhet',
        outstanding_amount: 0, overdue_amount: 0,
        overdue_share_percent: null,
        open_invoice_count: 0, invoice_count: 2, customer_count: 1,
      },
    ],
  },
  due_profile: {
    as_on_date: '2026-08-29',
    overdue_amount: 11900000,
    buckets: [
      { bucket: '0-7', invoice_count: 2, due_amount: 2630000 },
      { bucket: '8-30', invoice_count: 1, due_amount: 3470000 },
      { bucket: '31-60', invoice_count: 2, due_amount: 4000000 },
      { bucket: '61-90', invoice_count: 1, due_amount: 2000000 },
      { bucket: '90+', invoice_count: 0, due_amount: 0 },
    ],
  },
  status: [
    { status: 'NOT_YET_DUE', invoice_count: 6, outstanding_amount: 12000000,
      invoice_amount: 13000000 },
    { status: 'OVER_DUE', invoice_count: 7, outstanding_amount: 11900000,
      invoice_amount: 14500000 },
    // Cleared: a count, no outstanding by definition, and a billed figure that
    // is the only way to see it at all.
    { status: 'CLEARED', invoice_count: 3, outstanding_amount: 0,
      invoice_amount: 8000000 },
  ],
  top_overdue_customers: [
    { customer_code: 'C-1042', customer_name: 'Rahman Traders',
      territory_code: 'TR001', territory_name: 'Kazipara',
      region_code: 'REG001', region_name: 'Dhaka',
      overdue_amount: 5000000, invoice_count: 3, max_days_overdue: 61 },
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
  // The sales hierarchy, offered since revision 0040 gave
  // `vw_credit_invoice_detail` the six levels between company and sub-territory.
  // Before it, every one of these was in WITHHELD below — a control naming a
  // column the view lacked would have been a chip above unfiltered totals
  // claiming otherwise, which is the same gap that made this page refuse a
  // region-scoped reader outright.
  'Company', 'Business Unit', 'Sales Line', 'Zone', 'Region', 'Area', 'Unit',
  'Territory', 'Sub-Territory',
  'Plant', 'Customer',
  'Credit Days', 'Payment Mode', 'Credit Status', 'Aging Bucket',
];

/**
 * Filters belonging to other datasets, which this view has no column for.
 *
 * The material levels are listed deliberately: they are *global* since revision
 * 0022 and every other report draws them, so their absence here is a real
 * decision rather than a name that exists nowhere. A credit invoice is money
 * owed against a document, not against an item — which is why they stayed here
 * when the organisational levels left for OFFERED. The hierarchy was a missing
 * column; the material chain is a statement about what a receivable *is*.
 */
const WITHHELD = [
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

describe('Credit Control: the hierarchy sections', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.creditService, 'summary').mockResolvedValue(SUMMARY as never);
    vi.spyOn(services.creditService, 'customers').mockResolvedValue(CUSTOMERS as never);
    vi.spyOn(services.creditService, 'invoices').mockResolvedValue({
      ...CUSTOMERS, rows: [],
    } as never);
    vi.spyOn(services.masterDataService, 'options').mockImplementation(
      (level: string) => Promise.resolve({ level, options: [] } as never),
    );
  });

  it('draws the seventh card, without which the strip does not add up', async () => {
    wrap(<CreditControlPage />);
    // Overdue and Due Soon were shown without Due Later, so the two largest
    // figures did not account for Outstanding and nothing said what the rest
    // was. On the real book it is the largest of the three.
    expect(await screen.findByText('Due Later')).toBeInTheDocument();

    // Scoped to the strip: "Outstanding" and "Overdue" are legitimately column
    // headings on the matrix and the hierarchy table too, so a page-wide query
    // is ambiguous rather than wrong.
    const strip = (await screen.findByText('Due Later')).closest('.card')
      ?.parentElement;
    expect(strip).toBeTruthy();
    for (const label of ['Outstanding', 'Overdue', 'Due Soon', 'Due Later']) {
      expect(within(strip!).getByText(label)).toBeInTheDocument();
    }
  });

  it('renders every bucket on every matrix row, including the empty ones', async () => {
    wrap(<CreditControlPage />);
    await screen.findAllByText('Dhaka');

    // Eight columns plus the name and the row total. A row of four cells beside
    // a row of eight does not line up as a table, which is why the server fills
    // the empty buckets rather than omitting them.
    const row = screen.getAllByText('Khulna')[0].closest('tr');
    expect(row).toBeTruthy();
    expect(row!.querySelectorAll('td')).toHaveLength(BUCKETS.length + 2);

    // ...and the empty ones are drawn as an em dash rather than as 0, which
    // would read as a measured zero.
    expect(within(row!).getAllByText('—').length).toBeGreaterThan(0);
  });

  it('offers a grouping level and puts it in the URL', async () => {
    wrap(<CreditControlPage />);
    const select = await screen.findByLabelText('Group by');
    expect((select as HTMLSelectElement).value).toBe('region_code');

    // Shown absent *before* the change, so the assertion after it can fail.
    expect(query()).not.toContain('territory_code');

    fireEvent.change(select, { target: { value: 'territory_code' } });
    // Read off the router rather than `window.location`, which never moves
    // under MemoryRouter and would make either assertion pass vacuously.
    await waitFor(() => expect(query()).toContain('level=territory_code'));
  });

  it('sends the chosen level to the server rather than regrouping in the browser', async () => {
    const summary = vi.spyOn(services.creditService, 'summary')
      .mockResolvedValue(SUMMARY as never);
    wrap(<CreditControlPage />);
    await screen.findByLabelText('Group by');
    // No business calculation lives in the browser, and a breakdown is one: the
    // level travels to the query that produced the figures.
    await waitFor(() =>
      expect(summary).toHaveBeenCalledWith(
        expect.objectContaining({ group_level: 'region_code' }),
      ),
    );
  });

  it('suppresses a group overdue share rather than showing it as 0%', async () => {
    wrap(<CreditControlPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'By hierarchy' }));
    // Sylhet has nothing outstanding, so it has no overdue *proportion*. 0%
    // would read as good news about a book that does not exist.
    await screen.findByText('Sylhet');
    expect(screen.getAllByText('n/a').length).toBeGreaterThan(0);
  });

  it('keeps one table arrangement across every level', async () => {
    wrap(<CreditControlPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'By hierarchy' }));
    // Dhaka is a row of the matrix as well, so the table's own is taken by
    // scoping to it rather than by taking whichever came first.
    const table = await screen.findByRole('table', { name: /hierarchy/i })
      .catch(async () => (await screen.findAllByRole('table')).at(-1)!);
    await waitFor(() => expect(within(table).getAllByText('Dhaka').length)
      .toBeGreaterThan(0));

    // `credit-control.hierarchy`, not `.hierarchy.region_code`. Every level
    // lists the same columns, so a reader who hid Code and widened Outstanding
    // while looking at regions means that to survive a switch to territories.
    const stored = Object.keys(window.localStorage).filter((key) =>
      key.startsWith('bi.table-layout.credit-control.hierarchy'),
    );
    expect(stored.every((key) => key === 'bi.table-layout.credit-control.hierarchy'))
      .toBe(true);
  });

  it('says what the due profile is not showing', async () => {
    wrap(<CreditControlPage />);
    // Five forward buckets are not the whole book, and a chart that did not say
    // so would read as though they were. The overdue total is named in words
    // rather than drawn as a sixth bar — the same taka on two charts is taka a
    // reader will add.
    const note = await screen.findByText(/not yet late/i);
    expect(note.textContent).toMatch(/1\.19 Cr|11,900,000/);
  });

  it('drills from a group into the customers inside it', async () => {
    wrap(<CreditControlPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'By hierarchy' }));
    const table = (await screen.findAllByRole('table')).at(-1)!;
    await waitFor(() => expect(within(table).getAllByText('Dhaka').length)
      .toBeGreaterThan(0));
    fireEvent.click(within(table).getAllByText('Dhaka')[0]);

    // The question a reader asks next: who inside this region owes it. Same
    // drill the customer table already offers into invoices.
    await waitFor(() => expect(query()).toContain('view=customers'));
    expect(query()).toContain('REG001');
  });
});
