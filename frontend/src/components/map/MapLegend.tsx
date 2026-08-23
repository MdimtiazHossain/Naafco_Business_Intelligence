/**
 * What the map's symbols mean.
 *
 * Every swatch is drawn from the same source the map draws from: marker artwork
 * is the Marker Designer's server-rendered SVG, and a boundary swatch is painted
 * with the style row `map_area_styles` holds. Neither is re-specified here, so a
 * legend cannot describe a map that no longer looks like that.
 */

import { MarkerPreview } from '../MarkerPreview';
import { useT } from '../../contexts/I18nContext';
import type { AreaStyle, MarkerLegendEntry } from '../../types/api';
import {
  BOUNDARY_LEVELS,
  FALLBACK_AREA_STYLES,
  type BoundaryLevelKey,
} from './mapConfig';

export interface MapLegendProps {
  entries: readonly MarkerLegendEntry[] | undefined;
  activeLevels: readonly BoundaryLevelKey[];
  areaStyles: Partial<Record<BoundaryLevelKey, AreaStyle>>;
  /** Entity types with at least one placed record, so the legend matches. */
  drawnTypes: ReadonlySet<string>;
}

export function MapLegend({
  entries,
  activeLevels,
  areaStyles,
  drawnTypes,
}: MapLegendProps) {
  const t = useT();

  // Promoted entries are the ones the designer marked as worth listing; of
  // those, only the types actually on screen are shown. A legend row for a
  // marker the map is not drawing is noise the reader has to rule out.
  const markers = (entries ?? []).filter(
    (entry) => entry.promoted && drawnTypes.has(entry.entity_type),
  );

  const levels = BOUNDARY_LEVELS.filter((level) => activeLevels.includes(level.key));

  if (!markers.length && !levels.length) {
    return <p className="text-xs text-slate-400">{t('map.legendEmpty')}</p>;
  }

  return (
    <ul className="space-y-2">
      {markers.map((entry) => (
        <li key={entry.entity_type} className="flex items-center gap-2">
          <MarkerPreview svg={entry.preview_svg} size={24} />
          <span className="truncate text-sm">{entry.label}</span>
        </li>
      ))}

      {levels.map((level, index) => {
        const style = areaStyles[level.key] ?? FALLBACK_AREA_STYLES[level.key];
        return (
          <li
            key={level.key}
            className={`flex items-center gap-2 ${
              index === 0 && markers.length
                ? 'border-t border-slate-200 pt-2 dark:border-slate-700'
                : ''
            }`}
          >
            <span
              aria-hidden="true"
              className="inline-block h-6 w-6 shrink-0 rounded"
              style={{
                backgroundColor: style.fill_color,
                // The swatch has to be visible even where the level is unfilled
                // on the map — the stroke is what distinguishes those levels,
                // and a blank square would say nothing.
                opacity: Math.max(style.fill_opacity, 0.12),
                border: `${Math.max(style.stroke_width, 1)}px solid ${style.stroke_color}`,
              }}
            />
            <span className="truncate text-sm">{t(level.labelKey)}</span>
          </li>
        );
      })}
    </ul>
  );
}
