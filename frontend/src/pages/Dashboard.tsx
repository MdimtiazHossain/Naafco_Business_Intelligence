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
import { useDashboardSections } from './dashboardSections';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  barSeriesFrom,
  CHART_COLORS,
  ComboBarLineChart,
  orderedByYear,
  RankedBarChart,
  seriesHues,
  TrendChart,
  trendSeriesFrom,
  type TrendSeries,
} from '../charts/Charts';
import { KpiCard, StatCard } from '../components/KpiCard';
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
 * A combo card's bars, named and coloured as the reference design asks.
 *
 * Both dashboard combo cards read it — the monthly one over months, the region
 * one over regions — because the series are the same four things either way:
 * two earlier years, the plan, and the outcome. Two things are card-specific
 * and so live here rather than in `barSeriesFrom`, which every other trend
 * also uses.
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
function compactNames(
  series: TrendSeries[],
  t: (key: string) => string,
): TrendSeries[] {
  return series.map((one) => ({
    ...one,
    label: `${one.key.startsWith('target') ? t('chart.targetShort')
                                           : t('chart.actualShort')} `
      // "FY 2026-27" becomes "26-27": the century is the same on every series
      // here, so printing it four times buys nothing and costs the legend the
      // room it needs. A window that is not a financial year carries a
      // different label shape, matches neither pattern and is left exactly as
      // the tool wrote it.
      + one.label.replace(/^FY\s*/i, '').replace(/^\d{2}(\d{2}-\d{2})$/, '$1'),
  }));
}

function yearBars(
  chart: Parameters<typeof barSeriesFrom>[0],
  t: (key: string) => string,
): TrendSeries[] {
  const ordered = seriesHues(barSeriesFrom(chart, t('sales.netSales')));
  return chart?.series?.length ? compactNames(ordered, t) : ordered;
}

/**
 * The same series for the Sales Trend line chart: same order, same names, same
 * hues, and the dash kept.
 *
 * It goes through `trendSeriesFrom` rather than `barSeriesFrom` for exactly
 * that last reason — a bar cannot carry a dash so `barSeriesFrom` drops it,
 * while on a line it is what says plan against measurement. The ordering is the
 * shared `orderedByYear`, so the three cards' legends read history → plan →
 * outcome alike and a reader learns one arrangement rather than two.
 */
/**
 * The same series, measured in volume.
 *
 * Order, names, colours and which years survive are all decided once by
 * `yearBars`; this only swaps each series' key for the volume column beside
 * it, so a card can measure volume without a second opinion about what to
 * draw. The tool carries both measures on every row — `target_volume` and
 * `actual_volume` for this window, `volume_minus_N` for each earlier year —
 * so nothing here computes a figure.
 */
function volumeBars(
  chart: Parameters<typeof barSeriesFrom>[0],
  t: (key: string) => string,
): TrendSeries[] {
  const inVolume: Record<string, string> = {
    actual_sales: 'actual_volume',
    target_amount: 'target_volume',
  };
  return yearBars(chart, t).map((one) => ({
    ...one,
    key: inVolume[one.key]
      ?? one.key.replace(/^net_sales_minus_/, 'volume_minus_'),
  }));
}

