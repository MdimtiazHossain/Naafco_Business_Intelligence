/**
 * KPI card: current value, comparison and growth.
 *
 * Every number displayed here was computed by the backend. The card only
 * formats and colours it.
 */

import { Minus, TrendingDown, TrendingUp } from 'lucide-react';
import type { ReactNode } from 'react';
import { useT } from '../contexts/I18nContext';
import { formatByKind, formatPercent, growthClass, stockStatusClass } from '../utils/format';
import type { Kpi } from '../types/api';

export function KpiCard({
  kpi,
  icon,
  compareLabel,
}: {
  kpi: Kpi;
  icon?: ReactNode;
  compareLabel?: string;
}) {
  const t = useT();
  const growth = kpi.growth_percent;
  const label = t(`kpi.${kpi.key}`) === `kpi.${kpi.key}` ? kpi.label : t(`kpi.${kpi.key}`);
  // A stock status colours the whole metric — the name and the figure — so the
  // card reads as one green or amber thing. Keyed on `kpi.key`, so the backend
  // naming the measure is what decides, not the translated wording.
  const status = stockStatusClass(kpi.key);

  const TrendIcon =
    growth === null || growth === undefined || growth === 0
      ? Minus
      : growth > 0
        ? TrendingUp
        : TrendingDown;

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-2">
        <p
          className={`text-xs font-medium uppercase tracking-wide ${
            status ?? 'text-slate-500 dark:text-slate-400'
          }`}
        >
          {label}
        </p>
        {icon && (
          <span className={status ?? 'text-slate-400 dark:text-slate-500'}>{icon}</span>
        )}
      </div>

      {/*
        A step smaller below `sm`.

        A stock figure runs to eleven or twelve grouped digits, and at
        `text-2xl` that is about 145px of tabular numerals — wider than a KPI
        card gets on a phone, so the number ran past the edge of its own card.
        A figure must never be clipped or truncated, so the type gives way
        instead; from `sm` up the card has the room and keeps the size it had.
      */}
      <p
        className={`mt-2 text-xl font-semibold tabular-nums sm:text-2xl ${
          status ?? 'text-slate-900 dark:text-slate-50'
        }`}
      >
        {formatByKind(kpi.value, kpi.format, kpi.unit)}
      </p>

      {(growth !== null && growth !== undefined) || kpi.previous_value !== null ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          {growth !== null && growth !== undefined && (
            <span className={`inline-flex items-center gap-1 font-medium ${growthClass(growth)}`}>
              <TrendIcon size={14} />
              {formatPercent(growth, { signed: true })}
            </span>
          )}
          <span className="text-slate-500 dark:text-slate-400">
            {compareLabel ?? t('common.vsPrevious')}
          </span>
          {kpi.previous_value !== null && kpi.previous_value !== undefined && (
            <span className="tabular-nums text-slate-500 dark:text-slate-400">
              ({formatByKind(kpi.previous_value, kpi.format, kpi.unit)})
            </span>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** A simple metric tile for page-level KPIs that carry no comparison. */
export function StatCard({
  label,
  value,
  hint,
  tone = 'default',
  statusKey,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'default' | 'warning' | 'danger' | 'success';
  /**
   * A material stock measure or shelf-life bucket — `unrestricted_stock`,
   * `EXPIRING_SOON`, … — when this card carries one.
   *
   * A status colours the label and the value together and overrides `tone`,
   * which colours the figure alone. Both exist on purpose: `tone` is a hint
   * about one number (a non-zero blocked figure reads as a problem), while a
   * status is a standard applied to the complete metric wherever it appears.
   */
  statusKey?: string;
}) {
  const tones: Record<string, string> = {
    default: 'text-slate-900 dark:text-slate-50',
    warning: 'text-amber-600 dark:text-amber-400',
    danger: 'text-red-600 dark:text-red-400',
    success: 'text-emerald-600 dark:text-emerald-400',
  };
  const status = stockStatusClass(statusKey);
  return (
    <div className="card p-4">
      <p
        className={`text-xs font-medium uppercase tracking-wide ${
          status ?? 'text-slate-500 dark:text-slate-400'
        }`}
      >
        {label}
      </p>
      {/* Same step down as `KpiCard`, and for the same reason. */}
      <p
        className={`mt-2 text-xl font-semibold tabular-nums sm:text-2xl ${
          status ?? tones[tone]
        }`}
      >
        {value}
      </p>
      {/* The supporting count stays neutral: it is metadata about the metric,
          not the metric, and colouring it too would flatten the pairing above
          into a block of one colour. */}
      {hint && <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  );
}
