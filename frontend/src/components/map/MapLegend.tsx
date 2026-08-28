/**
 * What the map's symbols mean.
 *
 * Every swatch is drawn from the same source the map draws from: marker artwork
 * is the Marker Designer's server-rendered SVG, and a boundary swatch is painted
 * with the style row `map_area_styles` holds. Neither is re-specified here, so a
 * legend cannot describe a map that no longer looks like that.
 *
 * Two placements, one component. `panel` is the rail card it has always been;
 * `overlay` is the same rows compacted into a card floating over the map's
 * bottom-left corner, the way `MapControls` floats over its top-left. *Which*
 * rows are drawn and what they say is decided once, above the split — only
 * density and chrome differ, so the two placements cannot end up describing the
 * same map differently.
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

/** Rail card, or floating over the map. Presentation only. */
export type MapLegendVariant = 'panel' | 'overlay';

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
  /** Where this legend is being drawn. Density and chrome only. */
  variant?: MapLegendVariant;
}

export function MapLegend({
  entries,
  activeLevels,
  areaStyles,
  drawnTypes,
  mode,
  modeAvailable,
  variant = 'panel',
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

  const overlay = variant === 'overlay';

  if (!markers.length && !levels.length && !scale.length) {
    // The rail says why it is empty, because the reader is looking at a card
    // that would otherwise be blank. Over the map there is no card to explain —
    // an empty floating box would be the only thing it said.
    return overlay ? null : <p className="text-xs text-slate-400">{t('map.legendEmpty')}</p>;
  }

  const rowGap = overlay ? 'gap-1.5' : 'gap-2';
  const swatchSize = overlay ? 'h-3.5 w-3.5' : 'h-6 w-6';
  const labelSize = overlay ? 'text-[11px] leading-tight' : 'text-sm';
  const dividerPad = overlay ? 'pt-1' : 'pt-2';

  const rows = (
    <ul className={overlay ? 'space-y-1' : 'space-y-2'}>
      {scale.map((step) => (
        <li key={step.label} className={`flex items-center ${rowGap}`}>
          <span
            aria-hidden="true"
            // Round over the map, square in the rail: the floating key reads as
            // a set of dots matching the circles drawn beside it, and the rail
            // card has the room for a swatch that shows the colour properly.
            className={`inline-block shrink-0 ${swatchSize} ${overlay ? 'rounded-full' : 'rounded'}`}
            style={{ backgroundColor: step.colour }}
          />
          <span className={`truncate ${labelSize}`}>{step.label}</span>
        </li>
      ))}

      {markers.map((entry) => (
        <li key={entry.entity_type} className={`flex items-center ${rowGap}`}>
          <MarkerPreview svg={entry.preview_svg} size={overlay ? 16 : 24} />
          <span className={`truncate ${labelSize}`}>{entry.label}</span>
        </li>
      ))}

      {levels.map((level, index) => {
        const style = areaStyles[level.key] ?? FALLBACK_AREA_STYLES[level.key];
        return (
          <li
            key={level.key}
            className={`flex items-center ${rowGap} ${
              index === 0 && markers.length
                ? `border-t border-slate-200 ${dividerPad} dark:border-slate-700`
                : ''
            }`}
          >
            <span
              aria-hidden="true"
              className={`inline-block shrink-0 rounded ${swatchSize}`}
              style={{
                backgroundColor: style.fill_color,
                // The swatch has to be visible even where the level is unfilled
                // on the map — the stroke is what distinguishes those levels,
                // and a blank square would say nothing.
                opacity: Math.max(style.fill_opacity, 0.12),
                border: `${Math.max(style.stroke_width, 1)}px solid ${style.stroke_color}`,
              }}
            />
            <span className={`truncate ${labelSize}`}>{t(level.labelKey)}</span>
          </li>
        );
      })}
    </ul>
  );

  if (!overlay) return rows;

  // Positions itself, the same way `MapControls` does — both are chrome the map
  // owns rather than things the page lays out around it. `pointer-events-none`
  // so a legend sitting over the country never swallows a drag.
  return (
    <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-lg border border-slate-200 bg-white/95 p-2.5 shadow-sm backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
      {rows}
    </div>
  );
}
