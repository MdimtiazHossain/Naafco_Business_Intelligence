/**
 * The approval chain for one version: who has signed, whose turn it is, and
 * what the last step is still waiting on.
 *
 * **The chain is drawn bottom-up, in the order it runs.** Sequence 1 is the
 * sub-territory, where a target is first questioned, and the last step is
 * Management. Drawing it top-down would look tidier and would be the wrong
 * story: a target is allocated downwards and reviewed upwards.
 *
 * **Steps that do not approve are shown and labelled.** The Sales Officer and
 * the Territory Manager sit in the chain without signing at it — they are where
 * a target is *questioned* — and hiding them would make the sequence numbers
 * look like they skipped.
 *
 * **Blockers are listed one per line, never merged.** An unbalanced
 * reconciliation, an open revision and an incomplete chain fail for different
 * reasons and are fixed in different places; one combined sentence would send a
 * reader looking in the wrong one.
 */

import { Check, CircleDot, Lock, MessageSquare, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import type { TargetApprovalState, TargetChainStep } from '../../types/api';

function roleLabel(role: string): string {
  return role
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function StepMark({ step }: { step: TargetChainStep }) {
  if (step.approved) {
    return (
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-400">
        <Check className="h-3.5 w-3.5" />
      </span>
    );
  }
  if (step.is_current) {
    return (
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-400">
        <CircleDot className="h-3.5 w-3.5" />
      </span>
    );
  }
  return (
    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500">
      {step.approval_sequence}
    </span>
  );
}

export function ApprovalPanel({
  state,
  canApprove,
  onApprove,
  onReject,
  onSendBack,
  onSubmit,
  submittable,
  busy,
  error,
}: {
  state: TargetApprovalState;
  canApprove: boolean;
  onApprove: (comment: string) => void;
  onReject: (comment: string) => void;
  onSendBack: (comment: string) => void;
  onSubmit: (comment: string) => void;
  submittable: boolean;
  busy: boolean;
  error: string | null;
}) {
  const t = useT();
  const [comment, setComment] = useState('');

  const approvedStatus = state.version?.status === 'APPROVED';

  return (
    <div className="space-y-4">
      <Section title={t('targetMgmt.approval.chainTitle')}>
        <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.approval.chainDescription')}
        </p>
        <ol className="space-y-2">
          {state.steps.map((step) => (
            <li
              key={step.role}
              className={`flex items-start gap-3 rounded-md border px-3 py-2 ${
                step.is_current
                  ? 'border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/30'
                  : 'border-slate-200 dark:border-slate-700'
              }`}
            >
              <StepMark step={step} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
                    {roleLabel(step.role)}
                  </span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {step.hierarchy_level.replace(/_/g, ' ')}
                  </span>
                  {!step.can_approve && (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                      {t('targetMgmt.approval.questionsOnly')}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                  {step.approved
                    ? t('targetMgmt.approval.approvedBy', {
                        actor: step.actor ?? '—',
                      })
                    : step.is_current
                      ? t('targetMgmt.approval.awaiting')
                      : t('targetMgmt.approval.notYet')}
                </p>
              </div>
            </li>
          ))}
        </ol>
      </Section>

      {state.blockers.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-800 dark:bg-amber-950/30">
          <p className="flex items-center gap-2 text-sm font-medium text-amber-900 dark:text-amber-300">
            <TriangleAlert className="h-4 w-4" />
            {t('targetMgmt.approval.blockedTitle')}
          </p>
          <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-sm text-amber-800 dark:text-amber-400">
            {state.blockers.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {(submittable || canApprove || approvedStatus) && (
        <Section title={t('targetMgmt.approval.actTitle')}>
          <label className="block">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
              {t('targetMgmt.approval.comment')}
            </span>
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              rows={2}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            />
            <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
              {t('targetMgmt.approval.commentHint')}
            </span>
          </label>

          <div className="mt-3 flex flex-wrap gap-2">
            {submittable && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onSubmit(comment.trim())}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {t('targetMgmt.approval.submit')}
              </button>
            )}
            {canApprove && state.my_turn && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onApprove(comment.trim())}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
              >
                {t('targetMgmt.approval.approve')}
              </button>
            )}
            {canApprove && state.my_turn && (
              <button
                type="button"
                disabled={busy || comment.trim().length === 0}
                title={
                  comment.trim().length === 0
                    ? t('targetMgmt.approval.rejectNeedsReason')
                    : undefined
                }
                onClick={() => onReject(comment.trim())}
                className="rounded-md border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40"
              >
                {t('targetMgmt.approval.reject')}
              </button>
            )}
            {canApprove && approvedStatus && (
              <button
                type="button"
                disabled={busy || comment.trim().length === 0}
                title={
                  comment.trim().length === 0
                    ? t('targetMgmt.approval.rejectNeedsReason')
                    : undefined
                }
                onClick={() => onSendBack(comment.trim())}
                className="rounded-md border border-amber-400 px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-50 disabled:opacity-50 dark:border-amber-700 dark:text-amber-400 dark:hover:bg-amber-950/40"
              >
                {t('targetMgmt.approval.sendBack')}
              </button>
            )}
          </div>

          {canApprove && !state.my_turn && state.current_role && (
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
              {t('targetMgmt.approval.notYourTurn', {
                role: roleLabel(state.current_role),
              })}
            </p>
          )}
          {state.already_acted === 'APPROVED' && (
            <p className="mt-2 flex items-center gap-1.5 text-sm text-emerald-700 dark:text-emerald-400">
              <Lock className="h-3.5 w-3.5" />
              {t('targetMgmt.approval.youApproved')}
            </p>
          )}
          {error && (
            <p className="mt-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
              {error}
            </p>
          )}
        </Section>
      )}

      {state.history.length > 0 && (
        <Section title={t('targetMgmt.approval.historyTitle')}>
          <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
            {t('targetMgmt.approval.historyDescription')}
          </p>
          <ul className="space-y-2">
            {state.history.map((act) => (
              <li key={act.approval_id} className="flex gap-3 text-sm">
                <MessageSquare className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
                <div className="min-w-0">
                  <span className="font-medium text-slate-900 dark:text-slate-100">
                    {act.action.replace(/_/g, ' ').toLowerCase()}
                  </span>
                  <span className="text-slate-500 dark:text-slate-400">
                    {' · '}
                    {act.actor ?? '—'}
                    {act.actor_role ? ` (${roleLabel(act.actor_role)})` : ''}
                    {act.node_code ? ` · ${act.node_code}` : ''}
                  </span>
                  {act.comment && (
                    <p className="text-slate-600 dark:text-slate-400">
                      {act.comment}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
