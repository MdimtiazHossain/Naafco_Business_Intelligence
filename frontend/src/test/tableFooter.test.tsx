/**
 * The totals row as the reader meets it.
 *
 * `tableTotals.test.ts` pins the arithmetic; this pins the two things that are
 * only wrong once rendered — a percentage silently acquiring a total, and a
 * footer whose figures have drifted out from under their headings because the
 * reader moved a column.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { DataTable, type Column } from '../tables/DataTable';

const ROWS = [
  { label: 'Dhaka', net_sales: 15_000_000, achievement_percent: 31.4 },
  { label: 'Chattogram', net_sales: 7_200_000, achievement_percent: 28.1 },
];

/** Net Sales totals; Achievement deliberately declares nothing. */
const COLUMNS: Column<(typeof ROWS)[number]>[] = [
  { key: 'label', header: 'Name' },
  { key: 'net_sales', header: 'Net Sales', total: 'sum' },
  { key: 'achievement_percent', header: 'Achievement' },
];

function show(
  props: {
    serverMode?: boolean;
    total?: number;
    page?: number;
    pageSize?: number;
    tableId?: string;
  } = {},
  columns: Column<(typeof ROWS)[number]>[] = COLUMNS,
) {
  return render(
    <I18nProvider>
      <DataTable rows={ROWS} columns={columns} {...props} />
    </I18nProvider>,
  );
}

function footerCells() {
  const footer = document.querySelector('tfoot');
  if (!footer) return null;
  return within(footer as HTMLElement)
    .getAllByRole('cell')
    .map((cell) => cell.textContent?.trim());
}

describe('the totals row', () => {
  it('totals a measure and leaves a percentage alone', () => {
    show();
    const cells = footerCells();
    expect(cells).not.toBeNull();
    // Net Sales is summed; Achievement gets an empty cell rather than 59.5,
    // which is what adding two percentages would produce.
    expect(cells?.[1]).toContain('2.22');
    expect(cells?.[2]).toBe('');
  });

  it('is not drawn at all when no column asks for one', () => {
    show({}, COLUMNS.map(({ total: _total, ...rest }) => rest));
    expect(document.querySelector('tfoot')).toBeNull();
  });

  it('says the figures are the whole result set when they are', () => {
    show();
    expect(screen.getByText('Total')).toBeInTheDocument();
  });

  it('says it is only this page when the server is holding rows back', () => {
    // 2 rows on screen out of 154: the number under Net Sales is a page
    // subtotal, and a footer labelled "Total" would read as a grand total.
    show({ serverMode: true, total: 154, page: 1, pageSize: 2 });
    expect(screen.getByText(/this page/)).toBeInTheDocument();
    expect(screen.queryByText('Total')).not.toBeInTheDocument();
  });

  it('suppresses the total rather than skipping an absent row', () => {
    render(
      <I18nProvider>
        <DataTable
          rows={[{ label: 'Dhaka', net_sales: 100 }, { label: 'Khulna', net_sales: null }]}
          columns={[
            { key: 'label', header: 'Name' },
            { key: 'net_sales', header: 'Net Sales', total: 'sum' },
          ] as never}
        />
      </I18nProvider>,
    );
    const cells = footerCells();
    expect(cells?.[1]).toBe('n/a');
  });

  it('recomputes rather than adds where the column says how', () => {
    // The only correct achievement total: summed actual over summed target.
    // Passing a function is how a ratio gets a footer at all.
    render(
      <I18nProvider>
        <DataTable
          rows={ROWS}
          columns={[
            { key: 'label', header: 'Name' },
            { key: 'net_sales', header: 'Net Sales', total: 'sum' },
            {
              key: 'achievement_percent',
              header: 'Achievement',
              total: () => '29.8%',
            },
          ] as never}
        />
      </I18nProvider>,
    );
    expect(footerCells()?.[2]).toBe('29.8%');
  });

  it('keeps the label somewhere when every visible column carries a figure', () => {
    // There is then no free cell in the footer. Dropping the label would leave
    // a row of figures with nothing saying whether they cover the result set or
    // one page — the exact misreading rule 3 exists to prevent — so it moves
    // below the table instead.
    render(
      <I18nProvider>
        <DataTable
          serverMode
          total={154}
          page={1}
          pageSize={2}
          rows={ROWS}
          columns={[
            { key: 'net_sales', header: 'Net Sales', total: 'sum' },
            { key: 'achievement_percent', header: 'Achievement', total: () => '29.8%' },
          ]}
        />
      </I18nProvider>,
    );
    expect(document.querySelector('tfoot')).not.toBeNull();
    expect(screen.getByText(/this page/)).toBeInTheDocument();
  });

  it('follows the reader when they move a column', () => {
    show({ tableId: 'test.footer-order' });
    // Move Net Sales to the front from the column panel; the total has to go
    // with it, or the figure ends up under "Name".
    fireEvent.click(screen.getByRole('button', { name: 'Columns' }));
    fireEvent.click(screen.getByRole('button', { name: 'Net Sales: Move left' }));

    const headers = screen
      .getAllByRole('columnheader')
      .map((cell) => cell.textContent?.trim() ?? '');
    const cells = footerCells() ?? [];
    const netSalesAt = headers.findIndex((header) => header.includes('Net Sales'));
    // It really moved — otherwise the assertion below would pass on a footer
    // that had ignored the arrangement entirely.
    expect(netSalesAt).toBe(0);
    expect(cells[netSalesAt]).toContain('2.22');
    // And the label followed it out of the first cell.
    expect(cells[1]).toContain('Total');
  });
});
