/**
 * Performance with hierarchy drill-down.
 *
 * Drilling in is a navigation: the clicked entity becomes a filter in the URL
 * and the level moves one step deeper. Because the filter goes to the backend,
 * a drill-down is authorised exactly like any other report — a user cannot
 * reach a branch outside their scope by clicking into it.
 */

import { ChevronRight } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { CategoryBarChart } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { HIERARCHY_ORDER, useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { performanceService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatPercent } from '../utils/format';
import type { FilterLevel } from '../types/api';

/** Drill level -> the filter it sets when you click a row. */
const FILTER_FOR_LEVEL: Record<string, FilterLevel> = {
  zone: 'zone_code',
  region: 'region_code',
  area: 'area_code',
  unit: 'unit_code',
  territory: 'territory_code',
  sub_territory: 'sub_territory_code',
};

const LEVEL_LABELS: Record<string, string> = {
  zone: 'filters.zone',
  region: 'filters.region',
  area: 'filters.area',
  unit: 'filters.unit',
  territory: 'filters.territory',
  sub_territory: 'filters.subTerritory',
  sales_force: 'filters.salesForce',
  material: 'filters.material',
  material_brand: 'filters.materialBrand',
  material_group: 'filters.materialGroup',
  customer: 'filters.customer',
};

export default function PerformancePage() {
  const t = useT();
  const { query, filters, setFilter } = useFilters();
  const [searchParams, setSearchParams] = useSearchParams();
  const level = searchParams.get('level') ?? 'region';

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['performance-page', level, query],
    queryFn: () => performanceService.page({ ...query, level }),
  });

  const rows = data?.performance.rows ?? [];
  const achievementByCode = new Map(
    (data?.achievement?.rows ?? []).map((row) => [row.code, row]),
  );

  function setLevel(next: string) {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous);
        params.set('level', next);
        return params;
      },
      { replace: true },
    );
  }

  function drillInto(row: Record<string, any>) {
    const nextLevel = data?.next_level;
    const filterLevel = FILTER_FOR_LEVEL[level];
    if (!nextLevel || !filterLevel || !row.code || row.code === '(unassigned)') return;
    setFilter(filterLevel, String(row.code));
    setLevel(nextLevel);
  }

  // Breadcrumbs from the filters that are actually set, in hierarchy order.
  const crumbs = HIERARCHY_ORDER.filter((key) => filters[key]).map((key) => ({
    key,
    value: filters[key] as string,
  }));

  const columns = [
    { key: 'label', header: t(LEVEL_LABELS[level] ?? 'common.total') },
    { key: 'quantity', header: t('sales.quantity') },
    { key: 'volume', header: t('sales.volume') },
    { key: 'net_sales', header: t('sales.netSales') },
    {
      key: 'achievement',
      header: t('target.achievement'),
      render: (row: Record<string, any>) => {
        const match = achievementByCode.get(row.code);
        const value = match?.achievement_percent ?? null;
        const below = value !== null && value !== undefined && value < 80;
        return (
          <span className={below ? 'font-semibold text-red-600 dark:text-red-400' : ''}>
            {formatPercent(value)}
          </span>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader
        title={t('performance.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={`${t('performance.title')} – ${level}`}
            rows={rows}
            period={data?.period}
          />
        }
      />

      <GlobalFilterBar />

      <div className="card mb-4 flex flex-wrap items-center gap-2 p-3">
        <span className="text-xs font-medium text-slate-500">{t('performance.level')}</span>
        <div className="flex flex-wrap gap-1">
          {(data?.drill_chain ?? Object.keys(FILTER_FOR_LEVEL)).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setLevel(option)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium ${
                level === option
                  ? 'bg-brand-600 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
              }`}
            >
              {t(LEVEL_LABELS[option] ?? option)}
            </button>
          ))}
          {['sales_force', 'product', 'customer'].map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setLevel(option)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium ${
                level === option
                  ? 'bg-brand-600 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
              }`}
            >
              {t(LEVEL_LABELS[option])}
            </button>
          ))}
        </div>

        {crumbs.length > 0 && (
          <nav
            aria-label="Breadcrumb"
            className="ml-auto flex flex-wrap items-center gap-1 text-xs text-slate-500"
          >
            {crumbs.map((crumb, index) => (
              <span key={crumb.key} className="flex items-center gap-1">
                {index > 0 && <ChevronRight size={12} />}
                <button
                  type="button"
                  className="rounded px-1.5 py-0.5 hover:bg-slate-100 dark:hover:bg-slate-800"
                  onClick={() => setFilter(crumb.key, undefined)}
                  title={t('performance.backTo')}
                >
                  {crumb.value}
                </button>
              </span>
            ))}
          </nav>
        )}
      </div>

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<CardSkeleton rows={6} />}
      >
        <div className="space-y-4">
          <Section title={t('performance.title')}>
            <CategoryBarChart data={rows.slice(0, 12)} xKey="label" yKey="net_sales" />
          </Section>

          <Section
            title={
              data?.next_level
                ? `${t('performance.drillDown')} → ${t(LEVEL_LABELS[data.next_level] ?? data.next_level)}`
                : t('performance.title')
            }
          >
            <DataTable
              rows={rows}
              columns={columns}
              onRowClick={data?.next_level ? drillInto : undefined}
            />
            <ResultNotes notes={data?.performance.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
