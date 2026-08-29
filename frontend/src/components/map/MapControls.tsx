/**
 * The map's own framing controls.
 *
 * Zoom, fullscreen and the scale bar are MapLibre's built-in controls, added by
 * `BusinessMap` — re-implementing them in React would mean two things that both
 * change the same camera. What is left here is what MapLibre has no opinion
 * about: the two framings a business user actually wants, "show me the country"
 * and "show me my data".
 *
 * Everything else this panel used to hold — the administrative levels and the
 * reference layers — is in `MapLayerPanel`, in the page's rail. A control that
 * is set once and then read off the map does not need to sit on top of it.
 */

import { Crosshair, Map as MapIcon } from 'lucide-react';
import { useT } from '../../contexts/I18nContext';

export interface MapControlsProps {
  onFitCountry: () => void;
  /** Absent when nothing is plotted, so the button is never a no-op. */
  onFitData?: () => void;
}

export function MapControls({ onFitCountry, onFitData }: MapControlsProps) {
  const t = useT();

  return (
    <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-col gap-2">
      <div className="pointer-events-auto flex gap-1">
        <button
          type="button"
          onClick={onFitCountry}
          title={t('map.fitCountry')}
          aria-label={t('map.fitCountry')}
          className="tap flex items-center justify-center rounded-lg border border-slate-200 bg-white/95 p-1.5 shadow-sm backdrop-blur hover:bg-white dark:border-slate-700 dark:bg-slate-900/95 dark:hover:bg-slate-900"
        >
          <MapIcon size={14} />
        </button>
        {onFitData && (
          <button
            type="button"
            onClick={onFitData}
            title={t('map.fitData')}
            aria-label={t('map.fitData')}
            className="tap flex items-center justify-center rounded-lg border border-slate-200 bg-white/95 p-1.5 shadow-sm backdrop-blur hover:bg-white dark:border-slate-700 dark:bg-slate-900/95 dark:hover:bg-slate-900"
          >
            <Crosshair size={14} />
          </button>
        )}
      </div>
    </div>
  );
}
