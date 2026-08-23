/**
 * Active and recent imports, read from the server.
 *
 * The Upload Center's answer to "what is running?" after the user has been
 * somewhere else. It takes nothing from component state and nothing from
 * storage: every field here comes from `GET /api/data-upload/jobs`, which reads
 * `upload_batches`. That is what makes it survive a route change, an unmount and
 * a browser refresh alike — after F5 this panel is populated by the same request
 * as on any other visit, because the job never lived in the browser.
 */

import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';

import { useT } from '../contexts/I18nContext';
import { JOB_STATUS_LABEL, useUploadJobs } from '../hooks/useUploadJobs';
import { dataUploadService } from '../services';
import { Section } from './PageHeader';
import { formatCount, formatDateTime } from '../utils/format';
import type { UploadJob } from '../types/api';

function JobRow({ job, onCancelled }: { job: UploadJob; onCancelled: () => void }) {
  const t = useT();
  const running = job.is_active;
  const failed = job.status === 'FAILED' || job.status === 'CANCELLED';
  const percent = job.progress_percent;

  const cancel = useMutation({
    mutationFn: () => dataUploadService.cancelJob(job.job_id),
    // Either way the server is the authority on what happened next, so the only
    // thing to do here is ask again. A 409 means it finished first, which is a
    // real outcome rather than an error to show.
    onSettled: onCancelled,
  });

  return (
    <div className="flex items-start gap-3 border-b border-slate-100 py-3 last:border-0 dark:border-slate-800">
      <span className="mt-0.5">
        {running ? (
          <Loader2 size={16} className="animate-spin text-sky-600" />
        ) : failed ? (
          <XCircle size={16} className="text-red-600" />
        ) : (
          <CheckCircle2 size={16} className="text-emerald-600" />
        )}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="truncate text-sm font-medium">{job.file_name}</span>
          <span className="text-xs text-slate-500">
            {t(JOB_STATUS_LABEL[job.status])}
            {job.stage && running && <> · {job.stage}</>}
          </span>
          {running && percent != null && (
            <span className="text-xs tabular-nums text-slate-500">{percent}%</span>
          )}
        </div>

        <div className="mt-0.5 text-[11px] text-slate-500">
          {job.counts.total_records > 0 && (
            <>
              {formatCount(job.counts.processed_records)} /{' '}
              {formatCount(job.counts.total_records)} {t('upload.activity.rows')} ·{' '}
            </>
          )}
          {t('upload.activity.startedAt')} {formatDateTime(job.started_at)}
          {/* The Import Job ID, so a support conversation can name the run. */}
          <span className="ml-1 font-mono text-[10px] text-slate-400">
            {job.job_id.slice(0, 8)}
          </span>
        </div>

        {running && (
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
            <div
              className={
                percent == null
                  ? 'h-full w-1/3 animate-pulse rounded-full bg-sky-500'
                  : 'h-full rounded-full bg-sky-500 transition-all'
              }
              style={percent == null ? undefined : { width: `${percent}%` }}
            />
          </div>
        )}

        {!running && job.message && (
          <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">{job.message}</p>
        )}
      </div>

      {job.can_cancel && (
        <button
          type="button"
          className="btn-ghost shrink-0 px-2 py-1 text-xs text-red-600"
          disabled={cancel.isPending}
          onClick={() => cancel.mutate()}
        >
          {cancel.isPending ? t('upload.activity.cancelling') : t('common.cancel')}
        </button>
      )}
    </div>
  );
}

export function UploadActivity() {
  const t = useT();
  const { active, recent, refetch } = useUploadJobs();

  if (active.length === 0 && recent.length === 0) return null;

  return (
    <Section title={t('upload.activity.title')}>
      <p className="mb-2 text-xs text-slate-500">{t('upload.activity.note')}</p>
      {[...active, ...recent].map((job) => (
        <JobRow key={job.job_id} job={job} onCancelled={refetch} />
      ))}
    </Section>
  );
}
