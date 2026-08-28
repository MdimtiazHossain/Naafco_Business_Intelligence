/**
 * Where every plan has got to, and what is waiting on somebody.
 *
 * **Every figure here is either a count of workflow state or a number somebody
 * typed.** No scoped aggregate and no achievement percentage: achievement
 * against a locked target is the Target page's question, and a dashboard that
 * recomputed a figure another screen already reports is how two screens come to
 * disagree.
 *
 * **"Needs attention" always says why.** Each reason is a sentence from the
 * backend, so the screen never shows a red count a reader opens and cannot act
 * on.
 *
 * **A missing country target reads `n/a`, not 0.** Nobody having typed a target
 * and somebody having typed nothing are different statements, and only one of
 * them is a number.
 */

import { TriangleAlert } from 'lucide-react';
import { StatCard } from '../KpiCard';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { formatDateTime, formatQuantity } from '../../utils/format';
import type { TargetDashboardResponse, TargetDashboardPlan } from '../../types/api';

function actionLabel(value: string): string {
  return value
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function TargetDashboard({
  data,
  financialYear,
  onFinancialYearChange,
  onOpenPlan,
}: {
  data: TargetDashboardResponse;
  financialYear: string | null;
  onFinancialYearChange: (year: string | null) => void;
  onOpenPlan: (planId: number, tab: string) => void;
}) {
  const t = useT();

  const planRow = (entry: TargetDashboardPlan) => (
    <li
      key={entry.version.version_id}
      className="rounded-md border border-slate-200 px-3 py-2.5 dark:border-slate-700"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <button
            type="button"
            onClick={() => onOpenPlan(entry.plan.plan_id, 'country')}
            className="font-medium text-blue-600 hover:underline dark:text-blue-400"
          >
            {entry.plan.plan_code}
          </button>
          <span className="ml-2 text-sm text-slate-500 dark:text-slate-400">
            {entry.plan.financial_year} · {entry.plan.target_period} · V
            {entry.version.version_no}
          </span>
        </div>
        <span className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
          {entry.version.status.replace(/_/g, ' ')}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-6 gap-y-1 text-sm tabular-nums">
        <span className="text-slate-600 dark:text-slate-400">
          {t('targetMgmt.dashboard.countryTarget')}{' '}
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {entry.country_volume === null
              ? 'n/a'
              : formatQuantity(entry.country_volume)}
          </span>
        </span>
        <span className="text-slate-600 dark:text-slate-400">
          {t('targetMgmt.dashboard.allocated')}{' '}
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {entry.allocated_volume === null
              ? 'n/a'
              : formatQuantity(entry.allocated_volume)}
          </span>
          {entry.allocated_percent !== null && (
            <span className="ml-1 text-slate-500 dark:text-slate-400">
              ({entry.allocated_percent.toFixed(0)}%)
            </span>
          )}
        </span>
        {entry.open_revisions > 0 && (
          <span className="text-amber-700 dark:text-amber-400">
            {t('targetMgmt.dashboard.openRevisions', {
              count: String(entry.open_revisions),
            })}
          </span>
        )}
        {entry.locked_at && (
          <span className="text-emerald-700 dark:text-emerald-400">
            {t('targetMgmt.dashboard.lockedOn', {
              when: formatDateTime(entry.locked_at),
            })}
          </span>
        )}
      </div>
      {entry.attention.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {entry.attention.map((reason) => (
            <li
              key={reason}
              className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400"
            >
              <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{data.attention_reasons[reason] ?? reason}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select
          className="input"
          aria-label={t('targetMgmt.col.financialYear')}
          value={financialYear ?? ''}
          onChange={(event) => onFinancialYearChange(event.target.value || null)}
        >
          <option value="">{t('targetMgmt.dashboard.allYears')}</option>
          {data.financial_years.map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>
        <span className="text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.dashboard.planCount', {
            count: String(data.plan_count),
          })}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label={t('targetMgmt.dashboard.stage.drafting')}
          value={String(data.stages.drafting)}
        />
        <StatCard
          label={t('targetMgmt.dashboard.stage.allocated')}
          value={String(data.stages.allocated)}
        />
        <StatCard
          label={t('targetMgmt.dashboard.stage.in_approval')}
          value={String(data.stages.in_approval)}
        />
        <StatCard
          label={t('targetMgmt.dashboard.stage.locked')}
          value={String(data.stages.locked)}
        />
      </div>

      <Section title={t('targetMgmt.dashboard.queueTitle')}>
        {data.my_queue.versions === 0 && data.my_queue.revisions === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            {data.my_queue.notes[0] ?? t('targetMgmt.queue.emptyMessage')}
          </p>
        ) : (
          <p className="text-sm text-slate-700 dark:text-slate-300">
            {t('targetMgmt.dashboard.queueSummary', {
              versions: String(data.my_queue.versions),
              revisions: String(data.my_queue.revisions),
            })}
          </p>
        )}
      </Section>

      {data.attention.length > 0 && (
        <Section title={t('targetMgmt.dashboard.attentionTitle')}>
          <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
            {t('targetMgmt.dashboard.attentionDescription')}
          </p>
          <ul className="space-y-2">{data.attention.map(planRow)}</ul>
        </Section>
      )}

      <Section title={t('targetMgmt.dashboard.plansTitle')}>
        {data.plans.length === 0 ? (
          <EmptyState message={t('targetMgmt.dashboard.noPlans')} />
        ) : (
          <ul className="space-y-2">{data.plans.map(planRow)}</ul>
        )}
      </Section>

      {data.recent.length > 0 && (
        <Section title={t('targetMgmt.dashboard.recentTitle')}>
          <ul className="space-y-1.5 text-sm">
            {data.recent.map((entry) => (
              <li key={entry.audit_id} className="flex flex-wrap gap-x-2">
                <span className="font-medium text-slate-900 dark:text-slate-100">
                  {actionLabel(entry.action)}
                </span>
                <span className="text-slate-500 dark:text-slate-400">
                  {entry.node_label ?? '—'} · {entry.actor ?? '—'}
                </span>
                {entry.occurred_at && (
                  <span className="text-xs text-slate-400 dark:text-slate-500">
                    {formatDateTime(entry.occurred_at)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
