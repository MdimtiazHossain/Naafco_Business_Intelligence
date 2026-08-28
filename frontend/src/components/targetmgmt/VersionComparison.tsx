/**
 * What moved between two versions of one plan.
 *
 * **Added and removed are not zero.** A node the newer version does not reach
 * has no target in it — not a target cut to nothing — so its figure reads `n/a`
 * with a tooltip saying which side it is missing from, and its change
 * percentage is blank. Drawing either as `0` would be a claim nobody made.
 *
 * **A percentage against a zero base is left blank.** The same rule every ratio
 * in this app follows; `+∞%` and `0%` are both wrong and one of them looks
 * plausible.
 *
 * **The headline total is the roots, not the sum of the rows.** Summing a tree
 * adds each figure once per level it appears at, which would report one
 * movement several times over — the backend computes it and this component
 * renders it, which is why nothing here adds anything up.
 */

import { ArrowRight, Minus, Plus, TrendingDown, TrendingUp } from 'lucide-react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatQuantity } from '../../utils/format';
import type {
  TargetComparisonResponse,
  TargetComparisonRow,
} from '../../types/api';

/** A figure that does not exist on one side — never a dash, which reads as zero. */
function NotAvailable({ title }: { title: string }) {
  return (
    <span className="cursor-help text-slate-400 dark:text-slate-500" title={title}>
      n/a
    </span>
  );
}

const STATUS_TONE: Record<string, string> = {
  INCREASED: 'text-emerald-700 dark:text-emerald-400',
  DECREASED: 'text-red-600 dark:text-red-400',
  ADDED: 'text-blue-700 dark:text-blue-400',
  REMOVED: 'text-amber-700 dark:text-amber-400',
  UNCHANGED: 'text-slate-500 dark:text-slate-400',
};

