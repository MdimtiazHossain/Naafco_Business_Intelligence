/**
 * Executive dashboard.
 *
 * One request to `/api/dashboard` returns the KPI cards and every chart, all
 * computed by the Phase 3 tools under the caller's data scope.
 */

import {
  AlertTriangle,
  Boxes,
  Percent,
  Target,
  TrendingUp,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { CategoryBarChart, ComparisonBarChart, TrendChart } from '../charts/Charts';
import { KpiCard } from '../components/KpiCard';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { ExportButtons } from '../components/ExportButtons';
import { KpiSkeleton, QueryState } from '../components/States';
import {
  DASHBOARD_FILTERS,
  DASHBOARD_FILTER_GROUPS,
  useFilters,
} from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { dashboardService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatAmount, formatPercent } from '../utils/format';

const KPI_ICONS: Record<string, React.ReactNode> = {
  total_sales: <TrendingUp size={16} />,
  target: <Target size={16} />,
  achievement: <Percent size={16} />,
  unrestricted_stock: <Boxes size={16} />,
  expiring_soon_stock: <AlertTriangle size={16} />,
  expired_stock: <AlertTriangle size={16} />,
  // No `sales_volume` entry: the executive view no longer carries a Sales
  // Volume card. Volume is still reported where it can be broken down — the
  // Sales page, the Top 15 Brands table's Sales Vol column below, and the AI
  // assistant.
};

export default function Dashboard() {
  const t = useT();
  const navigate = useNavigate();
  const { queryFor } = useFilters();
  const [searchParams] = useSearchParams();

  /**
   * Only the filters this dashboard offers.
   *
   * Filter state is global and lives in the URL, so arriving from another page
   * can carry a sales-force or a batch this bar does not draw. Sending them
   * anyway would narrow every figure by a filter with no control and no chip —
   * the exact hidden filter the grouped bar exists to prevent.
   */
  const query = queryFor(DASHBOARD_FILTERS);

  /**
   * A drill-down is a link, so it has to carry the period and filters that
   * produced the row. Navigating to a bare path instead drops them and lands
   * the user on an empty report for whatever the default period happens to be.
   */
  const brandLink = (code: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('level', 'material_brand');
    params.set('material_brand', code);
    return `/materials?${params.toString()}`;
  };

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', query],
    queryFn: () => dashboardService.get(query),
  });

  // Each panel is guarded independently rather than only on `data`. A backend
  // serving an older shape omits a key entirely, and `data?.top_brands.rows`
  // throws on the missing key rather than falling back — which took the whole
  // page down behind the error boundary instead of leaving one chart empty.
  const trendRows = data?.sales_trend?.rows ?? [];
  const regionRows = data?.region_performance?.rows ?? [];
  const achievementRows = data?.target_achievement?.rows ?? [];
  const brandRows = data?.top_brands?.rows ?? [];

  const kpiPairs: [string, string][] = (data?.kpis ?? []).map((kpi) => [
    kpi.label,
    kpi.format === 'percent' ? formatPercent(kpi.value) : formatAmount(kpi.value),
  ]);

  return (
    <>
      <PageHeader
        title={t('dashboard.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={t('dashboard.title')}
            rows={regionRows}
            period={data?.period}
            kpis={kpiPairs}
          />
        }
      />

      {/* Sticky by default, and deliberately unwrapped — see GlobalFilterBar. */}
      <GlobalFilterBar groups={DASHBOARD_FILTER_GROUPS} />

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<KpiSkeleton count={6} columns={3} />}
      >
        <div className="space-y-4">
          {/*
            Six cards, and the column counts are chosen so none of them ever
            leaves a hole: two columns on a phone, three from `lg` up. The
            fourth column went with the Sales Volume card — six cards across
            four columns would have left the second row half empty.
          */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {(data?.kpis ?? []).map((kpi) => (
              <KpiCard key={kpi.key} kpi={kpi} icon={KPI_ICONS[kpi.key]} />
            ))}
          </div>

          <Section title={t('dashboard.salesTrend')}>
            <TrendChart
              data={trendRows}
              xKey={trendRows[0]?.date ? 'date' : 'label'}
              yKey="net_sales"
              height={280}
            />
          </Section>

          <div className="grid gap-4 lg:grid-cols-2">
            <Section title={t('dashboard.regionPerformance')}>
              <CategoryBarChart data={regionRows} xKey="label" yKey="net_sales" />
            </Section>

            <Section title={t('dashboard.targetAchievement')}>
              <ComparisonBarChart
                data={achievementRows}
                xKey="label"
                series={[
                  { key: 'target_amount', label: t('kpi.target') },
                  { key: 'actual_sales', label: t('target.actual') },
                ]}
              />
            </Section>

          </div>

          {/*
            Full width, and outside the two-column grid above: ten columns of
            plan against actual do not fit in half a dashboard, and wrapping
            them into a second card would break the row a reader follows across.
            `DataTable` scrolls horizontally on its own, so narrow screens keep
            the same table rather than a different one.

            Volume columns carry no unit. A brand sold in both mass and volume
            units has no single volume figure and shows a dash; the backend says
            so in the result notes rather than adding kilograms to litres.
          */}
          <Section title={t('dashboard.topBrands')}>
            <DataTable
              tableId="dashboard.brands"
              rows={brandRows}
              columns={[
                { key: 'rank', header: t('common.rank') },
                { key: 'label', header: t('filters.materialBrand') },
                { key: 'target_volume', header: t('dashboard.targetVol') },
                { key: 'volume', header: t('dashboard.salesVol') },
                { key: 'target_amount', header: t('dashboard.targetBdt') },
                { key: 'net_sales', header: t('sales.netSales') },
                { key: 'volume_achievement_percent', header: t('dashboard.volAch') },
                { key: 'achievement_percent', header: t('dashboard.bdtAch') },
                { key: 'volume_shortfall', header: t('dashboard.volShortfall') },
                { key: 'amount_shortfall', header: t('dashboard.bdtShortfall') },
              ]}
              searchable={false}
              pageSize={15}
              onRowClick={(row) => navigate(brandLink(String(row.code)))}
            />
            <ResultNotes notes={data?.top_brands?.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
