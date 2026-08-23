/**
 * The upload activity indicator, in the application header.
 *
 * Visible from every page, which is the whole point: an import runs on a server
 * worker and does not care which route the browser is on, so the place that
 * reports it must not care either. Before this existed the only sign of a
 * running import was inside the Upload Center, and navigating away took that
 * sign with it — which read as the upload having stopped.
 *
 * It renders nothing at all when there is no activity, so it costs the header
 * no space on an ordinary page.
 */

import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { useT } from '../contexts/I18nContext';
import { JOB_STATUS_LABEL, useUploadJobs } from '../hooks/useUploadJobs';
import { formatCount } from '../utils/format';

export function UploadDock() {
  const t = useT();
  const navigate = useNavigate();
  const { active, recent, primary } = useUploadJobs();

  // Nothing running and nothing just finished: the dock is not a permanent
  // fixture, it appears when there is something to say.
  if (!primary && recent.length === 0) return null;

  const job = primary ?? recent[0];
  const running = Boolean(primary);
  const failed = !running && (job.status === 'FAILED' || job.status === 'CANCELLED');

  /**
   * The server's percentage, shown only when the server has actually reported
   * one. A missing value renders as an indeterminate bar rather than a number
   * the browser made up — §7 and §23: no fake progress.
   */
  const percent = job.progress_percent;
  const counts = job.counts;

  return (
    <button
      type="button"
      onClick={() => navigate('/data-upload')}
      title={t('upload.dock.open')}
      className="flex max-w-[15rem] items-center gap-2 rounded-lg border border-slate-200 px-2 py-1 text-left transition hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
    >
      {running ? (
        <Loader2 size={16} className="shrink-0 animate-spin text-sky-600" />
      ) : failed ? (
        <XCircle size={16} className="shrink-0 text-red-600" />
      ) : (
        <CheckCircle2 size={16} className="shrink-0 text-emerald-600" />
      )}

      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-1.5">
          <span className="truncate text-xs font-medium">{job.file_name}</span>
          {running && percent != null && (
            <span className="shrink-0 text-xs tabular-nums text-slate-500">
              {percent}%
            </span>
          )}
        </span>

        <span className="block truncate text-[11px] leading-tight text-slate-500">
          {t(JOB_STATUS_LABEL[job.status])}
          {running && counts.total_records > 0 && (
            <>
              {' · '}
              {formatCount(counts.processed_records)} / {formatCount(counts.total_records)}
            </>
          )}
          {active.length > 1 && <> · {t('upload.dock.more', { count: active.length - 1 })}</>}
        </span>

        {running && (
          <span className="mt-0.5 block h-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
            <span
              className={
                percent == null
                  ? 'block h-full w-1/3 animate-pulse rounded-full bg-sky-500'
                  : 'block h-full rounded-full bg-sky-500 transition-all'
              }
              style={percent == null ? undefined : { width: `${percent}%` }}
            />
          </span>
        )}
      </span>
    </button>
  );
}
