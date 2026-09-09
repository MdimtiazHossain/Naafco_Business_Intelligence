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
import {
  barSeriesFrom,
  CHART_COLORS,
  ComboBarLineChart,
  seriesHues,
  TrendChart,
  trendSeriesFrom,
  type TrendSeries,
} from '../charts/Charts';
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

/**
 * The monthly card's bars, named and coloured as its reference design asks.
 *
 * Two things are card-specific and so live here rather than in
 * `barSeriesFrom`, which every other trend also uses.
 *
 * **The names are compact.** The tool labels each series with the window it
 * covers — "FY 2026-27" — which is right for a legend of three or four lines
 * and too wide for one of six items. `A 26-27` and `T 26-27` say actual and
 * target of that year in the width a legend has, and the prefixes come from
 * i18n rather than being spelled here.
 *
 * **The colour is not card-specific and is no longer decided here.**
 * `seriesHues` holds that rule and the Sales Trend line chart above reads it
 * too, so this period's actual is green on both. It used to be spelled out in
 * this function, which meant the two cards agreed only for as long as nobody
 * touched one of them.
 *
 * A window too short for months sends no series list at all — one actual and
 * nothing to compare it with — and is returned untouched, or the prefix would
 * rename it "A Net Sales".
 */
function monthlyBars(
  chart: Parameters<typeof barSeriesFrom>[0],
  t: (key: string) => string,
): TrendSeries[] {
  const ordered = seriesHues(barSeriesFrom(chart, t('sales.netSales')));
  if (!chart?.series?.length) return ordered;
  return ordered.map((bar) => {
    const isTarget = bar.key.startsWith('target');
    return {
      ...bar,
      label: `${isTarget ? t('chart.targetShort') : t('chart.actualShort')} `
        // "FY 2026-27" becomes "26-27": the century is the same on every
        // series here, so printing it four times buys nothing and costs the
        // legend the room it needs. A window that is not a financial year
        // carries a different label shape, matches neither pattern and is
        // left exactly as the tool wrote it.
        + bar.label.replace(/^FY\s*/i, '').replace(/^\d{2}(\d{2}-\d{2})$/, '$1'),
    };
  });
}

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
  // Its own section, not `sales_trend`: this card is a year of months whatever
  // period is chosen, and the line chart above follows the period exactly.
  const monthlyRows = data?.monthly_performance?.rows ?? [];
  // One panel where there were two. The old Region Performance card drew the
  // net sales that this card's `actual_sales` already is — the same column of
  // the same view over the same window, read twice.
  const regionRows = data?.region_overview?.rows ?? [];
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
          {/* One KPI to a row on the narrowest phones, two from 360px.
              Two columns at 320px left each card about 104px of inner width,
              which a grouped stock figure of eleven digits runs straight out
              of — and a figure is the one thing on this page that may not be
              clipped or shortened. `min-[360px]` rather than `sm`, so a 375 or
              430px handset still gets the pair of columns it has room for. */}
          <div className="grid grid-cols-1 gap-3 min-[360px]:grid-cols-2 lg:grid-cols-3">
            {(data?.kpis ?? []).map((kpi) => (
              <KpiCard key={kpi.key} kpi={kpi} icon={KPI_ICONS[kpi.key]} />
            ))}
          </div>

          <Section title={t('dashboard.salesTrend')}>
            <TrendChart
              data={trendRows}
              xKey={trendRows[0]?.date ? 'date' : 'label'}
              // A monthly window carries two earlier years and the target; a
              // daily one is a single line, and `trendSeriesFrom` returns
              // exactly that when the tool sent no series.
              // The same hue rule as the Monthly Performance card below, so
              // this period's actual is green on both and a reader is not
              // asked to learn one colour for a year on one card and another
              // on the next. The other pages that draw this chart keep the
              // palette order they have: they show one trend on its own, with
              // no plan-against-outcome for a colour to pick out.
              series={seriesHues(trendSeriesFrom(data?.sales_trend?.chart,
                                                 t('sales.netSales'),
                                                 t('kpi.target')))}
              height={280}
            />
          </Section>

          {/*
            A year of months, and its own query. It read `sales_trend` while
            that section was monthly, and drew one bar per *date* the moment a
            reader picked a period short enough to be charted by day — under a
            title promising months. The line chart above still follows the
            period exactly, which is what it is for.

            Money on the left axis, ratios on the right, for the reason the
            region card below gives: an achievement of 93 shares a taka axis
            with figures in the crores only by becoming invisible.
          */}
          <Section title={t('dashboard.monthlyPerformance')}>
            <ComboBarLineChart
              data={monthlyRows}
              xKey="label"
              height={320}
              // Named from the backend's own series list — prior years, target
              // and actual, ordered and coloured by `barSeriesFrom` — so a third
              // comparison year needs no change here and every year keeps the
              // hue it has on the line chart above.
              bars={monthlyBars(data?.monthly_performance?.chart, t)}
              // The percentages are named explicitly, because they are
              // deliberately absent from `chart.series`: that list is what the
              // line chart above turns into lines on a taka axis.
              lines={[
                { key: 'achievement_percent', label: t('dashboard.achievementShort'),
                  color: CHART_COLORS[4] },
                { key: 'growth_percent', label: t('dashboard.growthShort'),
                  color: CHART_COLORS[3] },
              ]}
              // Both axes are titled because they measure different kinds of
              // thing, and a reader who takes the right-hand scale for taka
              // reads every percentage as a rounding error.
              valueAxisLabel={t('dashboard.valueAxis')}
              percentAxisLabel={t('dashboard.percentAxis')}
              // The window is stated in the page header, so a month need not
              // repeat its year on every tick.
              xTickFormatter={(value) => value.replace(/\s+\d{4}$/, '')}
              // Twelve categories and two lines: the reading matters more here
              // than the shape, and hovering twelve points to find one figure
              // is not reading a chart.
              showLineValues
            />
            {/* This card's own notes: which financial year it covers, any
                year it could not draw, and whether the figures are narrower
                than the country its title names. */}
            <ResultNotes notes={data?.monthly_performance?.notes} />
          </Section>

          {/*
            Full width, because it carries five series over ten regions: the
            two-column grid this replaced gave each half a plot too narrow to
            read a region name in.

            Money on the left axis, ratios on the right. The two were previously
            in separate cards, which meant a reader comparing a region's
            achievement against what it actually sold had to hold one card in
            their head while looking at the other.
          */}
          <Section title={t('dashboard.regionOverview')}>
            <ComboBarLineChart
              data={regionRows}
              xKey="label"
              height={340}
              bars={[
                // The target is the plan, not another measurement, so it takes
                // the palette's neutral for the same reason the trend's target
                // line does.
                { key: 'target_amount', label: t('kpi.target'), color: CHART_COLORS[7] },
                { key: 'actual_sales', label: t('target.actual') },
                { key: 'previous_sales', label: t('dashboard.lastPeriodActual') },
              ]}
              lines={[
                { key: 'achievement_percent', label: t('target.achievement') },
                { key: 'growth_percent', label: t('dashboard.growthLine') },
              ]}
            />
            <ResultNotes notes={data?.region_overview?.notes} />
          </Section>

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
                { key: 'net_sales', header: t('dashboard.salesBdt') },
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
