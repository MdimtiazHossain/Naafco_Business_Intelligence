/**
 * Loading, empty and error states.
 *
 * Every report renders one of four things: a skeleton, data, an empty message,
 * or an error with a way to retry. Sharing these keeps that promise consistent
 * and stops a page from silently showing nothing.
 */

import { AlertTriangle, Inbox, Lock, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';
import { useT } from '../contexts/I18nContext';
import { ApiError } from '../services';

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />;
}

export function CardSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="card p-4" aria-busy="true">
      <Skeleton className="mb-4 h-4 w-32" />
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, index) => (
          <Skeleton key={index} className="h-8 w-full" />
        ))}
      </div>
    </div>
  );
}

/**
 * Placeholder KPI cards.
 *
 * `columns` is the grid the real cards will land in, so the page does not
 * reflow when they arrive — a six-card strip loading in four columns shows two
 * holes and then closes them. Spelled out rather than interpolated, because
 * Tailwind only emits the class names it can see in the source.
 */
const SKELETON_COLUMNS: Record<number, string> = {
  3: 'lg:grid-cols-3',
  4: 'lg:grid-cols-4',
  5: 'lg:grid-cols-5',
};

export function KpiSkeleton({ count = 4, columns = 4 }: { count?: number; columns?: number }) {
  return (
    <div
      className={`grid grid-cols-2 gap-3 ${SKELETON_COLUMNS[columns] ?? SKELETON_COLUMNS[4]}`}
      aria-busy="true"
    >
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="card p-4">
          <Skeleton className="mb-3 h-3 w-20" />
          <Skeleton className="mb-2 h-7 w-28" />
          <Skeleton className="h-3 w-16" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ message, icon }: { message?: string; icon?: ReactNode }) {
  const t = useT();
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
      <div className="text-slate-400 dark:text-slate-600">{icon ?? <Inbox size={32} />}</div>
      <p className="text-sm text-slate-500 dark:text-slate-400">
        {message ?? t('common.noData')}
      </p>
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const t = useT();
  const apiError = error instanceof ApiError ? error : null;

  // A permission refusal is a normal outcome, not a fault — say so plainly and
  // do not offer a pointless retry.
  if (apiError?.isForbidden) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
        <Lock size={32} className="text-amber-500" />
        <p className="max-w-md text-sm text-slate-600 dark:text-slate-300">
          {apiError.message || t('error.forbidden')}
        </p>
      </div>
    );
  }

  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-3 px-4 py-12 text-center"
    >
      <AlertTriangle size={32} className="text-red-500" />
      <p className="max-w-md text-sm text-slate-600 dark:text-slate-300">
        {apiError?.message ?? t('common.error')}
      </p>
      {onRetry && (
        <button type="button" className="btn-secondary" onClick={onRetry}>
          <RefreshCw size={14} />
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}

/**
 * One place that decides which of the four states to show.
 * `isEmpty` is passed in because "empty" means something different per report.
 */
export function QueryState({
  isLoading,
  error,
  isEmpty,
  onRetry,
  skeleton,
  emptyMessage,
  children,
}: {
  isLoading: boolean;
  error: unknown;
  isEmpty?: boolean;
  onRetry?: () => void;
  skeleton?: ReactNode;
  emptyMessage?: string;
  children: ReactNode;
}) {
  if (isLoading) return <>{skeleton ?? <CardSkeleton />}</>;
  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (isEmpty) return <EmptyState message={emptyMessage} />;
  return <>{children}</>;
}
