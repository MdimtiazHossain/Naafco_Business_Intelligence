/**
 * Choosing an administrative backdrop, and reading the outline you clicked.
 *
 * One component for both maps: the picker and the selected-outline card are
 * identical on the Business Map and on Area Demarcation, because a district is
 * a district on either. Written here once rather than twice for the same
 * reason `useBoundaryLayer` is one hook.
 *
 * **The sizes are shown, not hidden.** The upazila file is 1.7 MB, and a
 * control that silently stalls for several seconds on a slow connection reads
 * as broken. The figure comes from the server's own catalogue, so it cannot
 * drift from what is actually fetched.
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

/** Bytes as a figure a reader can weigh a download against. */
function readableSize(bytes: number): string {
  return bytes >= 1_000_000
    ? `${(bytes / 1_000_000).toFixed(1)} MB`
    : `${Math.round(bytes / 1000)} KB`;
}

/** Above this, the size is worth putting in front of somebody before they wait. */
const WARN_BYTES = 500_000;

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
          <option key={set.key} value={set.key}>
            {set.bytes >= WARN_BYTES
              ? `${set.label} (${readableSize(set.bytes)})`
              : set.label}
          </option>
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
