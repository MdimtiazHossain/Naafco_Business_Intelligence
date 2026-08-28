/**
 * Historical Analysis: the actual sales a target is generated from.
 *
 * The first screen in this module that reads real warehouse data rather than
 * planning input — two financial years of volume per material, out of
 * `vw_sales_detail`, under the caller's own scope.
 *
 * **Everything here can legitimately be absent, and absence is drawn as
 * absence.** A material with no sales in a year reads `n/a`, not `0`: the first
 * says the business did not sell it, the second is a measurement. Growth
 * against a year with no volume is undefined rather than infinite, and reads
 * `n/a` too. A deployment whose sales history has not been loaded sees an empty
 * table and a note saying so — never an invented basis.
 *
 * The **Suggested Basis** column is a suggestion. Nothing on this screen changes
 * a number; the allocation engine reads the same rule and a planner can override
 * it there.
 */

import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatPercent, formatQuantity } from '../../utils/format';
import type { TargetBasis, TargetHistoryResponse, TargetHistoryRow } from '../../types/api';

/**
 * Basis colours.
 *
 * Blue for a material whose trend is driving its allocation, slate for the
 * ordinary two-year average, cyan for one too new to have a trend, and amber
 * for one with no history at all — the only case a planner has to act on, so
 * the only one that reads as a warning. Colour is never the only signal: each
 * pill spells its basis out.
 */
const BASIS_TONE: Record<TargetBasis, string> = {
  TWO_YEAR_AVERAGE: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  GROWTH_WEIGHTED: 'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-300',
  NEW_MATERIAL: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-950/50 dark:text-cyan-300',
  NO_HISTORY: 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
};

/** A figure that could not be computed — never a dash, which reads as zero. */
function NotAvailable({ title }: { title?: string }) {
  return (
    <span className="cursor-help text-slate-400 dark:text-slate-500" title={title}>
      n/a
    </span>
  );
}

function volumeCell(value: number | null, complete: boolean, hint: string) {
  if (value === null) return <NotAvailable title={hint} />;
  return (
    <span className="tabular-nums">
      {formatQuantity(value)}
      {!complete && (
        <span
          className="ml-1 cursor-help text-amber-600 dark:text-amber-400"
          title={hint}
        >
          *
        </span>
      )}
    </span>
  );
}

