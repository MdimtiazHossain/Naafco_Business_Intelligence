/**
 * Achievement bands as toggles: which performance ranges the map draws.
 *
 * The bands are the ones the whole map already uses — `ACHIEVEMENT_BANDS`, the
 * same breaks the legend names and the same colours the bubbles are painted in
 * — so a chip and the points it governs cannot mean different things. Turning
 * one off hides those points; nothing is recomputed and nothing is recoloured.
 *
 * Offered only where the map is measuring achievement. In a mode that measures
 * nothing there is no band to filter on, and a control that silently did
 * nothing would be worse than no control.
 */

import { useT } from '../../contexts/I18nContext';
import { ACHIEVEMENT_BANDS } from './mapConfig';

export function MapBandChips({
  active,
  onChange,
}: {
  /** Band label keys currently drawn. */
  active: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
}) {
  const t = useT();

  function toggle(key: string) {
    const next = new Set(active);
    // The last band cannot be switched off: an empty map with every chip dark
    // reads as "no data" rather than as a filter the reader set.
    if (next.has(key) && next.size === 1) return;
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange(next);
  }

  return (
    <div className="card p-3 text-xs">
      <p className="mb-2 font-medium text-slate-500">{t('map.bandFilter')}</p>
      <div className="flex flex-wrap gap-1.5">
        {ACHIEVEMENT_BANDS.map((band) => {
          const on = active.has(band.labelKey);
          return (
            <button
              key={band.labelKey}
              type="button"
              aria-pressed={on}
              onClick={() => toggle(band.labelKey)}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] font-semibold transition-opacity ${
                on
                  ? 'border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-300'
                  : 'border-slate-200 text-slate-400 opacity-50 dark:border-slate-800'
              }`}
            >
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: band.color }}
                aria-hidden="true"
              />
              {t(band.labelKey)}
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-slate-400">
        {t('map.bandFilterHint')}
      </p>
    </div>
  );
}
