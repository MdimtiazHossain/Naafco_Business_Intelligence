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
  ACHIEVEMENT_BANDS,
  BOUNDARY_LEVELS,
  FALLBACK_AREA_STYLES,
  MODE_BY_KEY,
  SEQUENTIAL_RAMP,
  type BoundaryLevelKey,
  type MapModeKey,
} from './mapConfig';

export interface MapLegendProps {
  entries: readonly MarkerLegendEntry[] | undefined;
  activeLevels: readonly BoundaryLevelKey[];
  areaStyles: Partial<Record<BoundaryLevelKey, AreaStyle>>;
  /** Entity types with at least one placed record, so the legend matches. */
  drawnTypes: ReadonlySet<string>;
  /** The mode on screen; the legend explains that and nothing else. */
  mode: MapModeKey;
  /** False when the mode is listed but has no data, so no scale is drawn. */
  modeAvailable: boolean;
}

export function MapLegend({
  entries,
  activeLevels,
  areaStyles,
  drawnTypes,
  mode,
  modeAvailable,
}: MapLegendProps) {
  const t = useT();

  // Promoted entries are the ones the designer marked as worth listing; of
  // those, only the types actually on screen are shown. A legend row for a
  // marker the map is not drawing is noise the reader has to rule out.
  const markers = (entries ?? []).filter(
    (entry) => entry.promoted && drawnTypes.has(entry.entity_type),
  );

  const levels = BOUNDARY_LEVELS.filter((level) => activeLevels.includes(level.key));

  /* The scale for the mode being read, when it is colouring anything. A mode
     with no data draws no scale at all: a key to colours that are not on the
     map is worse than no key, because it implies they are. */
  const encoding = modeAvailable ? MODE_BY_KEY[mode].encoding : 'none';
  const scale: { colour: string; label: string }[] =
    // Colour means achievement wherever colour means anything, so the key is
    // the agreed bands. A density surface encodes count, not a business figure,
    // and says so in words rather than borrowing the achievement palette.
    encoding === 'size+colour' || encoding === 'colour'
      ? ACHIEVEMENT_BANDS.map((band) => ({ colour: band.color, label: t(band.labelKey) }))
      : encoding === 'count'
        ? [
            { colour: SEQUENTIAL_RAMP[0], label: t('map.legendLow') },
            { colour: SEQUENTIAL_RAMP[2], label: t('map.legendMedium') },
            { colour: SEQUENTIAL_RAMP[4], label: t('map.legendHigh') },
          ]
        : [];

  if (!markers.length && !levels.length && !scale.length) {
    return <p className="text-xs text-slate-400">{t('map.legendEmpty')}</p>;
  }

  return (
    <ul className="space-y-2">
      {scale.map((step) => (
        <li key={step.label} className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="inline-block h-6 w-6 shrink-0 rounded"
            style={{ backgroundColor: step.colour }}
          />
          <span className="truncate text-sm">{step.label}</span>
        </li>
      ))}

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
