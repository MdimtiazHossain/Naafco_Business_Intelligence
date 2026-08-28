/**
 * Asking for one node's figure to be changed.
 *
 * **The volume is sent as the raw string that was typed.** Not a parsed number:
 * the backend decides what `12,5OO` means, and the answer is "nothing" rather
 * than 125. Parsing it here would make the browser the place where a target
 * quietly becomes a different number, which is the one thing this platform is
 * built not to do.
 *
 * **The change is shown as a percentage before it is sent**, because that is
 * what routes the request. A change larger than the requester's adjustment
 * limit is not refused — it is escalated past the approver it would normally
 * have reached — and somebody typing a figure is entitled to know that before
 * they commit to it, not after.
 *
 * A reason is required, and the field says why: it travels with the request and
 * is what the approver reads.
 */

import { AlertTriangle, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useT } from '../../contexts/I18nContext';
import { formatQuantity } from '../../utils/format';
import type { TargetReviewRow } from '../../types/api';

export function RevisionDialog({
  row,
  materialCode,
  limitPercent,
  onSubmit,
  onClose,
  busy,
  error,
}: {
  row: TargetReviewRow;
  materialCode: string | null;
  /** The caller's own adjustment limit; `null` means unlimited. */
  limitPercent: number | null;
  onSubmit: (body: {
    level: string;
    node_code: string;
    material_code?: string | null;
    requested_volume: string;
    reason: string;
  }) => void;
  onClose: () => void;
  busy: boolean;
  error: string | null;
}) {
  const t = useT();
  const [volume, setVolume] = useState('');
  const [reason, setReason] = useState('');

  /**
   * The change, as a percentage of the allocated figure.
   *
   * `null` whenever it cannot be computed — an unreadable entry, an empty one,
   * or an allocated figure of zero, which has no percentage. Never rendered as
   * 0%, which would read as "no change".
   */
  const change = useMemo(() => {
    const text = volume.trim().replace(/,/g, '');
    if (!text || !/^\d+(\.\d+)?$/.test(text)) return null;
    if (!row.target_volume) return null;
    return ((Number(text) - row.target_volume) / row.target_volume) * 100;
  }, [volume, row.target_volume]);

  const escalates =
    change !== null && limitPercent !== null && Math.abs(change) > limitPercent;
  const canSubmit = volume.trim().length > 0 && reason.trim().length > 0 && !busy;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t('targetMgmt.revision.title')}
    >
      <div className="w-full max-w-lg rounded-lg border border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-700">
          <div>
            <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">
              {t('targetMgmt.revision.title')}
            </h2>
            <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
              {row.node_code}
              {row.name ? ` · ${row.name}` : ''}
              {materialCode ? ` · ${materialCode}` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          <div className="rounded-md bg-slate-50 px-3 py-2 text-sm dark:bg-slate-800/60">
            <span className="text-slate-500 dark:text-slate-400">
              {t('targetMgmt.revision.systemVolume')}
            </span>
            <span className="ml-2 font-semibold tabular-nums text-slate-900 dark:text-slate-100">
              {formatQuantity(row.target_volume)}
            </span>
          </div>

          <label className="block">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
              {t('targetMgmt.revision.requestedVolume')}
            </span>
            <input
              type="text"
              inputMode="decimal"
              value={volume}
              onChange={(event) => setVolume(event.target.value)}
              placeholder={String(row.target_volume)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm tabular-nums focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            />
            <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
              {change === null
                ? t('targetMgmt.revision.volumeHint')
                : t('targetMgmt.revision.changeIs', {
                    percent: `${change >= 0 ? '+' : ''}${change.toFixed(1)}`,
                  })}
            </span>
          </label>

          {escalates && (
            <div className="flex gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {t('targetMgmt.revision.willEscalate', {
                  limit: String(limitPercent),
                })}
              </span>
            </div>
          )}

          <label className="block">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
              {t('targetMgmt.revision.reason')}
            </span>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            />
            <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
              {t('targetMgmt.revision.reasonHint')}
            </span>
          </label>

          {error && (
            <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
              {error}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-3 dark:border-slate-700">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={() =>
              onSubmit({
                level: row.level,
                node_code: row.node_code,
                material_code: materialCode,
                requested_volume: volume.trim(),
                reason: reason.trim(),
              })
            }
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {t('targetMgmt.revision.submit')}
          </button>
        </div>
      </div>
    </div>
  );
}
