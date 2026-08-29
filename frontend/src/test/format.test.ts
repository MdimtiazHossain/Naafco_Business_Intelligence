/** Formatting rules: Bangladeshi currency, percentages and missing values. */

import { describe, expect, it } from 'vitest';
import {
  formatAmount,
  formatByKind,
  formatBytes,
  formatCell,
  formatDays,
  formatPercent,
  formatQuantity,
  formatStock,
  groupIndian,
  growthClass,
  humanizeColumn,
  severityClass,
  stockLabel,
} from '../utils/format';

describe('Indian digit grouping', () => {
  it.each([
    [1000, '1,000'],
    [100000, '1,00,000'],
    [18700000, '1,87,00,000'],
    [-52400, '-52,400'],
    [0, '0'],
  ])('groups %s as %s', (input, expected) => {
    expect(groupIndian(input)).toBe(expected);
  });
});

describe('currency', () => {
  it.each([
    [18_700_000, '৳1.87 Cr'],
    [5_240_000, '৳52.40 L'],
    [52_400, '৳52.4 K'],
    [940, '৳940'],
  ])('formats %s as %s', (input, expected) => {
    expect(formatAmount(input)).toBe(expected);
  });

  it('shows an em dash for a missing value rather than zero', () => {
    expect(formatAmount(null)).toBe('—');
    expect(formatAmount(undefined)).toBe('—');
  });

  it('can render the exact value', () => {
    expect(formatAmount(18_700_000, false)).toBe('৳1,87,00,000');
  });
});

describe('percentages', () => {
  it('renders one decimal place', () => {
    expect(formatPercent(89.44)).toBe('89.4%');
  });

  it('signs growth', () => {
    expect(formatPercent(8.4, { signed: true })).toBe('+8.4%');
    expect(formatPercent(-8.4, { signed: true })).toBe('-8.4%');
  });

  it('never renders a missing ratio as 0%', () => {
    expect(formatPercent(null)).toBe('n/a');
    expect(formatPercent(undefined)).toBe('n/a');
  });
});

describe('formatByKind', () => {
  it('respects the semantic type the backend declared', () => {
    expect(formatByKind(1_500_000, 'currency')).toBe('৳15.00 L');
    expect(formatByKind(89.4, 'percent')).toBe('89.4%');
    expect(formatByKind(12, 'count')).toBe('12');
    expect(formatByKind(12.5, 'quantity')).toBe('12.5');
  });
});

describe('formatCell', () => {
  it('picks a formatter from the column name', () => {
    expect(formatCell('net_sales', 1_500_000)).toBe('৳15.00 L');
    expect(formatCell('achievement_percent', 60)).toBe('60.0%');
    expect(formatCell('growth_percent', 8.4)).toBe('+8.4%');
    expect(formatCell('quantity', 120)).toBe('120');
    expect(formatCell('stock_coverage_days', 3)).toBe('3.0 d');
    expect(formatCell('days_overdue', 61)).toBe('61 d');
  });

  it('renders dates and missing values readably', () => {
    expect(formatCell('full_date', '2026-08-15')).toBe('15 Aug 2026');
    expect(formatCell('net_sales', null)).toBe('—');
  });

  it('leaves codes untouched', () => {
    expect(formatCell('material_code', '1400000086')).toBe('1400000086');
  });

  // Both of these fell through to the currency formatter when the brand table
  // was added, so a rank rendered as "৳1" and a volume as an amount of money.
  it('renders a rank as a plain ordinal, never as currency', () => {
    expect(formatCell('rank', 1)).toBe('1');
    expect(formatCell('rank', 15)).toBe('15');
  });

  it('renders volume as a measured quantity, never as currency', () => {
    expect(formatCell('volume', 50_000)).toBe('50,000');
    // A line whose source stated no volume has no figure, and says so.
    expect(formatCell('volume', null)).toBe('—');
  });
});

// Material stock names its unit once, on the label. A figure that carried
// "KG/LTR" after the number would read as a converted measurement rather than
// as the value the upload stated, and a column of them would not line up.
describe('material stock is a labelled unit and a bare figure', () => {
  it('formats the value with no unit attached', () => {
    expect(formatStock(12_500)).toBe('12,500');
    expect(formatCell('unrestricted_stock', 12_500)).toBe('12,500');
    // Grouped the Bangladeshi way, like every other figure on the platform —
    // 1,25,500 rather than 125,500.
    expect(formatByKind(125_500, 'stock')).toBe('1,25,500');
  });

  it('keeps a decimal the upload stated rather than rounding it away', () => {
    expect(formatStock(1_250.5)).toBe('1,250.5');
  });

  it('shows an em dash for a missing figure, never a zero', () => {
    expect(formatStock(null)).toBe('—');
  });

  it('puts the unit on the label instead', () => {
    expect(stockLabel('Unrestricted Stock')).toBe('Unrestricted Stock (KG/LTR)');
  });

  it('gives a derived stock column heading its unit', () => {
    expect(humanizeColumn('unrestricted_stock')).toBe('Unrestricted Stock (KG/LTR)');
    expect(humanizeColumn('stock_in_transit')).toBe('Stock in Transit (KG/LTR)');
    expect(humanizeColumn('total_stock')).toBe('Total Stock (KG/LTR)');
  });

  it('leaves columns that are not a stock figure without one', () => {
    // Provenance and duration are not measured in KG/LTR, and a target volume
    // is a planned figure rather than a position in a storage location — since
    // revision 0022 it carries no unit at all.
    expect(humanizeColumn('stock_source')).toBe('Stock Source');
    expect(humanizeColumn('stock_coverage_days')).toBe('Stock Coverage Days');
    expect(humanizeColumn('target_volume')).toBe('Target Volume');
  });
});

describe('helpers', () => {
  it('humanizes column names', () => {
    expect(humanizeColumn('net_sales')).toBe('Net Sales');
    expect(humanizeColumn('material_code')).toBe('Material Code');
    expect(humanizeColumn('bu_code')).toBe('BU Code');
  });

  it('colours growth by direction and stays neutral when unknown', () => {
    expect(growthClass(5)).toContain('emerald');
    expect(growthClass(-5)).toContain('red');
    expect(growthClass(null)).toContain('slate');
  });

  it('maps severities to distinct styles', () => {
    expect(severityClass('CRITICAL')).toContain('red');
    expect(severityClass('HIGH')).toContain('orange');
    expect(severityClass('UNKNOWN')).toContain('slate');
  });

  it('formats days and quantities', () => {
    expect(formatDays(3)).toBe('3.0 d');
    expect(formatDays(null)).toBe('n/a');
    expect(formatQuantity(1234)).toBe('1,234');
  });
});


describe('file sizes', () => {
  it('keeps the decimal that distinguishes a file from its limit', () => {
    // The number this sits beside is a ceiling: rounding 55.3 to 55 loses the
    // very thing the reader is comparing.
    expect(formatBytes(58_023_712)).toBe('55.3 MB');
  });

  it('renders a round limit as a round number', () => {
    expect(formatBytes(25 * 1024 * 1024)).toBe('25 MB');
  });

  it('drops to smaller units rather than reporting 0 MB', () => {
    expect(formatBytes(4096)).toBe('4 KB');
    expect(formatBytes(200)).toBe('200 bytes');
  });

  it('says n/a for a size it cannot state', () => {
    expect(formatBytes(Number.NaN)).toBe('n/a');
    expect(formatBytes(-1)).toBe('n/a');
  });
});
