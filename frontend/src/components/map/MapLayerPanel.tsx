/**
 * Which administrative geography the map draws.
 *
 * This used to sit inside the map, in a panel that had to be opened and that
 * covered the corner of the country it was drawn over. It is a settings surface
 * rather than something read off the map, so it belongs in the page's own rail
 * where it is visible without hiding anything.
 *
 * The state it edits is not its own: the levels and the reference toggles live
 * in `MapPage` and are handed to `BusinessMap` to apply, so this panel is a
 * control and never the authority.
 */

import { Locate } from 'lucide-react';
import { useT } from '../../contexts/I18nContext';
import { BOUNDARY_LEVELS, type BoundaryLevelKey } from './mapConfig';

export function MapLayerPanel({
  activeLevels,
  onLevelsChange,
  showMask,
  showCapitals,
  showAdminLines,
  onReferenceChange,
}: {
  activeLevels: readonly BoundaryLevelKey[];
  onLevelsChange: (levels: BoundaryLevelKey[]) => void;
  showMask: boolean;
  showCapitals: boolean;
  showAdminLines: boolean;
  onReferenceChange: (next: { capitals: boolean; lines: boolean; mask: boolean }) => void;
}) {
  const t = useT();

  // Toggling a level rebuilds the set in `BOUNDARY_LEVELS` order rather than
  // appending, so the levels stay coarse-to-fine however they were switched on.
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

  return (
    <div className="card p-3 text-xs">
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
              onChange={() => onReferenceChange({ ...reference, capitals: !showCapitals })}
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
              onChange={() => onReferenceChange({ ...reference, lines: !showAdminLines })}
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
  );
}
