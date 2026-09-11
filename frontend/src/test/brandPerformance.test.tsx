/**
 * The general performance view is brand-wise, and Material Analysis is not.
 *
 * These tests pin the two halves of that from the browser's side: the dashboard
 * shows a ranked brand table with a single Volume column and no unit beside it,
 * and the Material Analysis page offers all three levels of the Material Master.
 *
 * Every "brand" here is a **material brand**. Revision 0022 removed the SKU
 * master and its separate sales brand, so there is one brand of one set of
 * goods, read from `dim_material`.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import Dashboard from '../pages/Dashboard';
import MaterialsPage from '../pages/MaterialsPage';
import * as services from '../services';

function wrap(ui: React.ReactNode, route = '/') {
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

const PERIOD = {
  type: 'CUSTOM',
  date_from: '2026-08-01',
  date_to: '2026-08-31',
  label: 'August 2026',
};

const BRAND_ROWS = [
  { rank: 1, code: 'Alpha', label: 'Alpha', quantity: 10_000,
    target_volume: 60_000, volume: 50_000,
    target_amount: 15_000_000, net_sales: 12_500_000,
    volume_achievement_percent: 83.33, achievement_percent: 83.33,
    volume_shortfall: -10_000, amount_shortfall: -2_500_000 },
  { rank: 2, code: 'Beta', label: 'Beta', quantity: 8_500,
    target_volume: 40_000, volume: 40_000,
    target_amount: 10_000_000, net_sales: 10_800_000,
    volume_achievement_percent: 100, achievement_percent: 108,
    volume_shortfall: 0, amount_shortfall: 800_000 },
  // No volume was recorded and no target was set, so the volume figure and both
  // achievements are unanswerable.
  { rank: 3, code: 'Gamma', label: 'Gamma', quantity: 7_200,
    target_volume: null, volume: null,
    target_amount: 0, net_sales: 9_500_000,
    volume_achievement_percent: null, achievement_percent: null,
    volume_shortfall: null, amount_shortfall: 9_500_000 },
];

/**
 * The KPI strip exactly as the backend now composes it: six cards, and no
 * Sales Volume among them.
 */
const KPIS = [
  { key: 'total_sales', label: 'Total Sales', value: 18_700_000,
    previous_value: null, growth_percent: null, format: 'currency' },
  { key: 'target', label: 'Target', value: 20_000_000,
    previous_value: null, growth_percent: null, format: 'currency' },
  { key: 'achievement', label: 'Achievement', value: 93.5,
    previous_value: null, growth_percent: null, format: 'percent' },
  { key: 'unrestricted_stock', label: 'Unrestricted Stock (KG/LTR)', value: 125_500,
    previous_value: null, growth_percent: null, format: 'stock' },
  { key: 'expiring_soon_stock', label: 'Expiring Soon (KG/LTR)', value: 289,
    previous_value: null, growth_percent: null, format: 'stock' },
  { key: 'expired_stock', label: 'Expired Stock (KG/LTR)', value: 13_354.8,
    previous_value: null, growth_percent: null, format: 'stock' },
];

/**
 * A monthly trend as the tool sends one: three years and a target, with each
 * month missing something different. That is what the two nullable percentages
 * exist for — no target means no achievement, no prior year means no growth.
 */
const TREND_ROWS = [
  { label: 'Jul 2026', net_sales: 161_449_437, net_sales_minus_1: 166_802_869,
    net_sales_minus_2: 203_202_529, target_amount: 168_912_348,
    achievement_percent: 95.6, growth_percent: -3.2 },
  // No target this month, so no achievement — never 0%.
  { label: 'Aug 2026', net_sales: 223_891_243, net_sales_minus_1: 197_823_740,
    net_sales_minus_2: 196_332_347, target_amount: null,
    achievement_percent: null, growth_percent: 13.2 },
  // The prior year recorded nothing here, so no growth — never -100%.
  { label: 'Sep 2026', net_sales: 58_765, net_sales_minus_1: null,
    net_sales_minus_2: 217_259_641, target_amount: 390_667_700,
    achievement_percent: 0.0, growth_percent: null },
];