function yearLines(
  chart: Parameters<typeof barSeriesFrom>[0],
  t: (key: string) => string,
): TrendSeries[] {
  const ordered = seriesHues(orderedByYear(
    trendSeriesFrom(chart, t('sales.netSales'))));
  return chart?.series?.length ? compactNames(ordered, t) : ordered;
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
  const customerLink = (code: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('customer_code', code);
    return `/customers?${params.toString()}`;
  };

  // The frame: the KPI strip, the period, and which cards this build serves.
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', query],
    queryFn: () => dashboardService.get(query),
  });
  // The cards themselves, one request each and all in flight together. The page
  // shows its KPIs as soon as the frame lands rather than waiting for every
  // aggregate, which is what it used to do for nine seconds.
  const sections = useDashboardSections(data?.sections, query);

  // Each panel is guarded independently rather than only on `data`. A backend
  // serving an older shape omits a key entirely, and `sections.byName.x.rows`
  // throws on the missing key rather than falling back — which took the whole
  // page down behind the error boundary instead of leaving one chart empty.
  const trendRows = sections.byName.sales_trend?.rows ?? [];
  // Its own section, not `sales_trend`: this card is a year of months whatever
  // period is chosen, and the line chart above follows the period exactly.
  const monthlyRows = sections.byName.monthly_performance?.rows ?? [];
  // One panel where there were two. The old Region Performance card drew the
  // net sales that this card's `actual_sales` already is — the same column of
  // the same view over the same window, read twice.
  const regionRows = sections.byName.region_overview?.rows ?? [];
  const customerRows = sections.byName.top_customers?.rows ?? [];
  // The year columns that table draws, oldest first and without the plan: the
  // same series the region card turns into bars, read as columns instead. The
  // browser names no year of its own — a hard-coded "24-25" is wrong the day
  // the financial year turns, and would be wrong now on any period whose third
  // year back recorded nothing.
  const customerYears = orderedByYear(
    trendSeriesFrom(sections.byName.top_customers?.chart),
  ).filter((series) => !series.key.startsWith('target'));
  // Present only for a reader who holds Credit Control: the server leaves the
  // card out of `sections` otherwise, so `byName` never gains the key and the
  // block below draws nothing. Absent rather than disabled, which is the rule
  // this application follows for a control whose only outcome is a refusal.
  const overdue = sections.byName.overdue_receivables;
  const territorySalesRows = sections.byName.territory_sales?.rows ?? [];
  const brandSalesRows = sections.byName.brand_sales?.rows ?? [];

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

          {/*
            Receivables, under the KPI strip and above the charts.

            This card was refused for three revisions and the reason was never
            that an overdue figure is uninteresting: the credit view reached the
            customer's sub-territory and no further, so a regional manager —
            the reader this page is most often open in front of — could only
            have been shown an unscoped national total or refused the whole
            dashboard. Revision 0040 gave that view the sales hierarchy, so the
            figure here is the reader's own region.

            It leads with the **share**, not the amount, for the same reason the
            HIGH_OVERDUE alert does: a crore overdue is alarming on a small book
            and routine on a large one, so a taka threshold would need re-setting
            every time the business grew.

            It draws nothing at all when the server did not offer the card, and
            `n/a` rather than 0% when the book is empty — a portfolio with
            nothing in it has no overdue proportion, and 0% would read as good
            news about something that does not exist.
          */}
          {overdue ? (
            <div className="grid grid-cols-1 gap-3 min-[360px]:grid-cols-2 lg:grid-cols-3">
              <StatCard
                label={t('dashboard.overdueShare')}
                value={
                  overdue.values.overdue_share_percent === null ||
                  overdue.values.overdue_share_percent === undefined
                    ? t('common.notAvailable')
                    : formatPercent(overdue.values.overdue_share_percent as number)
                }
                tone={
                  typeof overdue.values.overdue_share_percent === 'number' &&
                  overdue.values.overdue_share_percent >= 25
                    ? 'danger'
                    : 'default'
                }
                hint={t('dashboard.overdueOf', {
                  amount: formatAmount(overdue.values.outstanding_amount as number),
                })}
              />
              <StatCard
                label={t('dashboard.overdueAmount')}
                value={formatAmount(overdue.values.overdue_amount as number)}
                hint={t('credit.invoiceCount', {
                  count: (overdue.values.overdue_invoice_count as number) ?? 0,
                })}
              />
              <div className="card p-4">
                <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  {t('dashboard.overdueAsOn')}
                </p>
                {/* The tool states its own reporting date in a note, and it is
                    carried through rather than re-derived here: the same invoice
                    is Not Yet Due in June and Over Due in August, so a figure
                    without the date it was measured on is not one anybody can
                    check. No business calculation lives in the browser, and
                    neither does this. */}
                <ResultNotes notes={overdue.notes} />
              </div>
            </div>
          ) : null}

          <Section title={t('dashboard.salesTrend')}>
            <TrendChart
              data={trendRows}
              xKey={trendRows[0]?.date ? 'date' : 'label'}
              // A monthly window carries two earlier years and the target; a
              // daily one is a single line, and `trendSeriesFrom` returns
              // exactly that when the tool sent no series.
              // The same order, names and hues as the two cards below, so one
              // legend arrangement serves all three. The other pages that draw
              // this chart are untouched: they show one trend on its own, with
              // no plan-against-outcome to arrange around.
              series={yearLines(sections.byName.sales_trend?.chart, t)}
              // The same two ratios the cards below draw, against a right-hand
              // axis. They are honoured only where the rows carry them, which
              // is a monthly window: a period charted by day has no monthly
              // target and no aligned prior year, so `_multi_year_trend` puts
              // neither key on those rows and the axis is not drawn at all.
              percentSeries={[
                { key: 'achievement_percent', label: t('dashboard.achievementShort'),
                  color: CHART_COLORS[4] },
                { key: 'growth_percent', label: t('dashboard.growthShort'),
                  color: CHART_COLORS[3] },
              ]}
              percentAxisLabel={t('dashboard.percentAxis')}
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
              bars={yearBars(sections.byName.monthly_performance?.chart, t)}
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
            <ResultNotes notes={sections.byName.monthly_performance?.notes} />
          </Section>

          {/*
            Full width, because it carries six series over every region: the
            two-column grid this replaced gave each half a plot too narrow to
            read a region name in. The count is deliberately not written down
            here — the card draws whatever the master data holds, and a number
            in this comment would be wrong the day a region is opened.

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
              // The same four bars the Monthly Performance card draws, named
              // from the backend's own series list: two earlier years, the
              // plan, the outcome. `previous_sales` is still on the row and
              // still drives the growth line — it is simply not drawn, because
              // over a financial year it is the same window as A 25-26 and a
              // fifth bar per region would repeat one of the four.
              bars={yearBars(sections.byName.region_overview?.chart, t)}
              // And its line labels and colours. "Achievement %" and "Growth %"
              // are the same two measures under longer names; one legend
              // spelling across both cards is one thing to learn instead of two.
              lines={[
                { key: 'achievement_percent', label: t('dashboard.achievementShort'),
                  color: CHART_COLORS[4] },
                { key: 'growth_percent', label: t('dashboard.growthShort'),
                  color: CHART_COLORS[3] },
              ]}
            />
            <ResultNotes notes={sections.byName.region_overview?.notes} />
          </Section>

          {/*
            Two ranked cards side by side, under the region card. Each is the
            same three measures grouped a different way, ranked by what was
            sold — the colours are the region and monthly cards' own, so a
            reader learns one scheme for the whole page.

            Side by side because they answer the same question about two
            dimensions and are read against each other; they stack on a phone,
            where twenty rows of names need the full width.
          */}
          <div className="grid gap-4 lg:grid-cols-2">
            <Section title={t('dashboard.territorySales')}>
              <RankedBarChart
                data={territorySalesRows}
                xKey="label"
                bars={yearBars(sections.byName.territory_sales?.chart, t)}
              />
              <ResultNotes notes={sections.byName.territory_sales?.notes} />
            </Section>

            <Section title={t('dashboard.brandSales')}>
              <RankedBarChart
                data={brandSalesRows}
                xKey="label"
                bars={volumeBars(sections.byName.brand_sales?.chart, t)}
                // Volume carries no unit anywhere in this platform, so the
                // figures are plain grouped numbers — a taka sign would name a
                // currency they are not in.
                valueKind="quantity"
              />
              <ResultNotes notes={sections.byName.brand_sales?.notes} />
            </Section>
          </div>

          {/*
            Full width, and outside the two-column grid above: a customer name
            plus three years of taka does not fit in half a dashboard, and
            `DataTable` scrolls horizontally on its own so a narrow screen keeps
            the same table rather than a different one.

            The year columns are **not written here**. They come from the
            backend's own `chart.series`, ordered oldest-first by the same
            helper the region card's bars use, so the table shows two columns
            rather than three when the third year recorded nothing — and no
            financial year is spelled into the browser, which would be wrong
            the moment the year rolled over.
          */}
          <Section title={t('dashboard.topCustomers')}>
            <DataTable
              tableId="dashboard.customers"
              rows={customerRows}
              columns={[
                { key: 'label', header: t('filters.customer') },
                ...customerYears.map((year) => ({
                  key: year.key,
                  header: year.label,
                })),
                // Growth is the last column because it reads as the change
                // between the two before it — and it is exactly that: the
                // backend caps the window to what has elapsed, so this figure
                // and those columns cannot disagree.
                { key: 'growth_percent', header: t('common.growth') },
              ]}
              searchable
              pageSize={25}
              onRowClick={(row) => navigate(customerLink(String(row.code)))}
            />
            <ResultNotes notes={sections.byName.top_customers?.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
