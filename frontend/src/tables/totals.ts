/**
 * The footer figures for a report table, and the three ways a total goes wrong.
 *
 * `DataTable` draws every report table in this platform, so a footer added here
 * appears on all of them — which is the reason the rules below are enforced in
 * one tested place rather than trusted to each caller. A wrong total is the
 * most expensive kind of wrong number: it is the figure somebody quotes, and
 * unlike a wrong row it carries the authority of a summary.
 *
 * **A percentage is not a sum.** Achievement, growth and share are ratios, and
 * adding them produces a number with no meaning that still renders neatly at
 * the bottom of a column. So a column totals only when it says how — `'sum'`
 * for an additive measure, or a function for a ratio that must be *recomputed*
 * from the underlying totals (achievement is total actual ÷ total target, never
 * the mean of the column). A column that says nothing gets an empty cell, which
 * is the honest answer for a code, a name or a date.
 *
 * **A suppressed figure must not become zero.** This is the rule
 * `targetmgmt.country.totals` already sets on the server: where a contributing
 * cell is absent, the total is withheld and what stopped it is named, because a
 * sum that silently skipped the rows it could not read is short by an unknown
 * amount. `null` and `undefined` are absent; `0` is a measurement and is added.
 *
 * **Say what is being totalled.** These tables page on the server, so the rows
 * in the browser are usually not the result set. Which of the two a footer
 * describes is decided by :func:`totalsScope` and always stated in the label —
 * a page subtotal that reads as a grand total is the failure this is here to
 * prevent.
 *
 * **An export does not carry the totals row, and that is a decision.** An export
 * is the backend's rows in the backend's column order — the frontend never
 * recomputes a figure for one, which is what keeps a downloaded file and the
 * screen the same numbers. A totals row appended to a CSV is not a total to
 * anything downstream: it is a data row with a label where a territory code
 * should be, and it re-imports, pivots and sums as one. Anyone who wants a
 * total in a spreadsheet has `SUM()` there and the rows to point it at. The day
 * an export is meant to carry one it belongs in the writer next to the header,
 * where it can be a real footer, not here.
 */

import type { ReactNode } from 'react';

/** What the footer is a total *of*. Always rendered into the label. */
export type TotalsScope = 'all' | 'page';

/**
 * How one column's footer cell is produced.
 *
 * `'sum'` adds the column's numeric values. A function receives the rows and
 * returns whatever the column means — the escape hatch a ratio needs, since the
 * only correct achievement total is recomputed from the summed parts. Absent
 * means no footer cell at all.
 */
export type ColumnTotal<T> = 'sum' | ((rows: T[]) => ReactNode);

/** A total that could not be produced, and the reason to show on hover. */
export interface SuppressedTotal {
  suppressed: true;
  /** How many rows carried no value for this column. */
  missing: number;
  /**
   * Why there is no figure. The two are different messages to a reader: rows
   * that could not be read are a data problem they can go and fix, while a zero
   * denominator is the platform's standing rule that a ratio with nothing
   * underneath it renders `n/a` rather than `0%`.
   */
  reason: 'missing' | 'zeroDenominator';
}

export type TotalValue = number | SuppressedTotal;

export function isSuppressed(value: unknown): value is SuppressedTotal {
  return typeof value === 'object' && value !== null && 'suppressed' in value;
}

/**
 * Add one column, or refuse to.
 *
 * Refusing is the whole point. A column of twelve months where two are absent
 * sums to ten months of trading under a heading that claims twelve, and nothing
 * on screen would say so. `0` is not absent — a month that sold nothing is a
 * measurement, and treating it as missing would suppress totals that are fine.
 */
export function sumColumn<T extends Record<string, any>>(
  rows: T[],
  key: string,
): TotalValue {
  let total = 0;
  let missing = 0;
  for (const row of rows) {
    const raw = row[key];
    if (raw === null || raw === undefined || raw === '') {
      missing += 1;
      continue;
    }
    const value = typeof raw === 'number' ? raw : Number(raw);
    // A value that is present and unreadable is *not* a zero either. It counts
    // as missing, so the column is suppressed rather than quietly understated.
    if (!Number.isFinite(value)) {
      missing += 1;
      continue;
    }
    total += value;
  }
  if (missing > 0) return { suppressed: true, missing, reason: 'missing' };
  return total;
}