/**
 * The Top 50 Customers rows: a name, three years of net sales, and the growth.
 *
 * These figures are a *finished* period — Last Month, say — where the three
 * columns and the growth describe the same spans and 50 L to 62.5 L is the
 * +25% stated. On an unfinished year they would not agree, deliberately: the
 * earlier columns are then whole financial years while the last is the part of
 * this one that has happened, and the growth stays year on year over the
 * elapsed days. The card says so in a note rather than bending either figure,
 * and `test_platform_api` is where that case is pinned.
 */
const CUSTOMER_ROWS = [
  { code: 'CUST-1', label: 'M/S Shamim Enterprise',
    net_sales_minus_2: 5_400_000, net_sales_minus_1: 5_000_000,
    actual_sales: 6_250_000, growth_percent: 25 },
  // No sales two years back: absent, not zero, so the column shows a dash.
  { code: 'CUST-2', label: 'Master Traders',
    net_sales_minus_2: null, net_sales_minus_1: null,
    actual_sales: 5_200_000, growth_percent: null },
];

/**
 * The achievement tool's own spec, which is **not** the trend one above.
 *
 * The difference is the key for this period's actual: a trend calls it
 * `net_sales`, `get_target_achievement` calls it `actual_sales`, and the rows
 * it returns are keyed to match. A fixture that borrowed the trend spec drew
 * two of the three year columns and silently dropped the third — which is what
 * happened while this card was being built.
 */
const ACHIEVEMENT_CHART_SPEC = {
  type: 'bar', x_axis: 'label', y_axis: 'actual_sales',
  series: [
    { key: 'net_sales_minus_1', label: 'FY 2025-26' },
    { key: 'net_sales_minus_2', label: 'FY 2024-25' },
    { key: 'target_amount', label: 'FY 2026-27' },
    { key: 'actual_sales', label: 'FY 2026-27' },
  ],
};

const TREND_CHART_SPEC = {
  type: 'line', x_axis: 'label', y_axis: 'net_sales',
  series: [
    { key: 'net_sales', label: 'Jul 2026 - Sep 2026' },
    { key: 'net_sales_minus_1', label: 'Jul 2025 - Sep 2025' },
    { key: 'net_sales_minus_2', label: 'Jul 2024 - Sep 2024' },
    { key: 'target_amount', label: 'Jul 2026 - Sep 2026' },
  ],
};

/** The cards, keyed the way the section endpoint serves them. */
const SECTIONS: Record<string, unknown> = {
  // The Sales Trend line chart follows the reader's period. `monthly_performance`
  // below is the combo card's own always-monthly section; the two are separate
  // queries over different windows and the fixture keeps them apart.
  sales_trend: { rows: TREND_ROWS, chart: TREND_CHART_SPEC, notes: [] },
  // The combo card's own section: a full financial year, whatever period the
  // reader picked. Sharing `sales_trend` is what put a bar per date under a
  // title promising months.
  monthly_performance: {
    rows: TREND_ROWS,
    chart: TREND_CHART_SPEC,
    notes: ['Twelve months of FY 2026-27, the financial year of the selected period.'],
  },
  // The region card now draws the same four series as the monthly one, off
  // its own chart spec.
  region_overview: { rows: [], chart: TREND_CHART_SPEC, notes: [] },
  // The two ranked cards under it, each the same measures grouped its own
  // way and ranked by what was sold.
  territory_sales: { rows: [], chart: TREND_CHART_SPEC, notes: [] },
  brand_sales: { rows: [], chart: TREND_CHART_SPEC, notes: [] },
  // Top 50 Customers, which replaced Top 15 Brands. Its year columns are read
  // from `chart.series` rather than written into the page, so the fixture has
  // to carry a spec — a table with no spec draws a customer name and a growth
  // and nothing between them, which is what this shape proves.
  top_customers: { rows: CUSTOMER_ROWS, chart: ACHIEVEMENT_CHART_SPEC, notes: [] },
};

