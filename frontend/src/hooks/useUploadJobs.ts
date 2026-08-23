import { useQuery } from '@tanstack/react-query';

import { useAuth } from '../contexts/AuthContext';
import { dataUploadService } from '../services';
import type { UploadJob, UploadStatusValue } from '../types/api';

/**
 * Every import this user has running, from wherever the user happens to be.
 *
 * **This is what makes an upload survive navigation.** The job itself always ran
 * on the server — a worker thread owns it, its state lives in `upload_batches`,
 * and nothing about it ever depended on a React component. What was missing was
 * anywhere in the UI to ask. `useUploadProgress` watches a single run through
 * the mutation that started it, so unmounting the Upload Center took the only
 * evidence with it and the import looked as though it had stopped.
 *
 * This asks the server instead, from a query that lives above the router. The
 * answer is the same on every page and after a browser refresh, because it is
 * read from the database rather than from anything the browser kept.
 *
 * The React Query cache is shared, so the dock and the Upload Center mounting
 * this hook at the same time make **one** request between them, not two.
 */

/** While something is running. Fast enough to feel live, slow enough to be cheap. */
const ACTIVE_POLL_MS = 1500;

/**
 * While nothing is. Not zero: a job can be started from another tab or by a
 * super administrator watching someone else's import, and the dock should notice
 * without the user having to reload.
 */
const IDLE_POLL_MS = 20000;

/** Statuses that mean a worker still has the job. Mirrors `UploadStatus.ACTIVE`. */
const ACTIVE_STATUSES: readonly UploadStatusValue[] = [
  'UPLOADED',
  'QUEUED',
  'VALIDATING',
  'IMPORTING',
];

export function isActiveStatus(status: UploadStatusValue): boolean {
  return ACTIVE_STATUSES.includes(status);
}

export interface UploadJobsState {
  jobs: UploadJob[];
  active: UploadJob[];
  recent: UploadJob[];
  /** The one to show in the dock: the oldest still running, so it does not hop. */
  primary: UploadJob | null;
  isLoading: boolean;
  refetch: () => void;
}

export function useUploadJobs(enabled = true): UploadJobsState {
  const { user } = useAuth();
  // Never poll for a signed-out visitor: the endpoint would 401 on a loop.
  const canQuery = enabled && Boolean(user);

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['upload-jobs'],
    queryFn: () => dataUploadService.activeJobs(),
    enabled: canQuery,
    // Polling rather than a socket: the project has no realtime transport, and
    // §8 says not to introduce one where an equivalent already works. The
    // interval is a function of the last answer, so a quiet system costs three
    // requests a minute and a busy one reports promptly.
    refetchInterval: (query) =>
      (query.state.data?.active ?? 0) > 0 ? ACTIVE_POLL_MS : IDLE_POLL_MS,
    // Keep polling while the tab is in the background: an import started and
    // then left alone should be finished when the user looks again.
    refetchIntervalInBackground: true,
    staleTime: 0,
  });

  const jobs = data?.jobs ?? [];
  const active = jobs.filter((job) => isActiveStatus(job.status));
  const recent = jobs.filter((job) => !isActiveStatus(job.status));

  return {
    jobs,
    active,
    recent,
    // The *last* of the active list, which is the oldest: `active_jobs` orders
    // by upload id descending. Pinning the dock to the oldest running job stops
    // it flicking between two imports every poll.
    primary: active.length ? active[active.length - 1] : null,
    isLoading,
    refetch: () => void refetch(),
  };
}

/**
 * What to call each status on screen.
 *
 * The database keeps its own names and this maps them, rather than a migration
 * renaming stored rows. `VALIDATED` and `PARTIAL` have no equivalent in a plain
 * queued/processing/done vocabulary and both matter: one is a file waiting for
 * someone to confirm it, the other an import that loaded some rows and rejected
 * others.
 */
export const JOB_STATUS_LABEL: Record<UploadStatusValue, string> = {
  UPLOADED: 'upload.status.queued',
  QUEUED: 'upload.status.queued',
  VALIDATING: 'upload.status.validating',
  VALIDATED: 'upload.status.validated',
  IMPORTING: 'upload.status.processing',
  COMPLETED: 'upload.status.completed',
  PARTIAL: 'upload.status.partial',
  FAILED: 'upload.status.failed',
  CANCELLED: 'upload.status.cancelled',
  ROLLED_BACK: 'upload.status.rolledBack',
};
