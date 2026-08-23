/**
 * The filter bar works in both directions.
 *
 * A parent narrows what its children offer, and a child selects the parents its
 * master data implies. The second half is the new one: choosing a customer sets
 * the nine organisational levels above it, choosing a material sets its brand
 * and group, choosing a storage location sets its plant.
 *
 * Two properties matter more than the mechanics and are pinned here: the whole
 * chain costs **one** request and lands in **one** state update, and resolution
 * is only ever started by a user changing a control — so it cannot re-trigger
 * itself.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider, STOCK_FILTERS } from '../contexts/FilterContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import * as services from '../services';

/** The Material Stock bar, which is the only one drawing the material chain. */
const STOCK_BAR = (
  <GlobalFilterBar
    levels={[]}
    showIndependent={false}
    pageFilters={STOCK_FILTERS}
    showDate={false}
  />
);

/** The chain `dim_customer.sub_territory_code` implies, as the server returns it. */
const CUSTOMER_CHAIN = {
  sub_territory_code: 'ST-001',
  territory_code: 'T-005',
  unit_code: 'U-001',
  area_code: 'A-002',
  region_code: 'R-001',
  zone_code: 'Z-001',
  sales_line_code: 'SL-001',
  bu_code: 'BU-001',
  company_code: '1000',
};

const CHAINS: Record<string, Record<string, string>> = {
  customer_code: CUSTOMER_CHAIN,
  material_code: { material_brand: 'BR-05', material_group_code: 'MG-01' },
  storage_location_key: { plant_code: 'P-001', company_code: '1000' },
  // A brand that sits under two material groups implies no group at all.
  material_brand: {},
};

function wrap(route: string, bar = <GlobalFilterBar />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <FilterProvider>{bar}</FilterProvider>
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/**
 * Pick a value in one filter control and wait for the chips to settle.
 *
 * The control is a searchable dropdown, not a native `<select>`: open it, type
 * the code into its search box — which matches on code as well as label — and
 * click the option that survives. Driving it the way a user does is also what
 * keeps these tests honest about the search feature not breaking the cascade.
 */
async function choose(label: string, value: string) {
  const toggle = await screen.findByRole('button', { name: /Filters/ });
  fireEvent.click(toggle);
  const trigger = await screen.findByLabelText(label);
  // Disabled until its options arrive, and a click on a disabled button does
  // nothing at all.
  await waitFor(() => expect(trigger).not.toBeDisabled());
  fireEvent.click(trigger);

  const list = await screen.findByRole('listbox');
  fireEvent.change(within(list).getByLabelText('Search…'), {
    target: { value },
  });
  const options = await within(list).findAllByRole('option');
  fireEvent.click(options[0]);

  await waitFor(() => expect(services.masterDataService.ancestors).toHaveBeenCalled());
  fireEvent.click(toggle);
  await waitFor(() =>
    expect(screen.queryByLabelText(label)).not.toBeInTheDocument(),
  );
}

describe('Bidirectional hierarchical filters', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.masterDataService, 'options').mockImplementation(
      (level: string) =>
        Promise.resolve({
          level,
          parent_code: null,
          total: 1,
          truncated: false,
          options: [{ code: 'C-001', label: 'ABC Customer', parent_code: null }],
        }) as never,
    );
    vi.spyOn(services.masterDataService, 'ancestors').mockImplementation(
      (level: string, code: string | string[]) =>
        Promise.resolve({
          level,
          codes: Array.isArray(code) ? code : [code],
          source: null,
          ancestor: CHAINS[level] ?? {},
          ancestors: {},
        }) as never,
    );
  });

  it('selects the whole parent chain when a customer is chosen', async () => {
    wrap('/');
    await choose('Customer', 'C-001');

    // Every level the master data states, set from one selection.
    for (const [level, code] of Object.entries(CUSTOMER_CHAIN)) {
      expect(
        screen.getByText(new RegExp(`: ${code.replace(/[-|]/g, '\\$&')}$`)),
      ).toBeInTheDocument();
      expect(level).toBeTruthy();
    }
    expect(screen.getByText(/Customer: C-001/)).toBeInTheDocument();
  });

  it('resolves the chain in one request and one state update', async () => {
    wrap('/');
    await choose('Customer', 'C-001');

    // Nine parents, one call. Walking the hierarchy a level at a time would
    // cost nine, and nine renders with it.
    expect(services.masterDataService.ancestors).toHaveBeenCalledTimes(1);
    expect(services.masterDataService.ancestors).toHaveBeenCalledWith(
      'customer_code',
      'C-001',
    );
  });

  it('marks an auto-selected parent and explains it', async () => {
    wrap('/');
    await choose('Customer', 'C-001');

    const territory = screen.getByText(/Territory: T-005/).closest('button');
    expect(territory).toHaveAttribute(
      'title',
      'Automatically selected from the item you chose below it.',
    );
    // The child the user actually picked is not marked — it was not derived.
    expect(screen.getByText(/Customer: C-001/).closest('button')).not.toHaveAttribute(
      'title',
    );
  });

  it('lets a user remove an auto-selected parent without it coming back', async () => {
    wrap('/');
    await choose('Customer', 'C-001');

    fireEvent.click(screen.getByText(/Territory: T-005/).closest('button')!);

    // It stays gone, and the customer that implied it stays chosen. Filters are
    // ANDed server-side and the customer already implies the territory, so the
    // report is unchanged either way — but forcing the parent back would make
    // it impossible to take off.
    await waitFor(() =>
      expect(screen.queryByText(/Territory: T-005/)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/Customer: C-001/)).toBeInTheDocument();
    expect(services.masterDataService.ancestors).toHaveBeenCalledTimes(1);
  });

  it('selects brand and group when a material is chosen', async () => {
    wrap('/stock', STOCK_BAR);
    await choose('Material', 'C-001');

    expect(screen.getByText(/Material Brand: BR-05/)).toBeInTheDocument();
    expect(screen.getByText(/Material Group: MG-01/)).toBeInTheDocument();
  });

  it('selects nothing when the master data does not determine a parent', async () => {
    wrap('/stock', STOCK_BAR);
    await choose('Material Brand', 'C-001');

    // A brand can appear under more than one material group, so a brand alone
    // does not determine one. Choosing a plausible group would silently narrow
    // the report to the wrong slice.
    expect(screen.queryByText(/Material Group: /)).not.toBeInTheDocument();
    expect(screen.getByText(/Material Brand: C-001/)).toBeInTheDocument();
  });

  it('clears the whole resolved chain on reset', async () => {
    wrap('/');
    await choose('Customer', 'C-001');

    fireEvent.click(screen.getByRole('button', { name: /Clear all/ }));

    await waitFor(() =>
      expect(screen.queryByText(/Customer: C-001/)).not.toBeInTheDocument(),
    );
    // No auto-selected value outlives the selection that produced it.
    for (const code of Object.values(CUSTOMER_CHAIN)) {
      expect(screen.queryByText(new RegExp(`: ${code}$`))).not.toBeInTheDocument();
    }
  });

  it('asks the options endpoint which level a parent belongs to', async () => {
    // A customer's nearest selected ancestor may be a sub-territory or a zone,
    // and the code alone does not say which — so the level travels with it.
    wrap('/?zone_code=Z-001');
    fireEvent.click(await screen.findByRole('button', { name: /Filters/ }));
    await screen.findByLabelText('Customer');

    await waitFor(() =>
      expect(services.masterDataService.options).toHaveBeenCalledWith(
        'customer_code',
        'Z-001',
        undefined,
        'zone_code',
      ),
    );
  });
});
