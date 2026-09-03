/**
 * The pre-flight gate: what an allocation would find, before it runs.
 *
 * **This screen exists so a limitation is never a surprise.** Discovering that
 * no customer carries a sub-territory halfway through a background job is the
 * worst possible time to discover it — the version has moved, a worker has read
 * two years of sales, and the planner watching the bar has to be told the run
 * was never going to reach the level they wanted.
 *
 * **Each line shows one of five data states, not a tick or a cross.** "No sales
 * loaded", "sales loaded but none states a volume", "customers present but
 * unmapped", "a customer naming a sub-territory that does not exist" and "no
 * potential master exists in the platform" are five different situations with
 * five different fixes, and a boolean cannot tell them apart. The backend
 * decides which; this draws what it was told.
 *
 * A line can be absent-but-not-blocking. A missing Conversion Factor is
 * honestly `NO_DATA` and does *not* stop the run, because the engine allocates
 * volume and volume needs neither derivation input — so it is drawn as advice
 * rather than as a barrier.
 */

import { AlertTriangle, Ban, Check, CircleSlash, Info } from 'lucide-react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import { formatQuantity } from '../../utils/format';
import type { TargetReadiness, TargetReadinessCheck } from '../../types/api';

/**
 * How each tone reads.
 *
 * Colour is never the only signal — every line carries its own sentence and its
 * own icon — but a reader scanning nine checks needs to see at a glance which
 * ones need them.
 */
const TONE: Record<string, { text: string; icon: typeof Check }> = {
  ok: { text: 'text-emerald-700 dark:text-emerald-400', icon: Check },
  blocked: { text: 'text-amber-700 dark:text-amber-400', icon: AlertTriangle },
  error: { text: 'text-red-700 dark:text-red-400', icon: Ban },
  muted: { text: 'text-slate-400 dark:text-slate-500', icon: CircleSlash },
};

