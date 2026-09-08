/**
 * The footer's three refusals.
 *
 * Every report table in the platform is one `DataTable`, so each of these is a
 * rule that would otherwise go wrong on all of them at once — and each fails
 * *quietly*, producing a plausible number under a real heading rather than an
 * error anybody would notice.
 */

import { describe, expect, it } from 'vitest';
import { isSuppressed, sumColumn, totalsScope } from '../tables/totals';

describe('summing a column', () => {
  it('adds the values', () => {
    const rows = [{ net_sales: 100 }, { net_sales: 250 }, { net_sales: 0.5 }];
    expect(sumColumn(rows, 'net_sales')).toBe(350.5);
  });

  it('treats zero as a measurement, not as missing', () => {
    // A territory that sold nothing sold nothing. Reading 0 as absent would
    // suppress totals that are perfectly complete.
    const rows = [{ net_sales: 100 }, { net_sales: 0 }];
    expect(sumColumn(rows, 'net_sales')).toBe(100);
  });

  it('suppresses the total when a contributing cell is absent', () => {
    // The rule targetmgmt.country.totals sets: 350 under a heading claiming
    // three territories is short by whatever the third was worth, and nothing
    // on screen would say so.
    const rows = [{ net_sales: 100 }, { net_sales: 250 }, { net_sales: null }];
    const total = sumColumn(rows, 'net_sales');
    expect(isSuppressed(total)).toBe(true);
    expect(isSuppressed(total) && total.missing).toBe(1);
  });

  it('counts an unreadable value as absent rather than as zero', () => {
    const rows = [{ net_sales: 100 }, { net_sales: 'n/a' }];
    expect(isSuppressed(sumColumn(rows, 'net_sales'))).toBe(true);
  });

  it('suppresses on undefined and on empty string too', () => {
    expect(isSuppressed(sumColumn([{ a: 1 }, {}], 'a'))).toBe(true);
    expect(isSuppressed(sumColumn([{ a: 1 }, { a: '' }], 'a'))).toBe(true);
  });
});

describe('what the footer is a total of', () => {
  it('is the whole result set when every row is on screen', () => {
    expect(totalsScope({ rowsOnScreen: 12, rowCount: 12, hasServerTotals: false }))
      .toBe('all');
  });

  it('is this page when the server is holding rows back', () => {
    // 154 territories paged at 25: adding the 25 in the browser and labelling
    // it "Total" is the page subtotal that reads as a grand total.
    expect(totalsScope({ rowsOnScreen: 25, rowCount: 154, hasServerTotals: false }))
      .toBe('page');
  });

  it('is the whole result set when the backend supplied the figures', () => {
    expect(totalsScope({ rowsOnScreen: 25, rowCount: 154, hasServerTotals: true }))
      .toBe('all');
  });

  it('is the whole result set when there is no server count to compare against', () => {
    expect(totalsScope({ rowsOnScreen: 8, rowCount: undefined, hasServerTotals: false }))
      .toBe('all');
  });
});