/**
 * A ratio rebuilt from two totals — the only correct total for a percentage.
 *
 * Achievement is the summed actual over the summed target, and growth is the
 * summed current over the summed previous. Averaging the column instead gives
 * every row equal weight, so a territory that sold two hundred taka against a
 * target of one hundred drags the whole column up as hard as one that sold two
 * crore — a different number, equally plausible on screen, and wrong.
 *
 * It inherits both suppressions. If either side withheld its total the ratio
 * cannot be honest either, and a zero denominator is `n/a` rather than `0%`,
 * which is the same rule the backend applies to every percentage it reports.
 */
export function percentOfTotals(
  numerator: TotalValue,
  denominator: TotalValue,
): TotalValue {
  if (isSuppressed(numerator) || isSuppressed(denominator)) {
    const missing =
      (isSuppressed(numerator) ? numerator.missing : 0) +
      (isSuppressed(denominator) ? denominator.missing : 0);
    return { suppressed: true, missing, reason: 'missing' };
  }
  if (denominator === 0) {
    return { suppressed: true, missing: 0, reason: 'zeroDenominator' };
  }
  return (numerator / denominator) * 100;
}

/**
 * Sum a column the way the backend sums it: skipping the rows it excludes.
 *
 * Credit exposure is the case. `etl.credit` keeps an over-adjusted invoice at
 * its negative figure rather than flooring it — the negative is a data-quality
 * signal carrying `BALANCE_NEGATIVE` — and the exposure total leaves those rows
 * out, so a credit that somebody posted twice cannot net off against real debt
 * and understate what is owed. A footer that added the visible column instead
 * would sit directly beneath the KPI card and disagree with it, and the reader
 * would have no way to tell which of the two was lying.
 *
 * The predicate is the caller's because the exclusion is the *backend's* rule,
 * not a property of numbers: spelling it at the call site keeps it beside a
 * comment naming the rule it mirrors.
 */
export function sumWhere<T extends Record<string, any>>(
  rows: T[],
  key: string,
  include: (row: T) => boolean,
): TotalValue {
  return sumColumn(rows.filter(include), key);
}

/**
 * The numerator a growth total needs: the change, not the current figure.
 *
 * Growth is (current − previous) ÷ previous, so the total is the summed change
 * over the summed previous — and the change has to be summed from the same rows
 * the denominator came from, or a row present on one side and absent on the
 * other would quietly shift the answer. Both sides suppress together, which is
 * why this returns a `TotalValue` rather than a number.
 */
export function growthNumerator<T extends Record<string, any>>(
  rows: T[],
  currentKey = 'net_sales',
  previousKey = 'previous_net_sales',
): TotalValue {
  const current = sumColumn(rows, currentKey);
  const previous = sumColumn(rows, previousKey);
  if (isSuppressed(current) || isSuppressed(previous)) {
    return {
      suppressed: true,
      missing:
        (isSuppressed(current) ? current.missing : 0) +
        (isSuppressed(previous) ? previous.missing : 0),
      reason: 'missing',
    };
  }
  return current - previous;
}

/**
 * Whether the footer describes the result set or only the rows on screen.
 *
 * `serverTotals` is a grand total the backend computed, and it always describes
 * everything. Otherwise the browser can only add what it holds: that is the
 * result set when every row is present, and this page when it is not. There is
 * deliberately no third answer — a table that cannot tell which it is has no
 * business drawing a footer.
 */
export function totalsScope(args: {
  rowsOnScreen: number;
  rowCount: number | undefined;
  hasServerTotals: boolean;
}): TotalsScope {
  if (args.hasServerTotals) return 'all';
  if (args.rowCount === undefined) return 'all';
  return args.rowsOnScreen >= args.rowCount ? 'all' : 'page';
}
