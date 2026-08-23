/**
 * The stock status colour standard, on the Material Stock page.
 *
 * One colour per status, applied to the *complete* metric — the label and the
 * figure under it. Green for unrestricted stock, the only stock that can be
 * sold; amber for stock running out of shelf life; red for stock that has
 * already lost it. The supporting position count stays neutral: it is metadata
 * about the metric rather than the metric.
 *
 * The assertions name the token class rather than a colour, because the class
 * *is* the standard — `index.css` decides what green and amber are, and a page
 * that spelled its own shade would satisfy a colour assertion while leaving the
 * standard.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import StockPage from '../pages/StockPage';
import * as services from '../services';

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/stock']}>
            <FilterProvider>{ui}</FilterProvider>
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const EMPTY = { rows: [], notes: [] };

const STOCK_PAGE = {
  summary: {
    rows: [],
    values: {
      unrestricted_stock: 125_500,
      quality_inspection_stock: 1_200,
      blocked_stock: 0,
      stock_in_transit: 400,
      total_stock: 127_100,
    },
    notes: [],
  },
  by_plant: EMPTY,
  by_storage_location: EMPTY,
  by_material: EMPTY,
  by_material_group: EMPTY,
  by_material_brand: EMPTY,
  expiry: {
    rows: [
      { code: 'EXPIRED', label: 'Expired', total_stock: 13_354.8, row_count: 16 },
      { code: 'EXPIRING_SOON', label: 'Expiring Soon', total_stock: 289, row_count: 4 },
      { code: 'VALID', label: 'Valid', total_stock: 113_456.2, row_count: 3_101 },
    ],
    notes: [],
  },
  expiring: EMPTY,
};

describe('Material Stock status colours', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(services.stockService, 'page').mockResolvedValue(STOCK_PAGE as never);
    vi.spyOn(services.masterDataService, 'options').mockResolvedValue({
      level: 'plant_code',
      parent_code: null,
      total: 0,
      truncated: false,
      options: [],
    } as never);
  });

  it('colours the unrestricted card green on both halves', async () => {
    wrap(<StockPage />);
    const label = await screen.findByText('Unrestricted Stock (KG/LTR)');
    expect(label).toHaveClass('stock-status--unrestricted');
    expect(screen.getByText('1,25,500')).toHaveClass('stock-status--unrestricted');
  });

  it('colours each shelf-life card by its own status, label and value alike', async () => {
    wrap(<StockPage />);

    // The card says "Expired Stock", the name this measure carries everywhere
    // else; the Shelf Life filter still offers the shorter "Expired".
    const expired = await screen.findByText('Expired Stock (KG/LTR)');
    expect(expired).toHaveClass('stock-status--expired');
    expect(screen.getByText('13,354.8')).toHaveClass('stock-status--expired');

    const soon = screen.getByText('Expiring Soon (KG/LTR)');
    expect(soon).toHaveClass('stock-status--expiring');
    expect(screen.getByText('289')).toHaveClass('stock-status--expiring');
  });

  it('leaves the position count and the unrelated cards neutral', async () => {
    wrap(<StockPage />);
    await screen.findByText('Expired Stock (KG/LTR)');

    // Secondary metadata under a coloured metric.
    expect(screen.getByText('16 position(s)').className).not.toContain('stock-status');
    // Total stock is not a status, and neither is quality inspection.
    expect(screen.getByText('Total Stock (KG/LTR)').className).not.toContain('stock-status');
    expect(
      screen.getByText('Stock in Quality Inspection (KG/LTR)').className,
    ).not.toContain('stock-status');
  });

  it('keeps the unit in the label and off every value', async () => {
    wrap(<StockPage />);
    await screen.findByText('Unrestricted Stock (KG/LTR)');
    expect(screen.getByText('1,25,500').textContent).toBe('1,25,500');
    expect(screen.queryByText(/^[\d,.]+\s*KG\/LTR$/)).not.toBeInTheDocument();
  });
});
