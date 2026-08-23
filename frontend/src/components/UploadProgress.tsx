/**
 * The upload progress indicator.
 *
 * Everything shown here is measured: the bar's position comes from bytes the
 * browser actually sent and rows the server actually processed, and the counts
 * are the same six the finished upload summary reports. When the server has not
 * yet said anything the bar goes indeterminate rather than advancing on a timer —
 * a moving bar is a claim that work is happening, and this component only makes
 * that claim when it can back it up.
 */

import { AlertTriangle, CheckCircle2, Loader2, XCircle } from 'lucide-react';

import { useT } from '../contexts/I18nContext';
import type { UploadProgressState } from '../hooks/useUploadProgress';
import type { UploadPhase } from '../types/api';

/** Phase -> i18n key. The server's vocabulary, translated for the reader. */
const PHASE_LABEL: Record<UploadPhase, string> = {
  UPLOADING: 'upload.phase.uploading',
  PREPARING: 'upload.phase.preparing',
  READING: 'upload.phase.reading',
  VALIDATING: 'upload.phase.validating',
  MAPPING: 'upload.phase.mapping',
  IMPORTING: 'upload.phase.importing',
  WRITING: 'upload.phase.writing',
  COMPLETED: 'upload.phase.completed',
  FAILED: 'upload.phase.failed',
};

function Count({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: number;
  tone?: 'neutral' | 'good' | 'bad';
}) {
  const colour =
    tone === 'good'
      ? 'text-emerald-600 dark:text-emerald-400'
      : tone === 'bad'
        ? 'text-red-600 dark:text-red-400'
        : 'text-slate-700 dark:text-slate-200';
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className={`text-sm font-semibold tabular-nums ${colour}`}>
        {value.toLocaleString()}
      </dd>
    </div>
  );
}

export function UploadProgress({
  state,
  title,
}: {
  state: UploadProgressState;
  title?: string;
}) {
  const t = useT();
  const { percent, phase, counts, awaitingServer, active, message } = state;

  const failed = phase === 'FAILED';
  const completed = phase === 'COMPLETED';
  // "Completed with errors" is its own outcome: the import succeeded and some
  // rows did not, which is neither a success nor a failure and must not be
  // reported as either.
  const withErrors = completed && counts.failed_records > 0;

  const icon = failed ? (
    <XCircle size={16} className="text-red-500" />
  ) : withErrors ? (
    <AlertTriangle size={16} className="text-amber-500" />
  ) : completed ? (
    <CheckCircle2 size={16} className="text-emerald-500" />
  ) : (
    <Loader2 size={16} className="animate-spin text-blue-500" />
  );

  const barColour = failed
    ? 'bg-red-500'
    : withErrors
      ? 'bg-amber-500'
      : completed
        ? 'bg-emerald-500'
        : 'bg-blue-500';

  const showRecords = counts.total_records > 0;

  return (
    <div
      className="rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/40"
      // Announced politely so a screen reader hears the phase changes without
      // the percentage interrupting on every tick.
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {icon}
          <span className="truncate text-sm font-medium text-slate-700 dark:text-slate-200">
            {title ?? t('upload.progress.title')}
          </span>
        </div>
        <span className="shrink-0 text-sm font-semibold tabular-nums text-slate-700 dark:text-slate-200">
          {percent}%
        </span>
      </div>

      <div
        className="mt-2 h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        // Omitted while indeterminate, which is how a progressbar says
        // "working, position unknown" rather than claiming a false position.
        aria-valuenow={awaitingServer && active ? undefined : percent}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-300 ease-out ${barColour} ${
            awaitingServer && active ? 'animate-pulse' : ''
          }`}
          style={{ width: `${percent}%` }}
        />
      </div>

      <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
        {t('upload.progress.status')}: {t(PHASE_LABEL[phase])}
      </p>

      {showRecords && (
        <>
          <p className="mt-1 text-xs text-slate-500 tabular-nums">
            {t('upload.progress.processing')}:{' '}
            {counts.processed_records.toLocaleString()} /{' '}
            {counts.total_records.toLocaleString()} {t('upload.progress.records')}
          </p>
          <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Count
              label={t('upload.progress.valid')}
              value={counts.valid_records}
              tone="good"
            />
            <Count
              label={t('upload.progress.invalid')}
              value={counts.invalid_records}
              tone={counts.invalid_records > 0 ? 'bad' : 'neutral'}
            />
            {(counts.imported_records > 0 || completed) && (
              <Count
                label={t('upload.progress.imported')}
                value={counts.imported_records}
              />
            )}
            {(counts.failed_records > 0 || completed) && (
              <Count
                label={t('upload.progress.failed')}
                value={counts.failed_records}
                tone={counts.failed_records > 0 ? 'bad' : 'neutral'}
              />
            )}
          </dl>
        </>
      )}

      {message && (
        <p className="mt-3 text-xs text-slate-600 dark:text-slate-300">{message}</p>
      )}
    </div>
  );
}

/**
 * The checklist shown once a run is over.
 *
 * Each line states what actually happened rather than ticking every box on
 * completion: a file that failed validation never reached the import, and saying
 * otherwise would be the kind of invented reassurance this product exists to
 * avoid.
 */
export function UploadCompletionSummary({
  state,
  imported,
}: {
  state: UploadProgressState;
  /** True after a commit; false when only validation has run. */
  imported: boolean;
}) {
  const t = useT();
  const { phase, counts } = state;
  if (phase !== 'COMPLETED' && phase !== 'FAILED') return null;

  const failed = phase === 'FAILED';
  const withErrors = !failed && counts.failed_records > 0;

  const steps: { label: string; done: boolean }[] = [
    { label: t('upload.done.uploaded'), done: true },
    { label: t('upload.done.validated'), done: !failed || counts.total_records > 0 },
    { label: t('upload.done.mapped'), done: !failed && counts.valid_records > 0 },
    ...(imported
      ? [{ label: t('upload.done.imported'), done: counts.imported_records > 0 }]
      : []),
  ];

  return (
    <div
      className={`mt-3 rounded-lg border p-3 ${
        failed
          ? 'border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/30'
          : withErrors
            ? 'border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/30'
            : 'border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/30'
      }`}
    >
      <p className="mb-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
        {failed
          ? t('upload.done.failedTitle')
          : withErrors
            ? t('upload.done.withErrorsTitle')
            : t('upload.done.successTitle')}
      </p>
      <ul className="space-y-1">
        {steps.map((step) => (
          <li
            key={step.label}
            className="flex items-center gap-2 text-xs text-slate-700 dark:text-slate-200"
          >
            {step.done ? (
              <CheckCircle2 size={13} className="shrink-0 text-emerald-500" />
            ) : (
              <XCircle size={13} className="shrink-0 text-slate-400" />
            )}
            {step.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
