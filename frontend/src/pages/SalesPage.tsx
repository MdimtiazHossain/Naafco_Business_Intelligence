/**
 * Sales page: KPIs, trends, target comparison, four performance tables and the
 * server-paginated transaction list.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { CategoryBarChart, ComparisonBarChart, TrendChart } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { StatCard } from '../components/KpiCard';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { KpiSkeleton, QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { salesService } from '../services';
import { DataTable } from '../tables/DataTable';
import { TransactionTable } from '../tables/TransactionTable';
import { formatAmount, formatPercent } from '../utils/format';

// Quantity, volume and net sales are the three transactional measures, so every
// performance table reports all three — the same set the brand ranking shows.
// Volume is one figure with no unit column beside it: the source states a Total
// Volume per line and the group's figure is their sum.
const PERFORMANCE_COLUMNS = (t: (key: string) => string) => [
  { key: 'label', header: t('common.total') === 'Total' ? 'Name' : 'নাম' },
  { key: 'quantity', header: t('sales.quantity') },
  { key: 'volume', header: t('sales.volume') },
  { key: 'net_sales', header: t('sales.netSales') },
];

/** Brand ranking: the three transactional measures, one Volume column. */
const BRAND_COLUMNS = (t: (key: string) => string) => [
  { key: 'rank', header: t('common.rank') },
  { key: 'label', header: t('filters.materialBrand') },
  { key: 'quantity', header: t('sales.quantity') },
  { key: 'volume', header: t('sales.volume') },
  { key: 'net_sales', header: t('sales.netSales') },
];

export default function SalesPage() {
  const t = useT();
  const navigate = useNavigate();
  const { query } = useFilters();
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<'region' | 'brand' | 'customer' | 'salesforce' | 'territory'>(
    'region',
  );

  /** Carry the period and filters into the brand breakdown — see Dashboard. */
  const brandLink = (code: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('level', 'brand');
    params.set('brand', code);
    return `/products?${params.toString()}`;
  };

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['sales-page', query],
    queryFn: () => salesService.page(query),
  });

  const values = data?.summary.values ?? {};
  const growth = data?.growth.values ?? {};
  const dailyRows = data?.daily_trend.rows ?? [];

  const tables = {
    region: data?.region_performance,
    brand: data?.brand_performance,
    customer: data?.customer_performance,
    salesforce: data?.salesforce_performance,
    territory: data?.territory_performance,
  } as const;

  const activeTable = tables[tab];
  const activeRows = activeTable?.rows ?? [];

  /**
   * The headline figures an export repeats at the top, and the same one the
   * page shows on a card.
   *
   * Kept in step with the card below on purpose: an export block naming a
   * quantity and a discount the page no longer states would be the export
   * disagreeing with the report it came from. Both measures are still reported
   * — quantity in every performance table, discount on the transaction list —
   * they are simply not headline figures.
   */
  const kpiPairs: [string, string][] = [
    [t('sales.netSales'), formatAmount(values.net_sales)],
  ];

  return (
    <>
      <PageHeader
        title={t('sales.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={t('sales.title')}
            rows={activeRows}
            period={data?.period}
            kpis={kpiPairs}
          />
        }
      />

      <GlobalFilterBar />

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<KpiSkeleton count={6} />}
      >
        <div className="space-y-4">
          {/*
            Net sales alone, and no grid around it. A grid holding one card
            leaves an empty column that reads as something having failed to
            load — the same reason the Customers page dropped its grid when the
            receivables card left it.
          */}
          <StatCard
            label={t('sales.netSales')}
            value={formatAmount(values.net_sales)}
            hint={
              growth.growth_percent !== null && growth.growth_percent !== undefined
                ? `${formatPercent(growth.growth_percent, { signed: true })} ${t('common.vsPrevious')}`
                : undefined
            }
          />

          <ResultNotes notes={data?.summary.notes} />

          <div className="grid gap-4 lg:grid-cols-2">
            <Section title={t('sales.dailyTrend')}>
              <TrendChart
                data={dailyRows}
                xKey={dailyRows[0]?.date ? 'date' : 'label'}
                yKey="net_sales"
              />
            </Section>

            <Section title={t('sales.targetVsActual')}>
              <ComparisonBarChart
                data={data?.target_vs_actual.rows ?? []}
                xKey="label"
                series={[
                  { key: 'target_amount', label: t('kpi.target') },
                  { key: 'actual_sales', label: t('target.actual') },
                ]}
              />
            </Section>
          </div>

          {data?.monthly_trend?.rows?.length ? (
            <Section title={t('sales.monthlyTrend')}>
              <TrendChart
                data={data.monthly_trend.rows}
                xKey="label"
                yKey="net_sales"
                area
              />
            </Section>
          ) : null}

          <Section title={t('sales.byRegion')}>
            <CategoryBarChart
              data={data?.region_performance.rows ?? []}
              xKey="label"
              yKey="net_sales"
            />
          </Section>

          <section className="card">
            <div className="card-header flex-wrap gap-2">
              <div className="flex flex-wrap gap-1">
                {(
                  [
                    ['region', t('sales.byRegion')],
                    ['brand', t('sales.byBrand')],
                    ['customer', t('sales.byCustomer')],
                    ['salesforce', t('sales.bySalesForce')],
                    ['territory', t('sales.byTerritory')],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setTab(key)}
                    className={`tap-y rounded-lg px-3 py-1.5 text-xs font-medium ${
                      tab === key
                        ? 'bg-brand-600 text-white'
                        : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <ExportButtons
                reportName={`${t('sales.title')} – ${tab}`}
                rows={activeRows}
                period={data?.period}
                compact
              />
            </div>
            <div className="p-4">
              <DataTable
                // Each tab is a different report, so each remembers its own
                // arrangement.
                tableId={`sales.${tab}`}
                rows={activeRows}
                columns={tab === 'brand' ? BRAND_COLUMNS(t) : PERFORMANCE_COLUMNS(t)}
                onRowClick={
                  tab === 'region'
                    ? (row) => navigate(`/performance?level=area&region_code=${row.code}`)
                    : tab === 'brand'
                      ? (row) => navigate(brandLink(String(row.code)))
                      : undefined
                }
              />
              <ResultNotes notes={activeTable?.notes} />
            </div>
          </section>

          <Section title={t('sales.transactions')}>
            <TransactionTable dataType="sales" />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
