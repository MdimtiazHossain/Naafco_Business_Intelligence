/**
 * The Material Stock page offers its own filters, and only its own.
 *
 * `vw_material_stock_detail` carries a company, a plant, a storage location and
 * the Material Master's classification of the material. It carries no region,
 * no customer, no sales force, no batch and no date. The backend skips a filter
 * whose column a view lacks, so the sales filters this page used to draw could
 * never narrow anything — they simply sat above unfiltered totals, with a chip
 * claiming otherwise.
 *
 * The three material levels are no longer what makes this set distinctive:
 * revision 0022 made them global, so a sales page draws them too. What is still
 * stock-only is the plant chain and the shelf-life bucket.
 *
 * These tests pin that from the browser's side: which controls exist, which
 * chips can appear, what reaches the service, and that the two cascades clear
 * their own descendants.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import StockPage from '../pages/StockPage';
import * as services from '../services';

function wrap(ui: React.ReactNode, route = '/stock') {
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

const EMPTY = { rows: [], notes: [] };

const STOCK_PAGE = {
  summary: { rows: [], values: { total_stock: 900 }, notes: [] },
  by_plant: EMPTY,
  by_storage_location: EMPTY,
  by_material: EMPTY,
  by_material_group: EMPTY,
  by_material_brand: EMPTY,
  expiry: EMPTY,
  expiring: EMPTY,
};

/** Every filter the page must offer, by its visible label. */
const OFFERED = [
  'Company', 'Plant', 'Storage Location',
  'Material Group', 'Material Brand', 'Material', 'Shelf Life',
];

/**
 * Filters that belong to other datasets and must not appear here.
 *
 * `Product`, `Brand` and `Category` are gone from the application entirely
 * since revision 0022, so they are not listed: an assertion that a label which
 * exists nowhere is absent here passes for the wrong reason and would keep
 * passing if the stock bar started drawing a customer filter.
 */
const WITHHELD = [
  'Region', 'Area', 'Zone', 'Territory', 'Sub-Territory', 'Unit',
  'Business Unit', 'Sales Line', 'Customer', 'Sales Force', 'Batch',
];

