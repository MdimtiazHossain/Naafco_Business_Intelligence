/**
 * My Approvals: everything waiting on this person, and nothing waiting on
 * anybody else.
 *
 * **Two lists, not one merged queue.** Signing off on a whole target and
 * granting one figure are different acts with different consequences, and a
 * shared row shape would have to blur them. They are drawn as two sections with
 * their own columns.
 *
 * **A version whose turn has not come is shown, and says who it is with.** The
 * alternative — hiding it — leaves an approver wondering whether a target they
 * know exists has gone missing. Naming the step it is with answers the question
 * without them having to ask anyone.
 *
 * **A role outside the chain gets an explanation, not an empty screen.** An
 * administrator legitimately holds this section, configures the matrix and
 * approves nothing; a blank list would read as a fault.
 */

import { ArrowRight, Inbox, TriangleAlert } from 'lucide-react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { formatQuantity } from '../../utils/format';
import type { TargetApprovalQueue } from '../../types/api';

function roleLabel(role: string): string {
  return role
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function MyApprovals({
  data,
  onOpenVersion,
  onDecide,
  busy,
}: {
  data: TargetApprovalQueue;
  onOpenVersion: (planId: number, versionId: number) => void;
  onDecide: (
    versionId: number,
    revisionId: number,
    approve: boolean,
  ) => void;
  busy: boolean;
}) {
  const t = useT();
  const empty = data.versions.length === 0 && data.revisions.length === 0;

  return (
    <div className="space-y-4">
      {data.matrix && (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.queue.yourStep', {
            role: roleLabel(data.role),
            level: data.matrix.hierarchy_level.replace(/_/g, ' '),
            limit: data.matrix.limit_label,
          })}
        </p>
      )}

      {empty && (
        <EmptyState
          icon={<Inbox size={32} />}
          message={data.notes[0] ?? t('targetMgmt.queue.emptyMessage')}
        />
      )}

      {data.versions.length > 0 && (
        <Section title={t('targetMgmt.queue.versionsTitle')}>
          <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
            {t('targetMgmt.queue.versionsDescription')}
          </p>
          <ul className="space-y-2">
            {data.versions.map((entry) => (
              <li
                key={entry.version.version_id}
                className={`rounded-md border px-3 py-2.5 ${
                  entry.is_my_turn
                    ? 'border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/30'
                    : 'border-slate-200 dark:border-slate-700'
                }`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div>
                    <span className="font-medium text-slate-900 dark:text-slate-100">
                      {entry.plan.plan_code} · V{entry.version.version_no}
                    </span>
                    <span className="ml-2 text-sm text-slate-500 dark:text-slate-400">
                      {entry.plan.financial_year} · {entry.plan.target_period}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      onOpenVersion(entry.plan.plan_id, entry.version.version_id)
                    }
                    className="flex items-center gap-1 text-sm font-medium text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {t('targetMgmt.queue.open')}
                    <ArrowRight className="h-3.5 w-3.5" />
                  </button>
                </div>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                  {entry.is_my_turn
                    ? t('targetMgmt.queue.yourTurn')
                    : t('targetMgmt.queue.waitingOn', {
                        roles: entry.waiting_on.map(roleLabel).join(', '),
                      })}
                </p>
                {entry.blockers.length > 0 && (
                  <p className="mt-1 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
                    <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{entry.blockers.join(' ')}</span>
                  </p>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {data.revisions.length > 0 && (
        <Section title={t('targetMgmt.queue.revisionsTitle')}>
          <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
            {t('targetMgmt.queue.revisionsDescription')}
          </p>
          <ul className="space-y-2">
            {data.revisions.map((revision) => (
              <li
                key={revision.revision_id}
                className="rounded-md border border-slate-200 px-3 py-2.5 dark:border-slate-700"
              >
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium text-slate-900 dark:text-slate-100">
                    {revision.node_code}
                    {revision.node_name ? ` · ${revision.node_name}` : ''}
                  </span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {revision.plan_code} · V{revision.version_no}
                  </span>
                  {revision.status === 'ESCALATED' && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
                      {t('targetMgmt.queue.escalated')}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm tabular-nums text-slate-700 dark:text-slate-300">
                  {formatQuantity(revision.system_volume)}
                  {' → '}
                  {formatQuantity(revision.requested_volume)}
                  {revision.change_percent !== null && (
                    <span className="ml-1.5 text-slate-500 dark:text-slate-400">
                      ({revision.change_percent >= 0 ? '+' : ''}
                      {revision.change_percent.toFixed(1)}%)
                    </span>
                  )}
                </p>
                <p className="mt-0.5 text-sm text-slate-600 dark:text-slate-400">
                  {revision.reason}
                </p>
                <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                  {t('targetMgmt.queue.requestedBy', {
                    actor: revision.requested_by ?? '—',
                  })}
                </p>
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      onDecide(revision.version_id!, revision.revision_id, true)
                    }
                    className="rounded-md bg-emerald-600 px-2.5 py-1 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                  >
                    {t('targetMgmt.queue.grant')}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      onDecide(revision.version_id!, revision.revision_id, false)
                    }
                    className="rounded-md border border-red-300 px-2.5 py-1 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40"
                  >
                    {t('targetMgmt.queue.refuse')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