function materialsPage(level: string, rows: Record<string, unknown>[]) {
  return {
    period: PERIOD,
    filters: {},
    level,
    levels: ['material', 'material_brand', 'material_group'],
    materials: { rows, notes: [] },
    top: rows,
    bottom: [],
    brand_detail: null,
  };
}

describe('Dashboard brand ranking', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // The frame names its cards; each is fetched separately, so the page's
    // own fan-out is exercised rather than stubbed away.
    vi.spyOn(services.dashboardService, 'get').mockResolvedValue({
      period: PERIOD,
      filters: {},
      kpis: KPIS,
      sections: Object.keys(SECTIONS),
    } as never);
    vi.spyOn(services.dashboardService, 'section').mockImplementation(
      (name: string) =>
        Promise.resolve({ period: PERIOD, filters: {}, name,
                          section: SECTIONS[name] } as never),
    );
    vi.spyOn(services.dashboardService, 'periodOptions').mockResolvedValue({
      options: [],
    } as never);
  });

  it('shows Top 50 Customers, and neither Top Brands nor Top Products', async () => {
    wrap(<Dashboard />);
    expect(await screen.findByText('Top 50 Customers')).toBeInTheDocument();
    expect(screen.queryByText('Top 15 Brands')).not.toBeInTheDocument();
    expect(screen.queryByText('Top Products')).not.toBeInTheDocument();
  });

  it('shows a growth that agrees with the columns beside it', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    // 50 L to 62.5 L is +25%, and the row states exactly that. A percentage
    // contradicting the figures it sits next to is the defect this card exists
    // to avoid, and the one that produced +96% on the region chart.
    const row = screen.getByText('M/S Shamim Enterprise').closest('tr');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('25%');
  });

  it('has no Sales Volume anywhere on the dashboard', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    // The KPI card went first; the brand table that still carried the column
    // heading has now gone too, so the phrase should not appear at all. Volume
    // is still reported wherever it can be broken down — the Sales page's own
    // volume section, and the brand card's bars, which are drawn rather than
    // written.
    expect(screen.queryByText('Sales Volume')).not.toBeInTheDocument();
  });

  it('colours the stock status cards on the label as well as the value', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    expect(screen.getByText('Unrestricted Stock (KG/LTR)')).toHaveClass(
      'stock-status--unrestricted',
    );
    expect(screen.getByText('1,25,500')).toHaveClass('stock-status--unrestricted');
    expect(screen.getByText('Expiring Soon (KG/LTR)')).toHaveClass('stock-status--expiring');
    expect(screen.getByText('289')).toHaveClass('stock-status--expiring');
    // Expired is its own status — red, a step past the amber warning.
    expect(screen.getByText('Expired Stock (KG/LTR)')).toHaveClass('stock-status--expired');
    expect(screen.getByText('13,354.8')).toHaveClass('stock-status--expired');
  });

  it('lays the customer table out in the order it was asked for', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    // Exact sequence, not a subset: the reading order across the row is the
    // requirement — the name, three years oldest first, then the growth between
    // the last two. The three year headings are the backend's own labels, so a
    // financial year is never spelled into the browser; and there is no target
    // column, because a table of what was sold is not a table of the plan.
    expect(headers).toEqual([
      'Customer', 'FY 2024-25', 'FY 2025-26', 'FY 2026-27', 'Growth',
    ]);
    expect(screen.getByText('M/S Shamim Enterprise')).toBeInTheDocument();
  });

  it('drops the quantity column', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).not.toContain('Quantity');
    expect(headers).not.toContain('Qty');
    // …and the plain "Volume" header is now "Sales Vol", so it does not linger.
    expect(headers).not.toContain('Volume');
  });

  it('never shows a unit column beside the volume figures', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    for (const forbidden of ['KG', 'LTR', 'MT', 'ML', 'Volume Unit', 'Unit']) {
      expect(headers).not.toContain(forbidden);
    }
    // No volume in this system carries a unit any more, so nothing can be
    // "mixed": revision 0022 removed the pack unit a target volume used to
    // inherit, and a sales volume never had one.
    expect(screen.queryByText('MIXED')).not.toBeInTheDocument();
  });

  it('renders a year with no sales as a dash rather than a zero or NaN', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    const row = screen.getByText('Master Traders').closest('tr');
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement)
      .getAllByRole('cell')
      .map((cell) => cell.textContent);
    // This customer traded in neither earlier year, so both columns and the
    // growth between them are unanswerable — and every one of them says so.
    // A zero would claim they bought nothing, which is a measurement nobody
    // made, and a growth against nothing is not −100%.
    expect(cells.filter((text) => text === '—').length).toBeGreaterThanOrEqual(3);
    for (const text of cells) {
      expect(text).not.toMatch(/NaN|Infinity/);
    }
  });

  it('no longer carries the Total Quantity card or the aging donut', async () => {
    wrap(<Dashboard />);
    await screen.findByText('Top 50 Customers');

    expect(screen.queryByText('Total Quantity')).not.toBeInTheDocument();
    expect(screen.queryByText('Outstanding Aging')).not.toBeInTheDocument();
  });
});

