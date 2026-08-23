import { useCallback, useEffect, useRef, useState } from 'react';

import { dataUploadService } from '../services';
import type { UploadBatch, UploadJobProgress, UploadPhase } from '../types/api';

/**
 * Live progress for one upload, assembled from the two things that can actually
 * be measured.
 *
 * 1. **Bytes leaving the browser.** Only the browser knows this, and only
 *    `XMLHttpRequest.upload.onprogress` reports it (see
 *    `apiClient.requestFormWithProgress`).
 * 2. **Rows processed on the server.** Only the server knows this, and it is
 *    published against a job token this hook mints and hands to the upload call.
 *
 * Neither half is a timer and neither is interpolated when no news arrives: if
 * the server stops reporting, the bar stops moving. That is the point — a bar
 * that keeps sliding while nothing happens is worse than no bar, because it
 * tells the operator a file is progressing when it may be stuck.
 */

/** Where the send ends and the processing begins on the combined bar. */
const UPLOAD_SHARE = 0.2;

/** How often the job is polled while a run is in flight. */
const POLL_MS = 700;

export interface UploadProgressState {
  /** True from the moment a run starts until its request settles. */
  active: boolean;
  /**
   * True once a run has begun, and stays true afterwards so the finished counts
   * remain on screen. `phase` cannot serve this purpose: it legitimately returns
   * to `PREPARING` once the bytes are sent and the server has yet to report.
   */
  started: boolean;
  /** 0-100 across both halves of the job. */
  percent: number;
  phase: UploadPhase;
  /** Server-reported counts; zeroes until the server has read the file. */
  counts: {
    total_records: number;
    processed_records: number;
    valid_records: number;
    invalid_records: number;
    imported_records: number;
    failed_records: number;
  };
  /**
   * True once the file is sent but the server has not yet reported anything.
   * The UI shows an indeterminate bar for this, rather than inventing a number.
   */
  awaitingServer: boolean;
  message: string | null;
}

const EMPTY_COUNTS = {
  total_records: 0,
  processed_records: 0,
  valid_records: 0,
  invalid_records: 0,
  imported_records: 0,
  failed_records: 0,
};

const IDLE: UploadProgressState = {
  active: false,
  started: false,
  percent: 0,
  phase: 'PREPARING',
  counts: EMPTY_COUNTS,
  awaitingServer: false,
  message: null,
};

/**
 * `crypto.randomUUID` is required by the API, which only tracks tokens of that
 * shape. It is available in every browser this app targets and over HTTPS or
 * localhost; the fallback keeps a plain-HTTP LAN deployment working rather than
 * throwing where the feature is merely cosmetic.
 */
function newJobId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
    const random = (Math.random() * 16) | 0;
    const value = char === 'x' ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

export function useUploadProgress() {
  const [state, setState] = useState<UploadProgressState>(IDLE);
  const jobId = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  /** Guards against a poll that resolves after the run has already finished. */
  const running = useRef(false);

  const stopPolling = useCallback(() => {
    if (timer.current !== null) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  // A component unmounted mid-upload must not leave an interval running.
  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback(async () => {
    const id = jobId.current;
    if (!id || !running.current) return;
    let job: UploadJobProgress;
    try {
      job = await dataUploadService.jobProgress(id);
    } catch {
      // A failed poll is not a failed upload. The upload request is the source
      // of truth; this one just goes quiet and tries again on the next tick.
      return;
    }
    if (!running.current || !job.known) return;

    setState((previous) => ({
      ...previous,
      // The server's percentage covers the part of the bar after the send, and
      // never moves backwards: a late-arriving poll must not rewind the bar.
      percent: Math.max(
        previous.percent,
        Math.round(UPLOAD_SHARE * 100 + (job.percent ?? 0) * (1 - UPLOAD_SHARE)),
      ),
      phase: job.phase ?? previous.phase,
      counts: job.counts ?? previous.counts,
      awaitingServer: false,
      message: job.message ?? previous.message,
    }));
  }, []);

  /** Called when the first byte of a new run is about to be sent. */
  const start = useCallback((): string => {
    stopPolling();
    const id = newJobId();
    jobId.current = id;
    running.current = true;
    setState({ ...IDLE, active: true, started: true, phase: 'UPLOADING',
               counts: EMPTY_COUNTS });
    timer.current = setInterval(() => void poll(), POLL_MS);
    return id;
  }, [poll, stopPolling]);

  /** Bytes sent, 0-100. Occupies the first slice of the combined bar. */
  const onUploadProgress = useCallback((percent: number) => {
    setState((previous) => {
      if (!previous.active) return previous;
      const sent = Math.round(percent * UPLOAD_SHARE);
      return {
        ...previous,
        percent: Math.max(previous.percent, sent),
        // Once the bytes are gone the browser has nothing left to measure and
        // the server may not have spoken yet, so the bar is honestly unknown.
        awaitingServer: percent >= 100 && previous.phase === 'UPLOADING',
        phase: previous.phase === 'UPLOADING' && percent >= 100
          ? 'PREPARING'
          : previous.phase,
      };
    });
  }, []);

  /**
   * Called once the upload request settles, either way.
   *
   * The final counts are taken from the response rather than from a poll. A
   * small file can finish well inside one poll interval, so the last thing the
   * registry published may be nothing at all — and the response carries the
   * authoritative totals regardless. Polling is what makes the bar move *during*
   * the run; it is not what the finished figures depend on.
   */
  const finish = useCallback(
    (outcome: {
      failed: boolean;
      message?: string | null;
      totals?: UploadBatch['totals'];
    }) => {
      running.current = false;
      stopPolling();
      setState((previous) => ({
        ...previous,
        active: false,
        percent: 100,
        phase: outcome.failed ? 'FAILED' : 'COMPLETED',
        awaitingServer: false,
        message: outcome.message ?? previous.message,
        counts: outcome.totals
          ? {
              total_records: outcome.totals.total_rows,
              processed_records: outcome.totals.total_rows,
              valid_records: outcome.totals.valid_rows,
              invalid_records: outcome.totals.invalid_rows,
              imported_records:
                outcome.totals.inserted_rows + outcome.totals.updated_rows,
              failed_records: outcome.totals.invalid_rows,
            }
          : previous.counts,
      }));
    },
    [stopPolling],
  );

  /** Clear the indicator entirely, e.g. when a different file is chosen. */
  const reset = useCallback(() => {
    running.current = false;
    jobId.current = null;
    stopPolling();
    setState(IDLE);
  }, [stopPolling]);

  /**
   * Switch to the job id the **server** gave this run.
   *
   * The token `start` mints is the browser's own and nothing on the server knows
   * it: neither the preview nor the commit endpoint accepts a client-supplied
   * job id, so each mints its own `upload_uuid` and publishes progress against
   * that. Polling the browser's token therefore only ever answered
   * `known: false`, and cancelling with it answered "Import job not found".
   *
   * The real id arrives with the 202 that acknowledges the upload, which is the
   * first moment it exists. From here the poll reports real phases and counts,
   * and the cancel endpoint has something to act on.
   */
  const adopt = useCallback((serverJobId: string) => {
    if (running.current) jobId.current = serverJobId;
  }, []);

  /**
   * The token of the run currently being watched, or null when none is.
   *
   * A getter rather than a value: the id lives in a ref because changing it must
   * not re-render, and anything acting on the live run (cancelling it) only needs
   * it at the moment of the click.
   */
  const currentJobId = useCallback(() => jobId.current, []);

  return { state, start, adopt, onUploadProgress, finish, reset, currentJobId };
}
