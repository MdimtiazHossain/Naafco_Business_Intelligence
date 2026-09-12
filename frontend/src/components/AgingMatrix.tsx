/**
 * The Aging × Level matrix: where the debt is, and how old it is, at once.
 *
 * The aging chart says how late the book is and the exposure breakdown says
 * which region carries it. Neither answers the question a credit committee
 * actually asks — *which region's debt is the old debt* — because that is a
 * two-dimensional question and a bar chart has one dimension to spend.
 *
 * **Shaded in a single hue, on purpose.** `AGING_COLORS` already spends hue on
 * severity: green inside terms through to near-black red at a year. If the cells
 * of this table were coloured on that scale, every column would be a flat block
 * of its bucket's colour and the shading would carry no information at all —
 * worse, a large figure in the green column and a small one in the red column
 * would look like the green one mattered less, which is backwards. So hue is
 * dropped entirely here and **intensity alone** carries magnitude, read within
 * each row: a reader scans across a region and sees where its money sits. The
 * column headers keep the bucket colours, so the severity scale is still on
 * screen — stated once, in the one place it means something.
 *
 * Relative to the **row**, not to the whole table. A table-wide scale would make
 * every cell of every small region pale and unreadable, and the question is not
 * "which is the biggest number on screen" — the ranking down the left already
 * answers that — but "how is *this* region's debt distributed".
 */

import { AGING_COLORS } from '../charts/Charts';
import { useT } from '../contexts/I18nContext';
import { formatAmount } from '../utils/format';
import type { CreditAgingMatrix } from '../types/api';

/**
 * The one hue, as an RGB triple so opacity can be mixed per cell.
 *
 * Slate rather than a brand colour: this table sits under charts that are
 * already carrying meaning in colour, and a second coloured surface competes
 * with them. A neutral ramp reads as weight rather than as category.
 */
const RAMP = '15, 23, 42';

/** Below this share of a row's total a cell is left unshaded. */
const FLOOR = 0.02;

export function AgingMatrix({
  matrix,
  levelLabel,
  emptyMessage,
}: {
  matrix: CreditAgingMatrix | undefined;
  levelLabel: string;
  emptyMessage: string;
}) {
  const t = useT();
  const rows = matrix?.rows ?? [];
  if (!matrix || !rows.length) {
    return <p className="py-6 text-center text-sm text-slate-500">{emptyMessage}</p>;
  }

  // Column totals, so the foot can carry them. Computed from the same cells the
  // body draws rather than fetched separately — a footer that disagreed with the
  // column above it would be the one thing nobody would think to check.
  const totals = matrix.buckets.map((_bucket, index) =>
    rows.reduce((sum, row) => sum + (row.amounts[index] ?? 0), 0),
  );
  const grandTotal = rows.reduce((sum, row) => sum + row.outstanding_amount, 0);

  return (
    // Wide content scrolls inside its own container: nine columns of currency
    // do not fit a phone, and the page body must never scroll sideways.
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] text-xs">
        <thead>
          <tr className="border-b border-slate-200 dark:border-slate-700">
            <th className="px-2 py-2 text-left font-medium text-slate-500">
              {levelLabel}
            </th>
            {matrix.buckets.map((bucket) => (
              <th key={bucket} className="px-2 py-2 text-right font-medium">
                <span className="inline-flex items-center gap-1">
                  {/* The severity scale, kept on screen while the cells below
                      carry magnitude instead. */}
                  <span
                    aria-hidden="true"
                    className="inline-block h-2 w-2 rounded-sm"
                    style={{ background: AGING_COLORS[bucket] }}
                  />
                  {t(`credit.bucket.${bucket}`) === `credit.bucket.${bucket}`
                    ? bucket
                    : t(`credit.bucket.${bucket}`)}
                </span>
              </th>
            ))}
            <th className="px-2 py-2 text-right font-medium">
              {t('credit.outstanding')}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            // The row's own largest cell is its scale. A row of zeros divides by
            // nothing, so it stays unshaded rather than becoming a row of NaN.
            const peak = Math.max(...row.amounts, 0);
            return (
              <tr
                key={row.code ?? row.name}
                className="border-b border-slate-100 last:border-0 dark:border-slate-800"
              >
                <td className="px-2 py-1.5 font-medium">{row.name}</td>
                {row.amounts.map((amount, index) => {
                  const share = peak > 0 ? amount / peak : 0;
                  return (
                    <td
                      key={matrix.buckets[index]}
                      className="px-2 py-1.5 text-right tabular-nums"
                      style={
                        share > FLOOR
                          ? { background: `rgba(${RAMP}, ${0.06 + share * 0.24})` }
                          : undefined
                      }
                      // The figure is read by sighted users from the cell and by
                      // everybody else from here; the shading is never the only
                      // signal, which is the rule the stock statuses follow too.
                      title={`${row.name} · ${matrix.buckets[index]}: ${formatAmount(amount)} · ${
                        row.counts[index] ?? 0
                      }`}
                    >
                      {amount ? formatAmount(amount) : '—'}
                    </td>
                  );
                })}
                <td className="px-2 py-1.5 text-right font-semibold tabular-nums">
                  {formatAmount(row.outstanding_amount)}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="border-t border-slate-300 font-semibold dark:border-slate-600">
            <td className="px-2 py-2">{t('common.total')}</td>
            {totals.map((total, index) => (
              <td
                key={matrix.buckets[index]}
                className="px-2 py-2 text-right tabular-nums"
              >
                {total ? formatAmount(total) : '—'}
              </td>
            ))}
            <td className="px-2 py-2 text-right tabular-nums">
              {formatAmount(grandTotal)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