describe('Material Stock filters', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.stockService, 'page').mockResolvedValue(STOCK_PAGE as never);
    // Two options per level, so a test can actually pick one — a <select>
    // ignores a value that is not among its options.
    vi.spyOn(services.masterDataService, 'options').mockImplementation(
      (level: string) =>
        Promise.resolve({
          level,
          parent_code: null,
          total: 2,
          truncated: false,
          options: [
            { code: `${level}-1`, label: `${level} one`, parent_code: null },
            { code: `${level}-2`, label: `${level} two`, parent_code: null },
          ],
        }) as never,
    );
    vi.spyOn(services.masterDataService, 'ancestors').mockResolvedValue({
      level: 'plant_code',
      codes: ['plant_code-1'],
      source: null,
      ancestor: {},
      ancestors: {},
    } as never);
  });

  async function openFilters() {
    wrap(<StockPage />);
    fireEvent.click(await screen.findByRole('button', { name: /Filters/ }));
  }

  it('offers exactly the filters the stock view can honour', async () => {
    await openFilters();
    for (const label of OFFERED) {
      expect(await screen.findByLabelText(label)).toBeInTheDocument();
    }
  });

  it('offers no filter belonging to another dataset', async () => {
    await openFilters();
    await screen.findByLabelText('Plant');
    for (const label of WITHHELD) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
  });

  /**
   * Company is the head of both cascades, so it is drawn in the bar's own row
   * rather than in the panel — visible without opening anything, and visible
   * exactly once when everything is open.
   */
  it('shows Company without the filter panel being opened', async () => {
    wrap(<StockPage />);
    expect(await screen.findByLabelText('Company')).toBeInTheDocument();
    // The panel is shut: a filter that lives inside it is not on screen yet.
    expect(screen.queryByLabelText('Plant')).not.toBeInTheDocument();
  });

  it('draws Company once when the panel is open', async () => {
    await openFilters();
    await screen.findByLabelText('Plant');
    // Two controls would mean the global row and the grid had both claimed it,
    // which is the duplicate the layout exists to prevent.
    expect(screen.getAllByLabelText('Company')).toHaveLength(1);
  });

  it('shows no period selector, because stock has no posting date', async () => {
    await openFilters();
    await screen.findByLabelText('Plant');
    expect(screen.queryByText('This Month')).not.toBeInTheDocument();
    expect(screen.queryByText('Period')).not.toBeInTheDocument();
  });

  it('sends only stock filters, ignoring one left in the URL by another page', async () => {
    wrap(<StockPage />, '/stock?region_code=R01&customer_code=C001&plant_code=PL01');
    await waitFor(() => expect(services.stockService.page).toHaveBeenCalled());

    const sent = vi.mocked(services.stockService.page).mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(sent.plant_code).toBe('PL01');
    // A filter this view cannot honour never leaves the browser, so it cannot
    // look applied. No period either — the page does not offer one.
    expect(sent.region_code).toBeUndefined();
    expect(sent.customer_code).toBeUndefined();
    expect(sent.period).toBeUndefined();
  });

  it('shows no chip for a filter it does not manage', async () => {
    wrap(<StockPage />, '/stock?region_code=R01&plant_code=PL01');
    await screen.findByRole('button', { name: /Filters/ });
    // The chip is the claim "these numbers are narrowed by this". A region
    // never narrows stock, so it must not make that claim here.
    expect(screen.queryByText(/Region: R01/)).not.toBeInTheDocument();
    expect(await screen.findByText(/Plant: PL01/)).toBeInTheDocument();
  });

  /**
   * Change one filter and read the resulting chips.
   *
   * The bar swaps chips for controls while it is open, so the panel is closed
   * again before anything is asserted — otherwise "the chip is gone" would be
   * true merely because no chip is drawn at all.
   */
  async function changeAndReadChips(level: string, value: string) {
    const toggle = await screen.findByRole('button', { name: /Filters/ });
    fireEvent.click(toggle);

    // A searchable dropdown, not a native select: open it, then either take the
    // "All" row (which is what clearing means here) or search for the code and
    // click the option that matches.
    const trigger = await screen.findByLabelText(level);
    await waitFor(() => expect(trigger).not.toBeDisabled());
    fireEvent.click(trigger);
    const list = await screen.findByRole('listbox');
    if (value === '') {
      fireEvent.click(within(list).getByText('All'));
    } else {
      fireEvent.change(within(list).getByLabelText('Search…'), {
        target: { value },
      });
      const options = await within(list).findAllByRole('option');
      fireEvent.click(options[0]);
    }

    fireEvent.click(toggle);
    // `aria-expanded`, not "the control has gone": Company is drawn in the
    // bar's always-visible row, so its control outlives the panel and only the
    // grid ones disappear. What this needs to know is that the panel is shut
    // and the chip row is therefore being drawn at all.
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'false'));
  }

  it('clears a plant and its storage location when the company changes', async () => {
    wrap(
      <StockPage />,
      '/stock?company_code=1000&plant_code=PL01&storage_location_key=PL01%7CFG01',
    );
    // Choosing a *different* company invalidates the plant beneath it and the
    // storage location beneath that — one chain, cleared from the change down.
    await changeAndReadChips('Company', 'company_code-2');

    expect(screen.queryByText(/Storage Location: /)).not.toBeInTheDocument();
    expect(screen.queryByText(/Plant: /)).not.toBeInTheDocument();
    expect(screen.getByText(/Company: company_code-2/)).toBeInTheDocument();
  });

  it('keeps the children when a parent is cleared rather than changed', async () => {
    wrap(
      <StockPage />,
      '/stock?company_code=1000&plant_code=PL01&storage_location_key=PL01%7CFG01',
    );
    await changeAndReadChips('Company', '');

    // Removing a parent is not the same as pointing it somewhere else. The
    // plant is still a valid narrowing on its own, and filters are ANDed
    // server-side, so dropping the company above it changes no number. This is
    // what lets a user take off a parent that was auto-selected from a child
    // without losing the child they actually chose.
    expect(screen.queryByText(/Company: /)).not.toBeInTheDocument();
    expect(screen.getByText(/Plant: PL01/)).toBeInTheDocument();
    expect(screen.getByText(/Storage Location: PL01\|FG01/)).toBeInTheDocument();
  });

  it('leaves the material cascade alone when the plant cascade changes', async () => {
    wrap(<StockPage />, '/stock?plant_code=PL01&material_group_code=MG01');
    await changeAndReadChips('Plant', '');

    // Where a position is held and what it is are independent: no material
    // belongs to a plant, so clearing one chain must not disturb the other.
    expect(screen.queryByText(/Plant: PL01/)).not.toBeInTheDocument();
    expect(screen.getByText(/Material Group: MG01/)).toBeInTheDocument();
  });
});
