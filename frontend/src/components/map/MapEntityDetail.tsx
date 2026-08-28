/**
 * What the reader clicked, and whether it has always looked like that.
 *
 * The map answers "where" and the ranking answers "which"; this answers "what
 * is this one, exactly". It draws an entity's identity, its metric as the map
 * measured it, and a short monthly history — the question a single period
 * cannot answer, and the reason this panel needs an endpoint of its own.
 *
 * It computes nothing. The metric is the value the entity carried in the
 * collection the map drew; the months are rows `get_sales_trend` aggregated
 * server-side. The only arithmetic here is turning a month's sales into a bar
 * height as a fraction of the tallest month on screen, which is a drawing
 * instruction and not a business figure — it is deliberately *not* labelled as
 * a percentage of anything, because it is a proportion of an arbitrary maximum
 * rather than of a target.
 *
 * **There is no target line behind the bars.** `fact_target` records a target
 * month and a financial year rather than a date, and nothing on the tool path
 * groups it by month, so a monthly target would have to be invented here. The
 * caption says the bars are actuals rather than leaving the reader to assume
 * they are compared against something.
 */

import type { ReactNode } from 'react';
import { useT } from '../../contexts/I18nContext';
import { formatAmount } from '../../utils/format';
import type { MapEntityTrendResponse } from '../../types/api';
import type { EntityFeatureProperties } from './businessGeoJson';

export interface MapEntityDetailProps {
  /**
   * The clicked entity, as the map drew it.
   *
   * The adapter's own type rather than a restatement of its shape: a panel that
   * described the fields it wanted would drift the first time a field was
   * added or made nullable, which is exactly what it did on the first compile.
   */
  entity: EntityFeatureProperties;
  /** How the page formats this map's metric — currency or a count. */
  formatValue: (value: number | null) => string;
  /** The metric's own name, as the server labelled it. */
  metricLabel?: string;
  trend: MapEntityTrendResponse | undefined;
  trendLoading: boolean;
  /** Link to the record behind this entity, where one exists and is permitted. */
  action?: ReactNode;
}

/** A month's bar, as a fraction of the tallest month drawn. */
function heightPercent(value: number, max: number): number {
  if (max <= 0) return 0;
  // A floor of 2% so a real but tiny month is still visibly a bar rather than
  // reading as a month with no sales at all.
  return Math.max(2, Math.round((value / max) * 100));
}

export function MapEntityDetail({
  entity,
  formatValue,
  metricLabel,
  trend,
  trendLoading,
  action,
}: MapEntityDetailProps) {
  const t = useT();

  const months = (trend?.rows ?? []).map((row) => ({
    label: String(row.label ?? row.date ?? ''),
    value: Number(row.net_sales ?? 0),
  }));
  const max = months.reduce((highest, month) => Math.max(highest, month.value), 0);

  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-slate-400">
        {entity.entityType?.replace(/_/g, ' ')}
      </p>
      <p className="text-sm font-semibold leading-tight">{entity.name}</p>
      <p className="text-[11px] text-slate-500">{entity.code}</p>
      {entity.parentCode && (
        <p className="text-[11px] text-slate-400">
          {entity.parentType?.replace(/_/g, ' ')}: {entity.parentCode}
        </p>
      )}

      <div className="mt-3 flex items-end justify-between gap-2">
        <div>
          <p className="text-[10px] font-medium uppercase tracking-wide text-slate-500">
            {metricLabel}
          </p>
          <p className="text-xl font-semibold tabular-nums">{formatValue(entity.value)}</p>
          {/* A dash is this application's word for "not available", but this is
              the level the map opens on, so the reason is worth a line rather
              than leaving the reader to decide whether the dealer sold nothing
              or was simply never scored. */}
          {entity.value === null && (
            <p className="mt-0.5 max-w-[14rem] text-[10px] leading-relaxed text-slate-400">
              {t('map.metricNotScored')}
            </p>
          )}
        </div>
        <p className="text-[11px] tabular-nums text-slate-400">
          {entity.latitude.toFixed(4)}, {entity.longitude.toFixed(4)}
        </p>
      </div>

      {/* ---- history ---- */}
      <p className="mt-4 text-[10px] font-medium uppercase tracking-wide text-slate-500">
        {t('map.history')}
      </p>

      {trendLoading && (
        <p className="mt-1 text-[11px] text-slate-400">{t('common.loading')}</p>
      )}

      {!trendLoading && trend?.error && (
        <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">{trend.error}</p>
      )}

      {!trendLoading && !trend?.error && months.length === 0 && (
        <p className="mt-1 text-[11px] text-slate-400">{t('map.historyEmpty')}</p>
      )}

      {months.length > 0 && (
        <>
          <div className="mt-2 flex h-16 items-end gap-1">
            {months.map((month) => (
              <div
                key={month.label}
                className="flex min-w-0 flex-1 flex-col items-center gap-1"
                // Native tooltip rather than a popover: this is a glance, and
                // the figure is the one the reports state.
                title={`${month.label}: ${formatAmount(month.value)}`}
              >
                <div className="flex h-12 w-full items-end rounded-sm bg-slate-100 dark:bg-slate-800">
                  <span
                    className="block w-full rounded-sm bg-brand-500"
                    style={{ height: `${heightPercent(month.value, max)}%` }}
                  />
                </div>
                <span className="w-full truncate text-center text-[9px] text-slate-400">
                  {month.label}
                </span>
              </div>
            ))}
          </div>
          {/* Says what the bars are. Without this a reader fills the gap with
              the assumption every other chart on this page invites — that the
              grey behind a bar is a target. It is an empty track. */}
          <p className="mt-1.5 text-[10px] leading-relaxed text-slate-400">
            {t('map.historyActualsOnly')}
          </p>
        </>
      )}

      {action}
    </div>
  );
}
