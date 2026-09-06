/**
 * Choosing an administrative backdrop, and reading the outline you clicked.
 *
 * One component for both maps: the picker and the selected-outline card are
 * identical on the Business Map and on Area Demarcation, because a district is
 * a district on either. Written here once rather than twice for the same
 * reason `useBoundaryLayer` is one hook.
 *
 * **A wait is explained while it happens, not predicted in the label.** The
 * upazila file is 1.7 MB and a control that silently stalls for several seconds
 * reads as broken — that much is unchanged, and it is still the problem being
 * solved. What changed is the answer. The option used to read
 * `Upazilas (1.7 MB)`; now it reads `Upazilas`, and the spinner beside the
 * control says `map.boundaryLoading` for exactly as long as the fetch takes.
 *
 * The spinner is the better instrument because a size is a *prediction* and it
 * is wrong in both directions: it warns every time, including the second time
 * when `geoData` has the file cached and the swap is instant, and it says
 * nothing about a slow connection making 370 KB take just as long. The
 * loading state fires when there is a wait and stays silent when there is not,
 * which is the thing the size was a proxy for.
 *
 * It also had to become real. `useBoundaryLayer` returned `{ loading, error }`
 * and both map components discarded it, so this component's `loading` prop had
 * no caller and `map.boundaryLoading` had never once rendered. Area Demarcation
 * now opens with upazilas by default, so the longest wait on either map happens
 * before anybody touches this control — which is precisely when a prediction in
 * an option label would have been no use at all.
 *
 * **What the note says matters more than where it sits.** No fact table
 * carries a district, so nothing on this backdrop is coloured by a figure and
 * none of it says where a territory ends. The server states that in its
 * catalogue payload and this renders it, rather than the browser inventing a
 * caption somebody would have to keep in step.
 */

import { Info, Loader2, X } from 'lucide-react';
import { useT } from '../../contexts/I18nContext';
import type { MapBoundaryCatalogue, MapBoundarySet } from '../../types/api';
import type { BoundarySelection } from './useBoundaryLayer';

/*
 * `readableSize` and `WARN_BYTES` lived here and are gone with the label that
 * used them. An unused helper is a thing somebody wires back up.
 *
 * `bytes` and `features` stay in the catalogue payload, and not on the chance
 * something wants them: `test_map_boundaries` reads both and asserts them
 * against the real files on disk, which is what stops the server's catalogue
 * claiming a size or a feature count that was never deployed.
 */

export interface BoundaryControlProps {
  catalogue: MapBoundaryCatalogue;
  value: string | null;
  onChange: (key: string | null) => void;
  loading?: boolean;
  /** A failed fetch is a note, never a thrown error: the map still works. */
  error?: string | null;
  id?: string;
}

export function BoundaryControl({
  catalogue, value, onChange, loading, error, id = 'map-boundary',
}: BoundaryControlProps) {
  const t = useT();
  const chosen = catalogue.sets.find((set) => set.key === value) ?? null;

  return (
    <div className="flex items-center gap-2">
      <label className="label mb-0 whitespace-nowrap" htmlFor={id}>
        {t('map.boundary')}
      </label>
      <select
        id={id}
        className="input w-auto min-w-[9rem] py-1.5"
        value={value ?? ''}
        onChange={(event) => onChange(event.target.value || null)}
      >
        <option value="">{t('map.boundaryNone')}</option>
        {catalogue.sets.map((set) => (
          <option key={set.key} value={set.key}>{set.label}</option>
        ))}
      </select>
      {loading && (
        <span className="flex items-center gap-1 text-xs text-slate-500" role="status">
          <Loader2 size={12} className="animate-spin" />
          {t('map.boundaryLoading')}
        </span>
      )}
      {!loading && error && (
        <span className="text-xs text-amber-600 dark:text-amber-400">{error}</span>
      )}
      {!loading && !error && chosen && (
        <span
          className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400"
          title={catalogue.note}
        >
          <Info size={12} />
          {t('map.boundaryReference')}
        </span>
      )}
    </div>
  );
}

export interface BoundaryCardProps {
  selection: BoundarySelection | null;
  set: MapBoundarySet | null;
  onClear: () => void;
}

/** The outline the reader clicked: what it is, and nothing more. */
export function BoundaryCard({ selection, set, onClear }: BoundaryCardProps) {
  const t = useT();
  if (!selection || !set) return null;
  return (
    <div className="card mb-4 flex flex-wrap items-center gap-3 p-3 text-sm">
      <span
        className="h-3 w-3 shrink-0 rounded-sm border border-slate-400 bg-slate-400/20"
        aria-hidden="true"
      />
      <span className="font-medium text-slate-800 dark:text-slate-100">
        {selection.name}
      </span>
      <span className="text-slate-500 dark:text-slate-400">
        {set.label} · {selection.code}
      </span>
      {/* Said plainly beside the name, because this is the moment somebody is
          most likely to read a district as a sales area. */}
      <span className="text-xs text-slate-500 dark:text-slate-400">
        {t('map.boundaryNotBusiness')}
      </span>
      <button type="button" className="btn-secondary ml-auto py-1 text-xs" onClick={onClear}>
        <X size={12} />
        {t('common.clear')}
      </button>
    </div>
  );
}
