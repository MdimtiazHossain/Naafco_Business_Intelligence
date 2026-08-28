/**
 * Locking: the control that turns an agreed target into *the* target.
 *
 * **What it will do is stated before it is pressed, not after.** The panel
 * names the batch a lock writes under and lists every condition still unmet,
 * because the act is the one thing in this module that reaches outside it —
 * from the moment it lands, the Target page, every export and the AI assistant
 * are reading these figures.
 *
 * **Blockers are listed one per line and never merged.** A missing transfer
 * price is a Material Master upload, an unbalanced allocation is a re-run, a
 * branch stopping above territory is customer mapping. One combined sentence
 * would send a reader to the wrong place.
 *
 * **The button is absent rather than disabled when the reader cannot lock.** A
 * control whose only outcome is a refusal teaches people to ignore controls.
 */

import { Lock, ShieldCheck, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import { formatDateTime } from '../../utils/format';
import type { TargetLockState } from '../../types/api';

export function LockPanel({
  state,
  canLock,
  onLock,
  busy,
  error,
}: {
  state: TargetLockState;
  canLock: boolean;
  onLock: (comment: string) => void;
  busy: boolean;
  error: string | null;
}) {
  const t = useT();
  const [comment, setComment] = useState('');

  if (state.locked_at) {
    return (
      <Section title={t('targetMgmt.lock.lockedTitle')}>
        <p className="flex items-start gap-2 text-sm text-emerald-700 dark:text-emerald-400">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {t('targetMgmt.lock.lockedOn', {
              when: formatDateTime(state.locked_at),
            })}
          </span>
        </p>
        {state.locked_batch_id && (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {t('targetMgmt.lock.batch', { batch: state.locked_batch_id })}
          </p>
        )}
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.lock.lockedHelp')}
        </p>
      </Section>
    );
  }

  return (
    <Section title={t('targetMgmt.lock.title')}>
      <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
        {t('targetMgmt.lock.description')}
      </p>

      {state.blockers.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-800 dark:bg-amber-950/30">
          <p className="flex items-center gap-2 text-sm font-medium text-amber-900 dark:text-amber-300">
            <TriangleAlert className="h-4 w-4" />
            {t('targetMgmt.lock.blockedTitle')}
          </p>
          <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-sm text-amber-800 dark:text-amber-400">
            {state.blockers.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {canLock && state.lockable && (
        <div className="mt-3">
          <label className="block">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
              {t('targetMgmt.lock.comment')}
            </span>
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              rows={2}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => onLock(comment.trim())}
            className="mt-3 flex items-center gap-1.5 rounded-md bg-slate-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-900 disabled:opacity-50 dark:bg-slate-700 dark:hover:bg-slate-600"
          >
            <Lock className="h-3.5 w-3.5" />
            {t('targetMgmt.lock.action')}
          </button>
        </div>
      )}

      {!canLock && state.locks_role && (
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.lock.heldBy', {
            role: state.locks_role
              .toLowerCase()
              .split('_')
              .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
              .join(' '),
          })}
        </p>
      )}

      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}
    </Section>
  );
}