function CheckRow({ check }: { check: TargetReadinessCheck }) {
  const t = useT();
  // An advisory line is drawn muted rather than amber: the data really is
  // absent, and the run really does proceed, so alarming the reader would be
  // as misleading as hiding it.
  const tone = TONE[check.advisory && !check.ok ? 'muted' : check.tone] ?? TONE.muted;
  const Icon = tone.icon;
  return (
    <li className="flex gap-2 py-1.5">
      <Icon size={16} className={`mt-0.5 shrink-0 ${tone.text}`} />
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium text-slate-800 dark:text-slate-100">
            {check.label}
          </span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${tone.text}`}
          >
            {t(`targetMgmt.dataState.${check.state}`)}
          </span>
          {check.advisory && !check.ok && (
            <span className="text-[10px] italic text-slate-400 dark:text-slate-500">
              {t('targetMgmt.advisory')}
            </span>
          )}
        </div>
        <p className="text-xs text-slate-600 dark:text-slate-300">{check.detail}</p>
        {check.action && (
          <p className="mt-0.5 text-xs italic text-slate-500 dark:text-slate-400">
            {check.action}
          </p>
        )}
      </div>
    </li>
  );
}

export function ReadinessGate({ readiness }: { readiness: TargetReadiness }) {
  const t = useT();
  const mapping = readiness.customer_mapping;
  const projection = readiness.projection;

  return (
    <Section
      title={t('targetMgmt.readinessTitle')}
      actions={
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
            readiness.ready
              ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300'
              : 'bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300'
          }`}
        >
          {readiness.ready ? t('targetMgmt.ready') : t('targetMgmt.notReady')}
        </span>
      }
    >
      <ul className="divide-y divide-slate-100 dark:divide-slate-800">
        {readiness.checks.map((check) => (
          <CheckRow key={check.key} check={check} />
        ))}
      </ul>

      {/*
        The level a run would reach, stated up front. A planner who asked for a
        customer-level target and will get a sub-territory one has to know
        before the run, not from a footnote afterwards.
      */}
      <div
        className={`mt-4 rounded-lg border p-3 ${
          readiness.allocation_level_is_customer
            ? 'border-emerald-200 bg-emerald-50/50 dark:border-emerald-900 dark:bg-emerald-950/20'
            : 'border-amber-200 bg-amber-50/50 dark:border-amber-900 dark:bg-amber-950/20'
        }`}
      >
        <p className="text-sm font-semibold text-slate-800 dark:text-slate-100">
          {t('targetMgmt.allocationLevelIs', {
            level: t(`targetMgmt.level.${readiness.allocation_level}`),
          })}
        </p>
        {!readiness.allocation_level_is_customer && (
          <p className="mt-1 text-xs text-amber-800 dark:text-amber-300">
            {t('targetMgmt.cannotReachCustomer')}
          </p>
        )}
      </div>

      {mapping.total_customers > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            {t('targetMgmt.mappingHealth')}
          </h4>
          <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Fact label={t('targetMgmt.totalCustomers')}
                  value={formatQuantity(mapping.total_customers)} />
            <Fact label={t('targetMgmt.mappedCustomers')}
                  value={formatQuantity(mapping.mapped_to_sub_territory)}
                  tone={mapping.mapped_to_sub_territory > 0 ? 'ok' : 'bad'} />
            <Fact label={t('targetMgmt.unmappedCustomers')}
                  value={formatQuantity(mapping.unmapped)}
                  tone={mapping.unmapped > 0 ? 'bad' : 'ok'} />
            <Fact label={t('targetMgmt.danglingCustomers')}
                  value={formatQuantity(mapping.mapped_to_unknown_sub_territory)}
                  tone={mapping.mapped_to_unknown_sub_territory > 0 ? 'bad' : 'ok'} />
          </dl>
        </div>
      )}

      <div className="mt-4">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          {t('targetMgmt.projection')}
        </h4>
        <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label={t('targetMgmt.projectedRows')}
                value={formatQuantity(projection.projected_rows)}
                tone={projection.exceeds ? 'bad' : undefined} />
          <Fact label={t('targetMgmt.maximumRows')}
                value={formatQuantity(projection.maximum_rows)} />
          {/*
            * Capacity and memory are shown before the run rather than after it
            * fails. A ceiling is a technical limit, so what a reader needs is
            * how much of it this plan uses — not only whether it fits.
            */}
          <Fact label={t('targetMgmt.availableRows')}
                value={
                  projection.available_rows < 0
                    ? `−${formatQuantity(Math.abs(projection.available_rows))}`
                    : formatQuantity(projection.available_rows)
                }
                tone={projection.exceeds ? 'bad' : undefined} />
          <Fact label={t('targetMgmt.capacityUsed')}
                value={
                  projection.capacity_used_percent === null
                    ? 'n/a'
                    : `${projection.capacity_used_percent.toFixed(1)}%`
                }
                tone={projection.exceeds ? 'bad' : undefined} />
          <Fact label={t('targetMgmt.estimatedMemory')}
                value={`${formatQuantity(projection.estimated_memory_mb)} MB`} />
          <Fact label={t('targetMgmt.materialCount')}
                value={formatQuantity(projection.materials)} />
          <Fact label={t('targetMgmt.monthCount')}
                value={formatQuantity(projection.months)} />
          <Fact label={t('targetMgmt.nodeCount')}
                value={formatQuantity(projection.nodes)} />
        </dl>
        {projection.exceeds && (
          <div
            role="alert"
            className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300"
          >
            <p className="font-semibold">{t('targetMgmt.exceedsLimit')}</p>
            <p className="mt-1">
              {t('targetMgmt.narrowBy', {
                options: projection.narrowing_options
                  .map((option) => t(`targetMgmt.narrow.${option}`))
                  .join(', '),
              })}
            </p>
          </div>
        )}
      </div>
    </Section>
  );
}

function Fact({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'ok' | 'bad';
}) {
  return (
    <div>
      <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
      <dd
        className={`text-sm font-semibold tabular-nums ${
          tone === 'bad'
            ? 'text-red-700 dark:text-red-300'
            : tone === 'ok'
              ? 'text-emerald-700 dark:text-emerald-300'
              : 'text-slate-900 dark:text-slate-50'
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

/** A short banner for the top of the allocation tab. */
export function ReadinessSummary({ readiness }: { readiness: TargetReadiness }) {
  const t = useT();
  if (readiness.ready) return null;
  return (
    <div
      role="status"
      className="mt-4 flex gap-2 rounded-lg border border-amber-200 bg-amber-50/60 p-3 dark:border-amber-900 dark:bg-amber-950/20"
    >
      <Info size={18} className="mt-0.5 shrink-0 text-amber-700 dark:text-amber-400" />
      <div>
        <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">
          {t('targetMgmt.notReadyTitle')}
        </p>
        <p className="mt-0.5 text-xs text-amber-900/80 dark:text-amber-200/80">
          {t('targetMgmt.notReadyBody', {
            count: String(readiness.blocking.length),
          })}
        </p>
      </div>
    </div>
  );
}
