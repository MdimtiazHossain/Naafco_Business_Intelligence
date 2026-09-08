/**
 * Material Analysis: the one page that still ranks individual materials.
 *
 * General performance elsewhere in the app is brand-wise, because "top
 * products" almost always means "which of our lines are selling". This page is
 * where the finer question is asked deliberately, so it keeps all three levels
 * of the Material Master — material, brand and group — and clicking a brand
 * opens its breakdown.
 */

import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { CategoryBarChart, TrendChart, trendSeriesFrom } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { materialService } from '../services';
import { DataTable, renderTotalValue } from '../tables/DataTable';
import { growthNumerator, percentOfTotals, sumColumn } from '../tables/totals';
import type { MaterialAnalysisLevel } from '../types/api';

const LEVELS: MaterialAnalysisLevel[] = [
  'material',
  'material_brand',
  'material_group',
];

const LEVEL_LABELS: Record<MaterialAnalysisLevel, string> = {
  material: 'materials.levelMaterial',
  material_brand: 'materials.levelBrand',
  material_group: 'materials.levelGroup',
};

export default function MaterialsPage() {
  const t = useT();
  const { query, setFilter } = useFilters();
  const [searchParams, setSearchParams] = useSearchParams();

  const level = (LEVELS.find((candidate) => candidate === searchParams.get('level')) ??
    'material') as MaterialAnalysisLevel;

  const setLevel = (next: MaterialAnalysisLevel) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous);
        if (next === 'material') params.delete('level');
        else params.set('level', next);
        return params;
      },
      { replace: true },
    );
  };

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['materials-page', level, query],
    queryFn: () => materialService.page({ ...query, level, limit: 100 }),
  });

  const rows = data?.materials.rows ?? [];
  const detail = data?.brand_detail ?? null;

  // No stock columns, even though sales and stock now share the Material Code.
  // A stock position carries no posting date, so its figure is today's whatever
  // period this page is showing — putting it in a period's table would read as a
  // measurement of that period. Stock questions belong on the Stock page.
  const materialColumns = [
    { key: 'code', header: t('filters.materialCode') },
    { key: 'label', header: t('filters.material') },
    { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
    { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
    { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
    {
      key: 'growth_percent',
      header: t('common.growth'),
      // Recomputed from the two totals, never averaged: a material that
      // grew 400% on eight hundred taka would otherwise pull the column
      // as hard as one that grew 4% on four crore. `previous_net_sales`
      // travels on every row for exactly this (routes_pages.materials).
      total: (totalled: Record<string, any>[]) =>
        renderTotalValue(
          percentOfTotals(
            growthNumerator(totalled),
            sumColumn(totalled, 'previous_net_sales'),
          ),
          'growth_percent',
          t,
        ),
    },
  ];

  const brandColumns = [
    { key: 'rank', header: t('common.rank') },
    { key: 'label', header: t('filters.materialBrand') },
    { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
    { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
    { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
    {
      key: 'growth_percent',
      header: t('common.growth'),
      // Recomputed from the two totals, never averaged: a material that
      // grew 400% on eight hundred taka would otherwise pull the column
      // as hard as one that grew 4% on four crore. `previous_net_sales`
      // travels on every row for exactly this (routes_pages.materials).
      total: (totalled: Record<string, any>[]) =>
        renderTotalValue(
          percentOfTotals(
            growthNumerator(totalled),
            sumColumn(totalled, 'previous_net_sales'),
          ),
          'growth_percent',
          t,
        ),
    },
  ];

  const groupColumns = [
    { key: 'label', header: t('filters.materialGroup') },
    { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
    { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
    { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
    {
      key: 'growth_percent',
      header: t('common.growth'),
      // Recomputed from the two totals, never averaged: a material that
      // grew 400% on eight hundred taka would otherwise pull the column
      // as hard as one that grew 4% on four crore. `previous_net_sales`
      // travels on every row for exactly this (routes_pages.materials).
      total: (totalled: Record<string, any>[]) =>
        renderTotalValue(
          percentOfTotals(
            growthNumerator(totalled),
            sumColumn(totalled, 'previous_net_sales'),
          ),
          'growth_percent',
          t,
        ),
    },
  ];

  const columns =
    level === 'material'
      ? materialColumns
      : level === 'material_brand'
        ? brandColumns
        : groupColumns;

  const nameHeader =
    level === 'material'
      ? t('filters.material')
      : level === 'material_brand'
        ? t('filters.materialBrand')
        : t('filters.materialGroup');

  return (
    <>
      <PageHeader
        title={t('materials.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={`${t('materials.title')} – ${t(LEVEL_LABELS[level])}`}
            rows={rows}
            period={data?.period}
          />
        }
      />

      <GlobalFilterBar />

      <div
        className="mb-4 flex flex-wrap gap-1"
        role="group"
        aria-label={t('materials.analysisLevel')}
      >
        {LEVELS.map((candidate) => (
          <button
            key={candidate}
            type="button"
            onClick={() => setLevel(candidate)}
            aria-pressed={level === candidate}
            className={`tap-y rounded-lg px-3 py-1.5 text-xs font-medium ${
              level === candidate
                ? 'bg-brand-600 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
            }`}
          >
            {t(LEVEL_LABELS[candidate])}
          </button>
        ))}
      </div>

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<CardSkeleton rows={6} />}
        isEmpty={rows.length === 0 && !detail}
      >
        <div className="space-y-4">
          {detail && (
            <Section title={`${t('materials.brandAnalysis')}: ${detail.brand}`}>
              <div className="space-y-4">
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => setFilter('material_brand', undefined)}
                >
                  {t('materials.clearBrand')}
                </button>

                <TrendChart
                  data={detail.monthly_trend.rows ?? []}
                  xKey="label"
                  series={trendSeriesFrom(detail.monthly_trend.chart,
                                          t('sales.netSales'), t('kpi.target'))}
                  area
                />

                <div className="grid gap-4 lg:grid-cols-2">
                  <Section title={t('materials.byGroup')}>
                    <DataTable
                      tableId="materials.detail.groups"
                      rows={detail.groups.rows ?? []}
                      columns={groupColumns}
                      searchable={false}
                      pageSize={10}
                      rowKey={(row, index) => `group-${row.code ?? index}`}
                    />
                  </Section>
                  <Section title={t('materials.byMaterial')}>
                    <DataTable
                      tableId="materials.detail.materials"
                      rows={detail.materials.rows ?? []}
                      columns={[
                        { key: 'code', header: t('filters.materialCode') },
                        { key: 'label', header: t('filters.material') },
                        { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
                        { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
                        { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
                      ]}
                      searchable={false}
                      pageSize={10}
                      rowKey={(row, index) => `mat-${row.code ?? index}`}
                    />
                  </Section>
                  <Section title={t('sales.byTerritory')}>
                    <DataTable
                      tableId="materials.detail.territories"
                      rows={detail.territories.rows ?? []}
                      columns={[
                        { key: 'label', header: t('filters.territory') },
                        { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
                        { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
                        { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
                      ]}
                      searchable={false}
                      pageSize={10}
                      rowKey={(row, index) => `terr-${row.code ?? index}`}
                    />
                  </Section>
                  <Section title={t('sales.byCustomer')}>
                    <DataTable
                      tableId="materials.detail.customers"
                      rows={detail.customers.rows ?? []}
                      columns={[
                        { key: 'label', header: t('filters.customer') },
                        { key: 'quantity', header: t('sales.quantity'), total: 'sum' as const },
                        { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
                        { key: 'net_sales', header: t('sales.netSales'), total: 'sum' as const },
                      ]}
                      searchable={false}
                      pageSize={10}
                      rowKey={(row, index) => `cust-${row.code ?? index}`}
                    />
                  </Section>
                </div>
                <ResultNotes notes={detail.summary.notes} />
              </div>
            </Section>
          )}

          <Section title={t('materials.top10')}>
            <CategoryBarChart
              data={data?.top ?? []}
              xKey="label"
              yKey="volume"
              valueKind="quantity"
              valueLabel={t('sales.volume')}
            />
          </Section>

          <div className="grid gap-4 lg:grid-cols-2">
            <Section title={t('materials.top10')}>
              <DataTable
                tableId="materials.top"
                rows={data?.top ?? []}
                columns={[
                  { key: 'label', header: nameHeader },
                  { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
                  { key: 'volume_growth_percent', header: t('common.growth') },
                ]}
                searchable={false}
                pageSize={10}
                rowKey={(row, index) => `top-${row.code ?? index}`}
              />
            </Section>
            <Section title={t('materials.bottom10')}>
              <DataTable
                tableId="materials.bottom"
                rows={data?.bottom ?? []}
                columns={[
                  { key: 'label', header: nameHeader },
                  { key: 'volume', header: t('sales.volume'), total: 'sum' as const },
                  { key: 'volume_growth_percent', header: t('common.growth') },
                ]}
                searchable={false}
                pageSize={10}
                rowKey={(row, index) => `bottom-${row.code ?? index}`}
              />
            </Section>
          </div>

          <Section title={t('materials.title')}>
            <DataTable
              // The three levels declare different columns, so they cannot share one
              // arrangement.
              tableId={`materials.${level}`}
              rows={rows}
              columns={columns}
              pageSize={25}
              rowKey={(row, index) => String(row.code ?? index)}
              onRowClick={
                level === 'material_brand'
                  ? (row) => setFilter('material_brand', String(row.code))
                  : undefined
              }
            />
            <ResultNotes notes={data?.materials.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
