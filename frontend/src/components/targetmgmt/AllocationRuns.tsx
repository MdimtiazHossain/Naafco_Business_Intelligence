/**
 * Allocation run history, and the standing management adjustments.
 *
 * **Failures are listed beside successes**, deliberately: the question a
 * planner brings to this list is usually "why did the last attempt not work",
 * and a list showing only completed runs could not answer it.
 *
 * Two columns here are three-valued rather than boolean, and both matter.
 * *Sales data* is `true` / `false` / unknown — a run that failed before it read
 * anything never looked, which is not the same as looking and finding nothing.
 * *Allocation level* names the deepest level a run actually reached, so a
 * sub-territory allocation is never mistaken for a completed customer-level one.
 */

import { Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatDateTime, formatQuantity } from '../../utils/format';
import type {
  TargetAdjustment,
  TargetAllocationRun,
  TargetAppliedAdjustment,
} from '../../types/api';

const RUN_TONE: Record<string, string> = {
  QUEUED: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  PROCESSING: 'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-300',
  COMPLETED:
    'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
  COMPLETED_WITH_WARNINGS:
    'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  FAILED: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
  CANCELLED: 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
  NO_HISTORY: 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
};

export function AllocationRuns({ runs }: { runs: TargetAllocationRun[] }) {
  const t = useT();

  const columns = [
    {
      key: 'job_id',
      header: t('targetMgmt.runId'),
      // The uuid in full is unreadable and unhelpful in a list; the first
      // segment is enough to tell two runs apart, and the title carries the
      // whole thing for anyone who needs to quote it.
      render: (row: TargetAllocationRun) => (
        <span className="font-mono text-xs" title={row.job_id}>
          {row.job_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: 'status',
      header: t('targetMgmt.col.status'),
      render: (row: TargetAllocationRun) => (
        <span
          className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${
            RUN_TONE[row.status] ?? RUN_TONE.QUEUED
          }`}
        >
          {t(`targetMgmt.jobStatus.${row.status}`)}
        </span>
      ),
    },
    { key: 'started_by', header: t('targetMgmt.startedBy') },
    {
      key: 'started_at',
      header: t('targetMgmt.startTime'),
      render: (row: TargetAllocationRun) =>
        formatDateTime(row.started_at ?? row.created_at),
    },
    {
      key: 'completed_at',
      header: t('targetMgmt.endTime'),
      render: (row: TargetAllocationRun) => formatDateTime(row.completed_at),
    },
    {
      key: 'projected_rows',
      header: t('targetMgmt.projectedRows'),
      align: 'right' as const,
      render: (row: TargetAllocationRun) =>
        row.projected_rows === null ? '—' : formatQuantity(row.projected_rows),
    },
    {
      key: 'generated_rows',
      header: t('targetMgmt.generatedRows'),
      align: 'right' as const,
      render: (row: TargetAllocationRun) => formatQuantity(row.generated_rows),
    },
    {
      key: 'allocation_level',
      header: t('targetMgmt.allocationLevel'),
      render: (row: TargetAllocationRun) =>
        row.allocation_level
          ? t(`targetMgmt.level.${row.allocation_level}`)
          : '—',
    },
    {
      key: 'sales_data_available',
      header: t('targetMgmt.salesData'),
      render: (row: TargetAllocationRun) => {
        // Three-valued on purpose: a run that failed before reading never
        // looked, which is not the same as looking and finding nothing.
        if (row.sales_data_available === null) {
          return (
            <span
              className="cursor-help text-slate-400 dark:text-slate-500"
              title={t('targetMgmt.salesNeverChecked')}
            >
              n/a
            </span>
          );
        }
        return row.sales_data_available ? (
          <span className="text-emerald-700 dark:text-emerald-400">
            {formatQuantity(row.sales_rows_found ?? 0)}
          </span>
        ) : (
          <span className="text-amber-700 dark:text-amber-400">0</span>
        );
      },
    },
    {
      key: 'error_count',
      header: t('targetMgmt.issues'),
      align: 'right' as const,
      render: (row: TargetAllocationRun) => (
        <span className="tabular-nums">
          {row.error_count > 0 && (
            <span className="text-red-700 dark:text-red-400">
              {row.error_count}E
            </span>
          )}
          {row.error_count > 0 && row.warning_count > 0 && ' · '}
          {row.warning_count > 0 && (
            <span className="text-amber-700 dark:text-amber-400">
              {row.warning_count}W
            </span>
          )}
          {row.error_count === 0 && row.warning_count === 0 && '—'}
        </span>
      ),
    },
  ];

  return (
    <Section title={t('targetMgmt.runHistory')} className="mt-4">
      {runs.length === 0 ? (
        <EmptyState message={t('targetMgmt.noRuns')} />
      ) : (
        <DataTable
          tableId="target-management.runs"
          rows={runs}
          columns={columns}
          rowKey={(row) => (row as TargetAllocationRun).job_id}
          searchable
        />
      )}
    </Section>
  );
}

/**
 * Standing adjustments, and what the last run did with them.
 *
 * The columns the specification asks for — System Suggested, Adjustment, Final,
 * Adjustment %, Reason, Adjusted By, Adjusted Date — come from the run that
 * applied them. A stored adjustment with no run behind it yet shows its volume
 * and reason and says it applies on the next run, which is the truth: it is an
 * input, and nothing has moved.
 */
export function AdjustmentPanel({
  stored,
  applied,
  canEdit,
  editable,
  saving,
  onAdd,
  onRemove,
}: {
  stored: TargetAdjustment[];
  applied: TargetAppliedAdjustment[];
  canEdit: boolean;
  editable: boolean;
  saving: boolean;
  onAdd: (body: {
    level: string;
    node_code: string;
    adjustment_volume: number;
    reason: string;
  }) => void;
  onRemove: (adjustmentId: number) => void;
}) {
  const t = useT();
  const [level, setLevel] = useState('region');
  const [node, setNode] = useState('');
  const [volume, setVolume] = useState('');
  const [reason, setReason] = useState('');

  /** The last run's effect on each node, so the table can show all six columns. */
  const effect = new Map(
    applied
      .filter((row) => Number(row.adjustment_volume) !== 0)
      .map((row) => [`${row.level}:${row.node_code}`, row]),
  );

  const ready = node.trim() && volume.trim() && reason.trim();

  return (
    <Section title={t('targetMgmt.adjustmentsTitle')} className="mt-4">
      <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
        {t('targetMgmt.adjustmentsHelp')}
      </p>

      {stored.length === 0 ? (
        <EmptyState message={t('targetMgmt.noAdjustments')} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left dark:border-slate-700">
                <th className="px-2 py-1.5 font-medium">{t('targetMgmt.node')}</th>
                <th className="px-2 py-1.5 text-right font-medium">
                  {t('targetMgmt.systemSuggested')}
                </th>
                <th className="px-2 py-1.5 text-right font-medium">
                  {t('targetMgmt.adjustment')}
                </th>
                <th className="px-2 py-1.5 text-right font-medium">
                  {t('targetMgmt.finalTarget')}
                </th>
                <th className="px-2 py-1.5 text-right font-medium">
                  {t('targetMgmt.adjustmentPercent')}
                </th>
                <th className="px-2 py-1.5 font-medium">
                  {t('targetMgmt.reason')}
                </th>
                <th className="px-2 py-1.5 font-medium">
                  {t('targetMgmt.adjustedBy')}
                </th>
                <th className="px-2 py-1.5 font-medium">
                  {t('targetMgmt.adjustedDate')}
                </th>
                {canEdit && editable && <th />}
              </tr>
            </thead>
            <tbody>
              {stored.map((row) => {
                const ran = effect.get(`${row.level}:${row.node_code}`);
                return (
                  <tr
                    key={row.adjustment_id}
                    className="border-b border-slate-100 dark:border-slate-800"
                  >
                    <td className="px-2 py-1.5">
                      <span className="text-xs uppercase text-slate-400">
                        {t(`targetMgmt.level.${row.level}`)}
                      </span>{' '}
                      {row.node_code}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {ran ? formatQuantity(Number(ran.system_volume)) : (
                        <span
                          className="cursor-help text-slate-400"
                          title={t('targetMgmt.appliesNextRun')}
                        >
                          n/a
                        </span>
                      )}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right font-semibold tabular-nums ${
                        Number(row.adjustment_volume) >= 0
                          ? 'text-emerald-700 dark:text-emerald-400'
                          : 'text-red-600 dark:text-red-400'
                      }`}
                    >
                      {Number(row.adjustment_volume) > 0 ? '+' : ''}
                      {formatQuantity(Number(row.adjustment_volume))}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {ran ? formatQuantity(Number(ran.final_volume)) : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {ran?.adjustment_percent === null ||
                      ran?.adjustment_percent === undefined
                        ? 'n/a'
                        : `${ran.adjustment_percent.toFixed(1)}%`}
                    </td>
                    <td className="px-2 py-1.5 text-xs">{row.reason}</td>
                    <td className="px-2 py-1.5 text-xs">{row.adjusted_by ?? '—'}</td>
                    <td className="px-2 py-1.5 text-xs">
                      {formatDateTime(row.adjusted_at)}
                    </td>
                    {canEdit && editable && (
                      <td className="px-2 py-1.5">
                        <button
                          type="button"
                          className="text-slate-400 hover:text-red-600"
                          aria-label={t('targetMgmt.withdraw')}
                          onClick={() => onRemove(row.adjustment_id)}
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {canEdit && editable && (
        <div className="mt-4 grid gap-2 border-t border-slate-100 pt-4 sm:grid-cols-5 dark:border-slate-800">
          <select
            className="input"
            value={level}
            aria-label={t('targetMgmt.level.label')}
            onChange={(event) => setLevel(event.target.value)}
          >
            {['zone', 'region', 'area', 'unit', 'territory', 'sub_territory',
              'customer'].map((option) => (
              <option key={option} value={option}>
                {t(`targetMgmt.level.${option}`)}
              </option>
            ))}
          </select>
          <input
            className="input"
            placeholder={t('targetMgmt.nodeCode')}
            aria-label={t('targetMgmt.nodeCode')}
            value={node}
            onChange={(event) => setNode(event.target.value)}
          />
          <input
            className="input"
            placeholder={t('targetMgmt.adjustmentVolume')}
            aria-label={t('targetMgmt.adjustmentVolume')}
            value={volume}
            onChange={(event) => setVolume(event.target.value)}
          />
          <input
            className="input sm:col-span-1"
            placeholder={t('targetMgmt.reason')}
            aria-label={t('targetMgmt.reason')}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <button
            type="button"
            className="btn-primary"
            disabled={!ready || saving}
            onClick={() => {
              onAdd({
                level,
                node_code: node.trim(),
                adjustment_volume: Number(volume),
                reason: reason.trim(),
              });
              setNode('');
              setVolume('');
              setReason('');
            }}
          >
            {t('targetMgmt.addAdjustment')}
          </button>
        </div>
      )}
    </Section>
  );
}
