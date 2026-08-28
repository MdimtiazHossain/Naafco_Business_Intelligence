/**
 * The business audit trail: what a figure was, what it became, and why.
 *
 * Separate from the security log an administrator reads beside a login or a
 * permission change — this is the planner's record of the decisions taken on a
 * target, and neither answers the other's question.
 *
 * **Filtering happens on the server.** A plan's trail grows without bound —
 * every country-target edit, every allocation, every revision, every signature,
 * every lock — and shipping all of it so the browser could hide most of it
 * would get slower exactly as the record it exists to keep gets more valuable.
 *
 * **Old and new sit side by side.** They are what a reader compares, and a
 * merged "changed to X" line would lose the half that says what was there
 * before. A blank is drawn as a dash only where there genuinely was no prior
 * value — a plan being created has no "before".
 */

import { History } from 'lucide-react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatDateTime } from '../../utils/format';
import type { TargetAuditEntry, TargetAuditResponse } from '../../types/api';

function actionLabel(action: string): string {
  return action
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function AuditTrail({
  data,
  action,
  onActionChange,
  page,
  onPageChange,
  pageSize,
}: {
  data: TargetAuditResponse;
  action: string | null;
  onActionChange: (action: string | null) => void;
  page: number;
  onPageChange: (page: number) => void;
  pageSize: number;
}) {
  const t = useT();
  const lastPage = Math.max(0, Math.ceil(data.total / pageSize) - 1);

  const columns = [
    {
      key: 'occurred_at',
      header: t('targetMgmt.audit.when'),
      render: (row: TargetAuditEntry) =>
        row.occurred_at ? formatDateTime(row.occurred_at) : '—',
    },
    {
      key: 'action',
      header: t('targetMgmt.audit.action'),
      render: (row: TargetAuditEntry) => (
        <span className="font-medium text-slate-900 dark:text-slate-100">
          {actionLabel(row.action)}
        </span>
      ),
    },
    {
      key: 'actor',
      header: t('targetMgmt.audit.actor'),
      render: (row: TargetAuditEntry) => (
        <span>
          {row.actor ?? '—'}
          {row.actor_role && (
            <span className="ml-1 text-xs text-slate-500 dark:text-slate-400">
              {actionLabel(row.actor_role)}
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'node_label',
      header: t('targetMgmt.audit.what'),
      render: (row: TargetAuditEntry) => row.node_label ?? '—',
    },
    {
      key: 'old_value',
      header: t('targetMgmt.audit.from'),
      render: (row: TargetAuditEntry) => (
        <span className="tabular-nums text-slate-500 dark:text-slate-400">
          {row.old_value ?? '—'}
        </span>
      ),
    },
    {
      key: 'new_value',
      header: t('targetMgmt.audit.to'),
      render: (row: TargetAuditEntry) => (
        <span className="tabular-nums text-slate-900 dark:text-slate-100">
          {row.new_value ?? '—'}
        </span>
      ),
    },
    {
      key: 'reason',
      header: t('targetMgmt.audit.reason'),
      render: (row: TargetAuditEntry) => row.reason ?? '—',
    },
  ];

  return (
    <Section
      title={t('targetMgmt.audit.title')}
      actions={
        <select
          className="input"
          aria-label={t('targetMgmt.audit.action')}
          value={action ?? ''}
          onChange={(event) => onActionChange(event.target.value || null)}
        >
          <option value="">{t('targetMgmt.audit.allActions')}</option>
          {data.actions.map((name) => (
            <option key={name} value={name}>
              {actionLabel(name)}
            </option>
          ))}
        </select>
      }
    >
      <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
        {t('targetMgmt.audit.description')}
      </p>

      {data.rows.length === 0 ? (
        <EmptyState
          icon={<History size={32} />}
          message={t('targetMgmt.audit.empty')}
        />
      ) : (
        <>
          <DataTable
            tableId="target-management.audit"
            columns={columns}
            rows={data.rows}
            rowKey={(row: TargetAuditEntry) => String(row.audit_id)}
          />
          <div className="mt-3 flex items-center justify-between text-sm">
            <span className="text-slate-500 dark:text-slate-400">
              {t('targetMgmt.audit.showing', {
                from: String(page * pageSize + 1),
                to: String(page * pageSize + data.rows.length),
                total: String(data.total),
              })}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={page === 0}
                onClick={() => onPageChange(page - 1)}
                className="rounded-md border border-slate-300 px-2.5 py-1 font-medium text-slate-700 disabled:opacity-40 dark:border-slate-600 dark:text-slate-300"
              >
                {t('common.previous')}
              </button>
              <button
                type="button"
                disabled={page >= lastPage}
                onClick={() => onPageChange(page + 1)}
                className="rounded-md border border-slate-300 px-2.5 py-1 font-medium text-slate-700 disabled:opacity-40 dark:border-slate-600 dark:text-slate-300"
              >
                {t('common.next')}
              </button>
            </div>
          </div>
        </>
      )}
    </Section>
  );
}
