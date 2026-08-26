/** Target page: achievement against target, with under-performers highlighted. */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ComparisonBarChart } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { StatCard } from '../components/KpiCard';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { KpiSkeleton, QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { targetService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatAmount, formatPercent, formatQuantity } from '../utils/format';

/** Below this, achievement is flagged for attention. */
const ATTENTION_THRESHOLD = 80;

export default function TargetPage() {
  const t = useT();
  const navigate = useNavigate();
  const { query } = useFilters();
  const [searchParams] = useSearchParams();
  const [onlyBelow, setOnlyBelow] = useState(false);

  /**
   * A brand drill-down is a link, so it carries the period and filters that
   * produced the row — landing on `/materials` without them would show a
   * different window's numbers under the same brand name.
   */
  const brandLink = (code: string) => {
    const params = new URLSearchParams(searchParams);
    params.set('level', 'material_brand');
    params.set('material_brand', code);
    return `/materials?${params.toString()}`;
  };

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['target-page', query, onlyBelow],
    queryFn: () =>
      targetService.page({
        ...query,
        below_percent: onlyBelow ? ATTENTION_THRESHOLD : undefined,
      }),
  });

  const values = data?.summary.values ?? {};
  const regionRows = data?.region_achievement.rows ?? [];
  const territoryRows = data?.territory_achievement.rows ?? [];
  // Guarded with `?.` unlike its neighbours: a backend serving the shape from
  // before this table existed omits the key entirely, and reaching through it
  // would throw and take the whole page down rather than leaving one section
  // empty.
  const brandRows = data?.brand_achievement?.rows ?? [];
  const visibleRows = data?.summary.rows ?? [];

  /**
   * Achievement, red below the attention threshold.
   *
   * Shared by every table on this page, brand included: the reason to open the
   * Target page is to find what is behind, so a figure that is behind looks the
   * same wherever it appears. `n/a` is never red — an achievement that could
   * not be computed is not an achievement that is low.
   */
  const percentCell = (value: number | null | undefined) => {
    const below = value !== null && value !== undefined && value < ATTENTION_THRESHOLD;
    return (
      <span
        className={below ? 'font-semibold text-red-600 dark:text-red-400' : 'tabular-nums'}
      >
        {formatPercent(value)}
      </span>
    );
  };

  // Quantity and volume sit beside value: a region can hit its taka target on
  // price while shipping less than it promised, and one column cannot show that.
  // Volume is blank where the targets in scope span mass and volume units.
  const achievementColumns = (labelHeader: string) => [
    { key: 'label', header: labelHeader },
    { key: 'target_quantity', header: t('target.quantity') },
    { key: 'actual_quantity', header: t('target.actualQuantity') },
    { key: 'target_volume', header: t('target.volume') },
    { key: 'actual_volume', header: t('target.actualVolume') },
    { key: 'target_amount', header: t('kpi.target') },
    { key: 'actual_sales', header: t('target.actual') },
    {
      key: 'achievement_percent',
      header: t('target.achievement'),
      render: (row: Record<string, any>) =>
        percentCell(row.achievement_percent as number | null),
    },
    { key: 'gap', header: t('target.gap') },
  ];

  /**
   * Brand achievement, in the brand-target column order.
   *
   * Deliberately **not** `achievementColumns`. That set is built for an
   * organisational group — target quantity, actual quantity, one achievement,
   * one gap — and this row states a different thing: volume and amount are
   * tracked as two separate plans, each with its own achievement and its own
   * shortfall, because a brand can hit its taka target on price while shipping
   * well under what it promised. These are the same columns and the same
   * figures as the dashboard's brand table, so the two never disagree.
   *
   * Shortfall is actual − target and so is negative when a brand is behind;
   * `gap` on the other tables is the reverse. They are not the same column and
   * are not interchangeable.
   *
   * Volume carries no unit and is not converted. A brand with nothing planned
   * shows n/a rather than 0% — a missing target is not a missed one.
   */
  const brandColumns = [
    { key: 'rank', header: t('common.rank') },
    { key: 'label', header: t('filters.materialBrand') },
    { key: 'target_volume', header: t('dashboard.targetVol') },
    { key: 'volume', header: t('dashboard.salesVol') },
    { key: 'target_amount', header: t('dashboard.targetBdt') },
    { key: 'net_sales', header: t('sales.netSales') },
    {
      key: 'volume_achievement_percent',
      header: t('dashboard.volAch'),
      render: (row: Record<string, any>) =>
        percentCell(row.volume_achievement_percent as number | null),
    },
    {
      key: 'achievement_percent',
      header: t('dashboard.bdtAch'),
      render: (row: Record<string, any>) =>
        percentCell(row.achievement_percent as number | null),
    },
    { key: 'volume_shortfall', header: t('dashboard.volShortfall') },
    { key: 'amount_shortfall', header: t('dashboard.bdtShortfall') },
  ];

  return (
    <>
      <PageHeader
        title={t('target.title')}
        period={data?.period}
        actions={
          <ExportButtons
            reportName={t('target.title')}
            rows={visibleRows}
            period={data?.period}
            kpis={[
              [t('kpi.target'), formatAmount(values.target)],
              [t('target.actual'), formatAmount(values.actual)],
              [t('target.achievement'), formatPercent(values.achievement_percent)],
              [t('target.gap'), formatAmount(values.gap)],
            ]}
          />
        }
      />

      <GlobalFilterBar />

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<KpiSkeleton />}
      >
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label={t('target.quantity')}
              value={formatQuantity(values.target_quantity)}
            />
            <StatCard
              label={t('target.volume')}
              value={formatQuantity(values.target_volume)}
            />
            <StatCard label={t('kpi.target')} value={formatAmount(values.target)} />
            <StatCard label={t('target.actual')} value={formatAmount(values.actual)} />
            <StatCard
              label={t('target.achievement')}
              value={formatPercent(values.achievement_percent)}
              tone={
                values.achievement_percent === null || values.achievement_percent === undefined
                  ? 'default'
                  : values.achievement_percent < ATTENTION_THRESHOLD
                    ? 'danger'
                    : 'success'
              }
            />
            <StatCard
              label={t('target.gap')}
              value={formatAmount(values.gap)}
              tone={(values.gap ?? 0) > 0 ? 'warning' : 'success'}
            />
          </div>

          <ResultNotes notes={data?.summary.notes} />

          <Section title={t('target.vsActual')}>
            <ComparisonBarChart
              data={regionRows}
              xKey="label"
              series={[
                { key: 'target_amount', label: t('kpi.target') },
                { key: 'actual_sales', label: t('target.actual') },
              ]}
            />
          </Section>

          <Section
            title={t('target.regionAchievement')}
            actions={
              <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={onlyBelow}
                  onChange={(event) => setOnlyBelow(event.target.checked)}
                />
                {t('target.belowThreshold')}
              </label>
            }
          >
            <DataTable
              tableId="target.region"
              rows={onlyBelow ? visibleRows : regionRows}
              columns={achievementColumns(t('filters.region'))}
              onRowClick={(row) =>
                navigate(`/performance?level=area&region_code=${row.code}`)
              }
            />
          </Section>

          <Section title={t('target.territoryAchievement')}>
            <DataTable
              tableId="target.territory"
              rows={territoryRows}
              columns={achievementColumns(t('filters.territory'))}
            />
          </Section>

          {/*
            Where the plan stood against what moved, by brand rather than by
            geography — the other question this page is opened to answer. The
            notes are rendered because this is where the backend says a brand's
            targets did not cover the window, which is why an achievement reads
            n/a.
          */}
          <Section title={t('target.brandAchievement')}>
            <DataTable
              tableId="target.brand"
              rows={brandRows}
              columns={brandColumns}
              searchable={false}
              pageSize={15}
              onRowClick={(row) => navigate(brandLink(String(row.code)))}
            />
            <ResultNotes notes={data?.brand_achievement?.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