function StatusMark({ status }: { status: string }) {
  const Icon =
    status === 'INCREASED'
      ? TrendingUp
      : status === 'DECREASED'
        ? TrendingDown
        : status === 'ADDED'
          ? Plus
          : status === 'REMOVED'
            ? Minus
            : null;
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${STATUS_TONE[status] ?? ''}`}>
      {Icon && <Icon className="h-3 w-3" />}
      {status.toLowerCase()}
    </span>
  );
}

export function VersionComparison({
  data,
  onMaterialChange,
}: {
  data: TargetComparisonResponse;
  onMaterialChange: (material: string | null) => void;
}) {
  const t = useT();
  const totals = data.totals;

  const columns = [
    {
      key: 'name',
      header: t('targetMgmt.levelNode'),
      render: (row: TargetComparisonRow) => (
        <span
          className="flex items-center gap-1.5"
          style={{ paddingLeft: `${row.depth * 16}px` }}
        >
          <span className="min-w-[72px] text-[10px] uppercase tracking-wide text-slate-400">
            {row.level.replace(/_/g, ' ')}
          </span>
          <span className="font-medium text-slate-900 dark:text-slate-100">
            {row.node_code}
          </span>
          {row.name && (
            <span className="text-slate-500 dark:text-slate-400">{row.name}</span>
          )}
        </span>
      ),
    },
    {
      key: 'base_volume',
      header: `V${data.base_version.version_no}`,
      align: 'right' as const,
      render: (row: TargetComparisonRow) =>
        row.base_volume === null ? (
          <NotAvailable title={t('targetMgmt.compare.notInBase')} />
        ) : (
          formatQuantity(row.base_volume)
        ),
    },
    {
      key: 'volume',
      header: `V${data.version.version_no}`,
      align: 'right' as const,
      render: (row: TargetComparisonRow) =>
        row.volume === null ? (
          <NotAvailable title={t('targetMgmt.compare.notInHead')} />
        ) : (
          formatQuantity(row.volume)
        ),
    },
    {
      key: 'change',
      header: t('targetMgmt.compare.change'),
      align: 'right' as const,
      render: (row: TargetComparisonRow) =>
        row.change === null ? (
          <NotAvailable title={t('targetMgmt.compare.changeUndefined')} />
        ) : (
          <span className={STATUS_TONE[row.status] ?? ''}>
            {row.change >= 0 ? '+' : ''}
            {formatQuantity(row.change)}
          </span>
        ),
    },
    {
      key: 'change_percent',
      header: '%',
      align: 'right' as const,
      render: (row: TargetComparisonRow) =>
        row.change_percent === null ? (
          <NotAvailable title={t('targetMgmt.compare.percentUndefined')} />
        ) : (
          <span className={STATUS_TONE[row.status] ?? ''}>
            {row.change_percent >= 0 ? '+' : ''}
            {row.change_percent.toFixed(1)}%
          </span>
        ),
    },
    {
      key: 'status',
      header: t('targetMgmt.col.status'),
      render: (row: TargetComparisonRow) => <StatusMark status={row.status} />,
    },
  ];

  return (
    <div className="space-y-4">
      <Section title={t('targetMgmt.compare.summaryTitle')}>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              V{data.base_version.version_no}
            </p>
            <p className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              {totals.base_volume === null
                ? 'n/a'
                : formatQuantity(totals.base_volume)}
            </p>
          </div>
          <ArrowRight className="h-4 w-4 text-slate-400" />
          <div>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              V{data.version.version_no}
            </p>
            <p className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              {totals.volume === null ? 'n/a' : formatQuantity(totals.volume)}
            </p>
          </div>
          <div className="ml-auto text-right">
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {t('targetMgmt.compare.change')}
            </p>
            <p
              className={`text-lg font-semibold tabular-nums ${
                (totals.change ?? 0) >= 0
                  ? 'text-emerald-700 dark:text-emerald-400'
                  : 'text-red-600 dark:text-red-400'
              }`}
            >
              {totals.change === null ? (
                'n/a'
              ) : (
                <>
                  {totals.change >= 0 ? '+' : ''}
                  {formatQuantity(totals.change)}
                  {totals.change_percent !== null && (
                    <span className="ml-1 text-sm">
                      ({totals.change_percent >= 0 ? '+' : ''}
                      {totals.change_percent.toFixed(1)}%)
                    </span>
                  )}
                </>
              )}
            </p>
          </div>
        </div>
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.compare.nodesMoved', {
            changed: String(totals.nodes_changed),
            total: String(totals.nodes),
          })}
          {' · '}
          {data.scope}
        </p>
      </Section>

      {data.country.length > 0 && (
        <Section title={t('targetMgmt.compare.countryTitle')}>
          <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
            {t('targetMgmt.compare.countryDescription')}
          </p>
          <ul className="space-y-1 text-sm">
            {data.country.map((line) => (
              <li key={line.material_code} className="flex items-baseline gap-3">
                <span className="w-24 font-medium text-slate-900 dark:text-slate-100">
                  {line.material_code}
                </span>
                <span className="tabular-nums text-slate-500 dark:text-slate-400">
                  {line.base_volume === null
                    ? 'n/a'
                    : formatQuantity(line.base_volume)}
                </span>
                <ArrowRight className="h-3 w-3 text-slate-400" />
                <span className="tabular-nums text-slate-900 dark:text-slate-100">
                  {line.volume === null ? 'n/a' : formatQuantity(line.volume)}
                </span>
                <StatusMark status={line.status} />
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section
        title={t('targetMgmt.compare.rowsTitle')}
        actions={
          <select
            className="input"
            aria-label={t('targetMgmt.col.material')}
            value={data.material_code ?? ''}
            onChange={(event) => onMaterialChange(event.target.value || null)}
          >
            <option value="">{t('targetMgmt.allMaterials')}</option>
            {data.materials.map((material) => (
              <option key={material} value={material}>
                {material}
              </option>
            ))}
          </select>
        }
      >
        {data.rows.length === 0 ? (
          <EmptyState message={data.notes[0]} />
        ) : (
          <DataTable
            tableId="target-management.compare"
            columns={columns}
            rows={data.rows}
            rowKey={(row) =>
              `${(row as TargetComparisonRow).level}:${(row as TargetComparisonRow).node_code}`
            }
          />
        )}
        <ul className="mt-3 space-y-1 text-xs text-slate-500 dark:text-slate-400">
          {data.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </Section>
    </div>
  );
}