export function HistoricalAnalysis({ data }: { data: TargetHistoryResponse }) {
  const t = useT();
  const years = data.basis_years;

  /**
   * One volume column per basis year, built from `basis_years` rather than
   * hard-coded to two. A plan may state its own years, and a column list that
   * assumed two would silently drop a third.
   */
  const yearColumns = years.map((year, index) => ({
    key: `year_${index}`,
    header: year,
    align: 'right' as const,
    render: (row: TargetHistoryRow) =>
      volumeCell(
        row.volumes[index] ?? null,
        row.years[index]?.complete ?? true,
        row.volumes[index] === null || row.volumes[index] === undefined
          ? t('targetMgmt.noSalesInYear', { year })
          : t('targetMgmt.someRowsNoVolume'),
      ),
  }));

  const columns = [
    { key: 'material_code', header: t('targetMgmt.col.material') },
    { key: 'material_description', header: t('targetMgmt.col.description') },
    ...yearColumns,
    {
      key: 'growth_percent',
      header: t('targetMgmt.col.growth'),
      align: 'right' as const,
      render: (row: TargetHistoryRow) =>
        row.growth_percent === null ? (
          <NotAvailable title={t('targetMgmt.growthUndefined')} />
        ) : (
          <span
            className={`font-medium tabular-nums ${
              row.growth_percent >= 0
                ? 'text-emerald-700 dark:text-emerald-400'
                : 'text-red-600 dark:text-red-400'
            }`}
          >
            {formatPercent(row.growth_percent, { signed: true })}
          </span>
        ),
    },
    {
      key: 'average_volume',
      header: t('targetMgmt.col.averageVolume'),
      align: 'right' as const,
      render: (row: TargetHistoryRow) =>
        row.average_volume === null ? (
          <NotAvailable />
        ) : (
          <span className="tabular-nums">{formatQuantity(row.average_volume)}</span>
        ),
    },
    {
      key: 'contribution_percent',
      header: t('targetMgmt.col.contribution'),
      align: 'right' as const,
      render: (row: TargetHistoryRow) =>
        row.contribution_percent === null ? (
          <NotAvailable />
        ) : (
          <span className="tabular-nums">
            {formatPercent(row.contribution_percent)}
          </span>
        ),
    },
    {
      key: 'current_target_volume',
      header: t('targetMgmt.col.currentTarget'),
      align: 'right' as const,
      render: (row: TargetHistoryRow) =>
        row.current_target_volume === null ? (
          <NotAvailable title={t('targetMgmt.noTargetSet')} />
        ) : (
          <span className="font-semibold tabular-nums text-brand-700 dark:text-brand-300">
            {formatQuantity(row.current_target_volume)}
          </span>
        ),
    },
    {
      key: 'target_growth_percent',
      header: t('targetMgmt.col.targetGrowth'),
      align: 'right' as const,
      hidden: true,
      render: (row: TargetHistoryRow) =>
        row.target_growth_percent === null ? (
          <NotAvailable />
        ) : (
          <span className="tabular-nums">
            {formatPercent(row.target_growth_percent, { signed: true })}
          </span>
        ),
    },
    {
      key: 'basis',
      header: t('targetMgmt.col.basis'),
      render: (row: TargetHistoryRow) => (
        <span
          className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${
            BASIS_TONE[row.basis]
          }`}
        >
          {t(`targetMgmt.basis.${row.basis}`)}
        </span>
      ),
    },
    { key: 'material_brand', header: t('targetMgmt.col.brand'), hidden: true },
    {
      key: 'material_group_name',
      header: t('targetMgmt.col.materialGroup'),
      hidden: true,
    },
  ];

  return (
    <Section
      title={t('targetMgmt.historyTitle')}
      className="mt-4"
      actions={
        <span className="text-xs text-slate-400 dark:text-slate-500">
          {years.join(' · ')}
        </span>
      }
    >
      <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
        {t('targetMgmt.historyHelp', {
          guidance: String(data.growth_guidance_percent),
        })}
      </p>

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {data.totals.years.map((year) => (
          <SummaryCard
            key={year.financial_year}
            label={year.financial_year}
            value={
              year.volume === null ? 'n/a' : formatQuantity(year.volume)
            }
            hint={t('targetMgmt.materialsWithVolume', {
              count: String(year.materials_with_volume),
            })}
            muted={year.volume === null}
          />
        ))}
        <SummaryCard
          label={t('targetMgmt.totalVolume')}
          value={formatQuantity(data.totals.target_volume)}
          hint={t('targetMgmt.currentTargetHint')}
        />
        <SummaryCard
          label={t('targetMgmt.withHistory')}
          value={`${data.totals.with_history} / ${data.totals.material_count}`}
          hint={t('targetMgmt.withHistoryHint')}
          muted={data.totals.with_history === 0}
        />
      </div>

      {data.rows.length === 0 ? (
        <EmptyState message={t('targetMgmt.noHistoryRows')} />
      ) : (
        <DataTable
          tableId="target-management.history"
          rows={data.rows}
          columns={columns}
          rowKey={(row) => (row as TargetHistoryRow).material_code}
          searchable
        />
      )}

      {data.notes.length > 0 && (
        <ul className="mt-3 space-y-1">
          {data.notes.map((note) => (
            <li
              key={note}
              className="text-xs italic text-slate-500 dark:text-slate-400"
            >
              {note}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function SummaryCard({
  label,
  value,
  hint,
  muted = false,
}: {
  label: string;
  value: string;
  hint: string;
  muted?: boolean;
}) {
  return (
    <div className="card p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p
        className={`mt-2 text-xl font-semibold tabular-nums ${
          muted
            ? 'text-slate-400 dark:text-slate-500'
            : 'text-slate-900 dark:text-slate-50'
        }`}
      >
        {value}
      </p>
      <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">{hint}</p>
    </div>
  );
}