describe('Material Analysis', () => {
  const MATERIAL_ROW = {
    code: '1400000086',
    label: "Litosen 100ml (24's)",
    net_sales: 1_000,
  };

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.dashboardService, 'periodOptions').mockResolvedValue({
      options: [],
    } as never);
  });

  it('offers the Material Master’s three levels and no SKU or category', async () => {
    vi.spyOn(services.materialService, 'page').mockResolvedValue(
      materialsPage('material', [MATERIAL_ROW]) as never,
    );

    wrap(<MaterialsPage />);
    await screen.findAllByText("Litosen 100ml (24's)");

    const switcher = within(screen.getByRole('group', { name: 'Analysis level' }));
    for (const level of ['Material', 'Material Brand', 'Material Group']) {
      expect(switcher.getByRole('button', { name: level })).toBeInTheDocument();
    }
    // The two levels that named the removed SKU master are gone, not renamed.
    expect(switcher.queryByRole('button', { name: 'SKU' })).not.toBeInTheDocument();
    expect(switcher.queryByRole('button', { name: 'Category' })).not.toBeInTheDocument();
  });

  it('defaults to material level and asks the backend for it', async () => {
    const page = vi
      .spyOn(services.materialService, 'page')
      .mockResolvedValue(materialsPage('material', [MATERIAL_ROW]) as never);

    wrap(<MaterialsPage />);
    await screen.findAllByText("Litosen 100ml (24's)");

    expect(page).toHaveBeenCalledWith(expect.objectContaining({ level: 'material' }));
  });

  it('switches the backend query when brand level is chosen', async () => {
    const page = vi
      .spyOn(services.materialService, 'page')
      .mockResolvedValue(materialsPage('material', [MATERIAL_ROW]) as never);

    wrap(<MaterialsPage />);
    await screen.findAllByText("Litosen 100ml (24's)");

    const switcher = within(screen.getByRole('group', { name: 'Analysis level' }));
    fireEvent.click(switcher.getByRole('button', { name: 'Material Brand' }));

    await waitFor(() =>
      expect(page).toHaveBeenCalledWith(
        expect.objectContaining({ level: 'material_brand' }),
      ),
    );
  });

  it('reads the level from the URL, so a brand drill-down is a link', async () => {
    const page = vi
      .spyOn(services.materialService, 'page')
      .mockResolvedValue(materialsPage('material_brand', BRAND_ROWS) as never);

    wrap(<MaterialsPage />, '/materials?level=material_brand&material_brand=Alpha');
    await screen.findAllByText('Alpha');

    expect(page).toHaveBeenCalledWith(
      expect.objectContaining({ level: 'material_brand', material_brand: 'Alpha' }),
    );
  });
});
