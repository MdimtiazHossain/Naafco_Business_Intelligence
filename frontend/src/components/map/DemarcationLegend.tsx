/**
 * The demarcation legend: which shape is which level, and how many are placed.
 *
 * The analysis legend explains what a *colour* means, because that map encodes
 * a figure. This one explains what a *shape* means, because that map encodes a
 * level — and it answers the question a reader of coordinates actually has,
 * which is how much of each level is on the screen at all.
 *
 * **It explains a colour too, once there is one.** Shape carries the level and
 * colour carries the parent, so the two lists are separate and both are needed:
 * a reader has to be able to say both "that is a territory" and "that one is in
 * Mirpur". The group list renders the colours the server assigned, in the order
 * it sent them, so a swatch here and a dot on the canvas cannot come apart.
 *
 * Two numbers per level, deliberately. `placed` is what is drawn; `missing` is
 * what the master holds and has no coordinate at all — a centroid is neither,
 * and is reported on its own line as what the map is deliberately not drawing. A legend that printed only
 * the first would let a half-loaded level look complete, which on a map used to
 * judge where a boundary falls is the difference between "this area ends here"
 * and "we have not placed the rest yet".
 *
 * Under a filter the count becomes a pair, `placed of available`, and the two
 * denominators are different questions asked of the same level: `available` is
 * how many coordinates exist there inside the reader's scope, `total` is how
 * many records the master holds. So a level can read "9 of 94" and still say
 * eleven records have no coordinate — narrow filter, well-mapped level — or
 * read "94 of 94" beside the same sentence, which is the level nobody has
 * finished surveying. A single number cannot tell those apart.
 *
 * **A level with no coordinates at all is a finding, not an empty result**, so
 * it keeps its row in the legend and reads zero rather than disappearing: the
 * level somebody still has to survey is exactly what a demarcation reader is
 * looking for.
 *
 * The swatch is drawn from the same server-declared path the map draws, so a
 * shape can never mean one thing in the legend and another on the canvas.
 */

import { useT } from '../../contexts/I18nContext';
import type { MapColorBy, MapLocationLayer, MapShape } from '../../types/api';

export interface DemarcationLegendProps {
  layers: MapLocationLayer[];
  shapes: MapShape[];
  /** A filter is narrowing the points, so each level counts matched of total. */
  narrowed?: boolean;
  /** The groups the points were coloured by, if any. */
  colorBy?: MapColorBy | null;
  activeLevel: string;
  onActiveLevel: (level: string) => void;
}

/** One shape at one colour, as an inline SVG the same size as a text line. */
export function ShapeSwatch({
  shape, color, size = 14, hollow = false,
}: { shape: MapShape | undefined; color: string; size?: number; hollow?: boolean }) {
  if (!shape) return null;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${shape.viewbox} ${shape.viewbox}`}
      aria-hidden="true"
      className="shrink-0"
    >
      <path
        d={shape.path}
        fill={hollow ? 'none' : color}
        stroke={color}
        strokeWidth={hollow ? 2.5 : 1}
      />
    </svg>
  );
}

export function DemarcationLegend({
  layers, shapes, narrowed = false, colorBy = null, activeLevel, onActiveLevel,
}: DemarcationLegendProps) {
  const t = useT();
  if (layers.length === 0) return null;
  const shapeFor = (key: string) => shapes.find((shape) => shape.key === key);
  const active = layers.find((layer) => layer.level === activeLevel) ?? layers[0];
  const totalDerived = layers.reduce((sum, layer) => sum + layer.derived, 0);

  return (
    <div className="absolute bottom-8 left-3 z-10 max-w-[15rem] rounded-lg bg-white/95 p-3 text-xs shadow-lg backdrop-blur dark:bg-slate-900/95">
      <p className="mb-2 font-semibold text-slate-700 dark:text-slate-200">
        {t('map.demarcationLegend')}
      </p>
      <ul className="space-y-1.5">
        {layers.map((layer) => {
          const isActive = layer.level === active.level;
          return (
            <li key={layer.level}>
              <button
                type="button"
                onClick={() => onActiveLevel(layer.level)}
                className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left ${
                  isActive
                    ? 'bg-slate-100 font-medium text-slate-900 dark:bg-slate-800 dark:text-slate-100'
                    : 'text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800/60'
                }`}
                aria-pressed={isActive}
              >
                <ShapeSwatch
                  shape={shapeFor(layer.layer.style.shape)}
                  color={layer.layer.style.point_color}
                />
                <span className="flex-1 truncate">{layer.label}</span>
                <span className="tabular-nums text-slate-500 dark:text-slate-400">
                  {narrowed
                    ? t('map.legendMatched', { placed: String(layer.placed),
                                               available: String(layer.available) })
                    : layer.placed}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {/* What the map is *not* drawing, stated once for the whole map.
          A centroid is the average of the coordinates below it, so it marks a
          spot nobody surveyed; this tab counts them and leaves them off. Said
          here rather than per level because the rule is the same on every
          layer, and the per-level notes already name the counts. */}
      {totalDerived > 0 && (
        <p className="mt-2 border-t border-slate-200 pt-2 text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {t('map.legendDerived', { count: String(totalDerived) })}
        </p>
      )}

      {/*
        What each colour means, when the points are coloured by a parent.

        Named, not merely swatched: a reader looking at eleven colours has to
        be able to say which area is which, and the shape swatches above already
        answer a different question (which *level* a dot is). The list is the
        server's, in the server's order — by size, so the group carrying the map
        is at the top — and it renders the colour it was sent rather than
        recomputing one, which is what stops a swatch and a dot disagreeing.

        Capped in height rather than in length: 94 territories in focus mode is
        a real list somebody scrolls to find one entry, and truncating it would
        hide exactly the group they were looking for.
      */}
      {colorBy && colorBy.groups.length > 0 && (
        <div className="mt-2 border-t border-slate-200 pt-2 dark:border-slate-700">
          <p className="mb-1 font-semibold text-slate-700 dark:text-slate-200">
            {t('map.colorLegend', { level: colorBy.label.toLowerCase() })}
          </p>
          <ul className="max-h-32 space-y-1 overflow-y-auto pr-1">
            {colorBy.groups.map((group) => (
              <li key={group.code} className="flex items-center gap-2">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: group.color }}
                  aria-hidden="true"
                />
                <span className="flex-1 truncate text-slate-600 dark:text-slate-300">
                  {group.name}
                </span>
                <span className="tabular-nums text-slate-500 dark:text-slate-400">
                  {group.count}
                </span>
              </li>
            ))}
          </ul>
          {colorBy.ungrouped > 0 && (
            <p className="mt-1 text-slate-500 dark:text-slate-400">
              {t('map.colorUngrouped', { count: String(colorBy.ungrouped) })}
            </p>
          )}
        </div>
      )}

      {active.missing > 0 && (
        <p className="mt-2 border-t border-slate-200 pt-2 text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {t('map.legendMissing', {
            missing: String(active.missing),
            total: String(active.total),
            level: active.label.toLowerCase(),
          })}
        </p>
      )}
    </div>
  );
}
