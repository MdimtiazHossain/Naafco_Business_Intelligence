/**
 * The ranking beside the map: which zones, regions, areas, units or territories
 * are ahead, and a way into any of them.
 *
 * A map answers "where", and this answers "which" — the two questions a reader
 * moves between constantly, which is why the panel sits next to the map rather
 * than on a page of its own. It ranks whatever level it is pointed at over
 * whatever the filters currently admit, and a click walks one level deeper.
 *
 * Every figure here comes from `/api/pages/performance` — the same endpoint the
 * Performance page reads — so the map and that page cannot disagree about a
 * number. This component computes nothing: it is handed rows and draws them.
 */

import { useT } from '../../contexts/I18nContext';
import { EmptyState } from '../States';
import { formatAmount, formatPercent } from '../../utils/format';
import { ACHIEVEMENT_BANDS } from './mapConfig';

export interface RankRow {
  code: string;
  label: string;
  /** Actual ÷ target. `null` where no target covers the period — never 0. */
  achievement: number | null;
  net_sales: number;
}

/** The band a percentage falls in, for its colour. */
export function bandColor(value: number | null) {
  if (value === null) return undefined;
  return ACHIEVEMENT_BANDS.find((band) => band.max === null || value < band.max)?.color;
}

export function MapRanking({
  level,
  levels,
  levelLabel,
  onLevelChange,
  rows,
  activeCode,
  onSelect,
  loading = false,
}: {
  level: string;
  /** The drill chain, in order — what the level selector offers. */
  levels: readonly string[];
  /** Translated name of a level, for the selector and the column heading. */
  levelLabel: (level: string) => string;
  onLevelChange: (level: string) => void;
  rows: RankRow[];
  activeCode?: string;
  onSelect: (row: RankRow) => void;
  loading?: boolean;
}) {
  const t = useT();
  // Achievement is the ranking the business asks for, but it is only published
  // for the levels a target names. Where it is absent the panel ranks by net
  // sales and says so, rather than showing an order with no visible basis.
  const ranked = rows.some((row) => row.achievement !== null);

  return (
    <div className="card flex min-h-0 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-800">
        <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
          {t('map.ranking')}
        </span>
        <label className="flex items-center gap-1.5">
          <span className="sr-only">{t('map.rankBy')}</span>
          <select
            className="input w-auto py-1 text-xs"
            value={level}
            onChange={(event) => onLevelChange(event.target.value)}
            aria-label={t('map.rankBy')}
          >
            {levels.map((option) => (
              <option key={option} value={option}>
                {levelLabel(option)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {rows.length === 0 ? (
        <EmptyState message={loading ? t('common.loading') : t('map.rankEmpty')} />
      ) : (
        <div className="min-h-0 max-h-[32rem] flex-1 overflow-auto xl:max-h-none">
          {/* Fills the rail it sits in, and is capped only where there is
              no rail to fill — stacked on a narrow screen this panel has
              no height to share, so an unbounded fifty-row table would
              push the map off the screen. */}
          <table className="w-full border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-white dark:bg-slate-900">
              <tr className="border-b border-slate-200 text-[10px] uppercase tracking-wide text-slate-400 dark:border-slate-800">
                <th scope="col" className="px-2 py-1.5 text-right font-semibold">
                  #
                </th>
                <th scope="col" className="px-2 py-1.5 text-left font-semibold">
                  {levelLabel(level)}
                </th>
                <th scope="col" className="px-2 py-1.5 text-right font-semibold">
                  {t('target.achievement')}
                </th>
                <th scope="col" className="px-2 py-1.5 text-right font-semibold">
                  {t('sales.netSales')}
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr
                  key={row.code}
                  onClick={() => onSelect(row)}
                  className={`cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/50 ${
                    activeCode === row.code ? 'bg-brand-50 dark:bg-slate-800' : ''
                  }`}
                >
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                    {index + 1}
                  </td>
                  {/* The code goes under the name rather than beside it: these
                      codes are long enough to crowd the name off the panel, and
                      the name is what the reader is scanning for. */}
                  <td className="px-2 py-1.5">
                    <div className="font-medium text-slate-700 dark:text-slate-200">
                      {row.label}
                    </div>
                    <div className="text-[10px] text-slate-400">{row.code}</div>
                  </td>
                  {/* Colour repeats the band the map paints this level in, so a
                      row and its shape on the map read as the same thing. It is
                      never the only signal — the figure is right beside it. */}
                  <td
                    className="px-2 py-1.5 text-right font-semibold tabular-nums"
                    style={{ color: bandColor(row.achievement) }}
                  >
                    {formatPercent(row.achievement)}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-600 dark:text-slate-300">
                    {formatAmount(row.net_sales)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > 0 && !ranked && (
        <p className="border-t border-slate-200 px-3 py-1.5 text-[11px] text-slate-500 dark:border-slate-800 dark:text-slate-400">
          {t('map.rankNoTarget')}
        </p>
      )}
    </div>
  );
}
