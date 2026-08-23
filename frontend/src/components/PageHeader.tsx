/**
 * Page title with the resolved period and optional actions.
 *
 * Showing the period the backend actually resolved matters: "This month" means
 * a specific window, and the user should be able to see which one.
 */

import type { ReactNode } from 'react';
import { formatDate } from '../utils/format';
import type { DateRange } from '../types/api';

export function PageHeader({
  title,
  period,
  actions,
  description,
}: {
  title: string;
  period?: DateRange | null;
  actions?: ReactNode;
  description?: string;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">{title}</h1>
        {period && (
          <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
            {period.label} · {formatDate(period.date_from)} – {formatDate(period.date_to)}
            {period.financial_year ? ` · ${period.financial_year}` : ''}
          </p>
        )}
        {description && (
          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">{description}</p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Section({
  title,
  actions,
  children,
  className = '',
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      <div className="card-header">
        <h2 className="card-title">{title}</h2>
        {actions}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

/** Notes the backend attached to a result — shown, never hidden. */
export function ResultNotes({ notes }: { notes?: string[] }) {
  if (!notes?.length) return null;
  return (
    <ul className="mt-3 space-y-1">
      {notes.map((note) => (
        <li key={note} className="text-xs italic text-slate-500 dark:text-slate-400">
          {note}
        </li>
      ))}
    </ul>
  );
}
