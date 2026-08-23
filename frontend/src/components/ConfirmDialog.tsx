/**
 * The confirmation shown before anything is removed.
 *
 * Three things are always on it, because a confirmation that says only "Are you
 * sure?" is a button people learn to click without reading:
 *
 * * **what** — the record's name, its code and its type, so the user can see
 *   they are about to remove the row they think they are;
 * * **what depends on it** — the counts the backend returned, so retiring a
 *   customer with twelve hundred transactions does not feel the same as
 *   retiring an empty one;
 * * **what will actually happen** — "deactivated and can be restored", or
 *   "voided and removed from every report", never the word "delete" for
 *   something that is not one.
 *
 * When the backend requires a reason the dialog collects it and will not
 * confirm without one, because that is the API's rule and discovering it as a
 * 422 after pressing the button is a worse way to learn it.
 */

import { AlertTriangle, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useT } from '../contexts/I18nContext';
import { Modal } from './Modal';
import type { Dependants } from '../types/api';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** What the operation does, in plain words. */
  consequence: string;
  recordLabel?: string | null;
  recordCode?: string;
  entityLabel?: string;
  /**
   * The question itself. Defaults to the removal wording this dialog was built
   * for; anything that is not a removal must say what it actually does, because
   * "remove this record?" over a running import describes the wrong action.
   */
  question?: string;
  dependants?: Dependants | null;
  /** Prompt for a reason; when required, Confirm stays disabled until given. */
  reason?: 'none' | 'optional' | 'required';
  confirmLabel: string;
  /** Red for anything irreversible or accounting-affecting. */
  tone?: 'danger' | 'warning';
  busy?: boolean;
  error?: string | null;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  consequence,
  recordLabel,
  recordCode,
  entityLabel,
  question,
  dependants,
  reason = 'none',
  confirmLabel,
  tone = 'warning',
  busy = false,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useT();
  const [text, setText] = useState('');

  // A reason typed for one record must not survive into the next dialog.
  useEffect(() => {
    if (open) setText('');
  }, [open]);

  const blocked = dependants?.blocking ?? false;
  const missingReason = reason === 'required' && text.trim().length < 3;

  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className={tone === 'danger' ? 'btn-danger' : 'btn-primary'}
            disabled={busy || blocked || missingReason}
            onClick={() => onConfirm(text.trim())}
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p className="flex items-start gap-2">
          <AlertTriangle
            size={18}
            className={`mt-0.5 shrink-0 ${
              tone === 'danger' ? 'text-red-500' : 'text-amber-500'
            }`}
          />
          <span>{question ?? t('confirm.question')}</span>
        </p>

        {(recordLabel || recordCode) && (
          <dl className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-700">
            {recordLabel && (
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">{t('confirm.record')}</dt>
                <dd className="truncate font-medium">{recordLabel}</dd>
              </div>
            )}
            {recordCode && (
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">{t('confirm.code')}</dt>
                <dd className="font-mono text-xs">{recordCode}</dd>
              </div>
            )}
            {entityLabel && (
              <div className="flex justify-between gap-3">
                <dt className="text-slate-500">{t('confirm.type')}</dt>
                <dd>{entityLabel}</dd>
              </div>
            )}
          </dl>
        )}

        <p className="text-xs text-slate-500 dark:text-slate-400">{consequence}</p>

        {dependants && dependants.total > 0 && (
          <div
            className={`rounded-lg border-l-4 p-3 text-xs ${
              blocked
                ? 'border-l-red-500 bg-red-50 text-red-800 dark:bg-red-950/30 dark:text-red-200'
                : 'border-l-amber-500 bg-amber-50 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200'
            }`}
          >
            <p className="font-medium">
              {blocked ? t('confirm.blocked') : t('confirm.dependants')}
            </p>
            <ul className="mt-1 space-y-0.5">
              {Object.entries(dependants.counts).map(([label, count]) => (
                <li key={label}>
                  {count.toLocaleString()} {label}
                </li>
              ))}
            </ul>
          </div>
        )}

        {reason !== 'none' && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {reason === 'required' ? t('confirm.reasonRequired') : t('confirm.reason')}
            </span>
            <textarea
              className="input min-h-[4rem]"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={t('confirm.reasonPlaceholder')}
              maxLength={500}
            />
          </label>
        )}

        {error && (
          <p role="alert" className="text-xs text-red-600 dark:text-red-400">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
