/**
 * The map's own controls: which layers are drawn, and where the view sits.
 *
 * Zoom, fullscreen and the scale bar are MapLibre's built-in controls, added by
 * `BusinessMap` — re-implementing them in React would mean two things that both
 * change the same camera. What is here is what MapLibre has no opinion about:
 * the administrative levels, the reference layers, and the two framings a
 * business user actually wants ("show me the country", "show me my data").
 */

import { Crosshair, Layers, Locate, Map as MapIcon } from 'lucide-react';
import { useState } from 'react';
import { useT } from '../../contexts/I18nContext';
import {
  BOUNDARY_LEVELS,
  MAP_MODES,
  type BoundaryLevelKey,
  type MapModeKey,
} from './mapConfig';
import type { MapLayer } from '../../types/api';

export interface MapControlsProps {
  activeLevels: readonly BoundaryLevelKey[];
  onLevelsChange: (levels: BoundaryLevelKey[]) => void;
  showMask: boolean;
  showCapitals: boolean;
  showAdminLines: boolean;
  onReferenceChange: (next: { capitals: boolean; lines: boolean; mask: boolean }) => void;
  onFitCountry: () => void;
  /** Absent when nothing is plotted, so the button is never a no-op. */
  onFitData?: () => void;

  /* The two workspace controls. They live in the map rather than above it
     because they are what the reader changes *while looking*, and a control
     that changes the picture belongs beside the picture. */
  mode: MapModeKey;
  onModeChange: (mode: MapModeKey) => void;
  /** Which modes this deployment has data for; the rest render disabled. */
  availableModes: ReadonlyMap<string, boolean>;
  /** The business level drawn as points, and the levels worth offering. */
  salesLevel: MapLayer;
  onSalesLevelChange: (level: MapLayer) => void;
  salesLevels: readonly { key: MapLayer; labelKey: string; count: number }[];
}

export function MapControls({
  activeLevels,
  onLevelsChange,
  showMask,
  showCapitals,
  showAdminLines,
  onReferenceChange,
  onFitCountry,
  onFitData,
  mode,
  onModeChange,
  availableModes,
  salesLevel,
  onSalesLevelChange,
  salesLevels,
}: MapControlsProps) {
  const t = useT();
  // Collapsed by default: the panel is a settings surface, and an expanded one
  // covers the corner of the country it sits over on a laptop screen.
  const [open, setOpen] = useState(false);

  function toggleLevel(key: BoundaryLevelKey) {
    onLevelsChange(
      activeLevels.includes(key)
        ? activeLevels.filter((level) => level !== key)
        : BOUNDARY_LEVELS.filter(
            (level) => level.key === key || activeLevels.includes(level.key),
          ).map((level) => level.key),
    );
  }

  const reference = { capitals: showCapitals, lines: showAdminLines, mask: showMask };

  const chrome =
    'rounded-lg border border-slate-200 bg-white/95 text-xs shadow-sm backdrop-blur ' +
    'dark:border-slate-700 dark:bg-slate-900/95';

  return (
    <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-col gap-2">
      {/* Map View and Sales Level: what is drawn, and at what grain. */}
      <div className="pointer-events-auto flex flex-wrap gap-1">
        <label className="sr-only" htmlFor="map-view-control">{t('map.mode')}</label>
        <select
          id="map-view-control"
          className={`${chrome} max-w-[11rem] px-2 py-1.5 font-medium`}
          value={mode}
          onChange={(event) => onModeChange(event.target.value as MapModeKey)}
          aria-label={t('map.mode')}
        >
          {MAP_MODES.map((option) => {
            const enabled = availableModes.get(option.key);
            return (
              <option key={option.key} value={option.key} disabled={!enabled}>
                {t(option.labelKey)}
                {enabled ? '' : ` — ${t('map.modeUnavailable')}`}
              </option>
            );
          })}
        </select>

        <label className="sr-only" htmlFor="sales-level-control">{t('map.salesLevel')}</label>
        <select
          id="sales-level-control"
          className={`${chrome} max-w-[10rem] px-2 py-1.5 font-medium`}
          value={salesLevel}
          onChange={(event) => onSalesLevelChange(event.target.value as MapLayer)}
          aria-label={t('map.salesLevel')}
        >
          {salesLevels.map((level) => (
            <option key={level.key} value={level.key}>
              {t(level.labelKey)} ({level.count})
            </option>
          ))}
        </select>
      </div>

      <div className="pointer-events-auto flex gap-1">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white/95 px-2.5 py-1.5 text-xs font-medium shadow-sm backdrop-blur hover:bg-white dark:border-slate-700 dark:bg-slate-900/95 dark:hover:bg-slate-900"
        >
          <Layers size={14} />
          {t('map.layers')}
        </button>
        <button
          type="button"
          onClick={onFitCountry}
          title={t('map.fitCountry')}
          aria-label={t('map.fitCountry')}
          className="rounded-lg border border-slate-200 bg-white/95 p-1.5 shadow-sm backdrop-blur hover:bg-white dark:border-slate-700 dark:bg-slate-900/95 dark:hover:bg-slate-900"
        >
          <MapIcon size={14} />
        </button>
        {onFitData && (
          <button
            type="button"
            onClick={onFitData}
            title={t('map.fitData')}
            aria-label={t('map.fitData')}
            className="rounded-lg border border-slate-200 bg-white/95 p-1.5 shadow-sm backdrop-blur hover:bg-white dark:border-slate-700 dark:bg-slate-900/95 dark:hover:bg-slate-900"
          >
            <Crosshair size={14} />
          </button>
        )}
      </div>

      {open && (
        <div className="pointer-events-auto w-56 rounded-lg border border-slate-200 bg-white/95 p-3 text-xs shadow-lg backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
          <p className="mb-1.5 font-medium text-slate-500">{t('map.adminLevels')}</p>
          <ul className="space-y-1">
            {BOUNDARY_LEVELS.map((level) => (
              <li key={level.key}>
                <label className="flex cursor-pointer items-center gap-2">
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 accent-brand-600"
                    checked={activeLevels.includes(level.key)}
                    onChange={() => toggleLevel(level.key)}
                  />
                  <span>{t(level.labelKey)}</span>
                  {level.minZoom > 0 && (
                    <span className="ml-auto text-[10px] text-slate-400">
                      {t('map.fromZoom', { zoom: String(level.minZoom) })}
                    </span>
                  )}
                </label>
              </li>
            ))}
          </ul>

          <p className="mb-1.5 mt-3 font-medium text-slate-500">{t('map.reference')}</p>
          <ul className="space-y-1">
            <li>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-600"
                  checked={showMask}
                  onChange={() => onReferenceChange({ ...reference, mask: !showMask })}
                />
                <span>{t('map.focusMask')}</span>
              </label>
            </li>
            <li>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-600"
                  checked={showCapitals}
                  onChange={() =>
                    onReferenceChange({ ...reference, capitals: !showCapitals })
                  }
                />
                <span>{t('map.capitals')}</span>
              </label>
            </li>
            <li>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-600"
                  checked={showAdminLines}
                  onChange={() =>
                    onReferenceChange({ ...reference, lines: !showAdminLines })
                  }
                />
                <span>{t('map.adminLines')}</span>
              </label>
            </li>
          </ul>

          <p className="mt-3 flex items-start gap-1.5 text-[10px] leading-relaxed text-slate-400">
            <Locate size={11} className="mt-0.5 shrink-0" />
            {t('map.controlsHint')}
          </p>
        </div>
      )}
    </div>
  );
}
