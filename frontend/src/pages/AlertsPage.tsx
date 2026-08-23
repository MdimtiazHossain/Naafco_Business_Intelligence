/**
 * Alert centre.
 *
 * Alerts are computed live by the Phase 3 alert tool within the caller's scope,
 * and each carries the report it should be investigated in.
 */

import { ArrowUpRight } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ExportButtons } from '../components/ExportButtons';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { alertService } from '../services';
import { severityClass } from '../utils/format';
import type { Severity } from '../types/api';

const SEVERITIES: Severity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
const CATEGORIES = ['SALES', 'TARGET', 'STOCK'];

export default function AlertsPage() {
  const t = useT();
  const navigate = useNavigate();
  const { query } = useFilters();
  const [severity, setSeverity] = useState<string | undefined>();
  const [category, setCategory] = useState<string | undefined>();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['alerts-page', query, severity, category],
    queryFn: () => alertService.list({ ...query, severity, category }),
  });

  const alerts = data?.alerts ?? [];
  const counts = data?.counts_by_severity ?? {};

  return (
    <>
      <PageHeader
        title={t('alerts.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={t('alerts.title')}
            rows={alerts as unknown as Record<string, unknown>[]}
            period={data?.period}
          />
        }
      />

      <GlobalFilterBar />

      <div className="card mb-4 flex flex-wrap items-center gap-2 p-3">
        <span className="text-xs font-medium text-slate-500">{t('alerts.severity')}</span>
        <button
          type="button"
          onClick={() => setSeverity(undefined)}
          className={`rounded-lg px-2.5 py-1 text-xs ${
            !severity ? 'bg-brand-600 text-white' : 'bg-slate-100 dark:bg-slate-800'
          }`}
        >
          {t('common.all')}
        </button>
        {SEVERITIES.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setSeverity(value)}
            className={`rounded-lg px-2.5 py-1 text-xs ${
              severity === value
                ? 'bg-brand-600 text-white'
                : `${severityClass(value)}`
            }`}
          >
            {value} {counts[value] ? `(${counts[value]})` : ''}
          </button>
        ))}

        <span className="ml-3 text-xs font-medium text-slate-500">
          {t('alerts.category')}
        </span>
        <select
          className="input w-auto py-1 text-xs"
          value={category ?? ''}
          onChange={(event) => setCategory(event.target.value || undefined)}
        >
          <option value="">{t('common.all')}</option>
          {CATEGORIES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<CardSkeleton rows={5} />}
      >
        {alerts.length === 0 ? (
          <Section title={t('alerts.title')}>
            <EmptyState message={t('alerts.none')} />
          </Section>
        ) : (
          <div className="space-y-2">
            {alerts.map((alert, index) => (
              <div
                key={`${alert.alert_type}-${alert.entity}-${index}`}
                className="card flex flex-wrap items-start gap-3 p-4"
              >
                <span className={`badge ${severityClass(alert.severity)}`}>
                  {alert.severity}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-800 dark:text-slate-100">
                    {alert.entity} · {alert.alert_type.replace(/_/g, ' ')}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {alert.metric.replace(/_/g, ' ')}:{' '}
                    <span className="tabular-nums">
                      {alert.current_value?.toFixed?.(1) ?? alert.current_value}
                    </span>{' '}
                    ({t('alerts.threshold')}: {alert.threshold})
                  </p>
                  <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                    {alert.recommended_attention}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-secondary shrink-0"
                  onClick={() => navigate(alert.link)}
                >
                  {t('alerts.viewReport')}
                  <ArrowUpRight size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </QueryState>
    </>
  );
}
