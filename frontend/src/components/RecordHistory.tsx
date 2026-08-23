/**
 * A record's change history.
 *
 * Each line says who, when and what — and expands to the old and new value of
 * every field that moved. That expansion is the point of the panel: "Territory
 * changed" tells nobody anything, "Territory changed from TR001 to TR004" is an
 * answer.
 *
 * A blank value is rendered as an em dash rather than as nothing, so "the phone
 * number was cleared" and "the phone number was not touched" do not look
 * identical.
 */

import { ChevronDown, ChevronRight, History } from 'lucide-react';
import { useState } from 'react';
import { useT } from '../contexts/I18nContext';
import { EmptyState } from './States';
import { formatDateTime } from '../utils/format';
import type { HistoryEntry } from '../types/api';

const TONE: Record<string, string> = {
  CREATED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
  UPDATED: 'bg-sky-100 text-sky-800 dark:bg-sky-950/50 dark:text-sky-300',
  DELETED: 'bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-300',
  RESTORED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
  VOIDED: 'bg-red-100 text-red-800 dark:bg-red-950/50 dark:text-red-300',
  UNVOIDED: 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
};

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

export function RecordHistory({
  entries,
  total,
  labelFor,
}: {
  entries: HistoryEntry[];
  total?: number;
  /** Turn a column name into the label the table shows for it. */
  labelFor?: (field: string) => string;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState<number | null>(
    entries.length ? entries[0].change_id : null,
  );

  if (entries.length === 0) {
    return <EmptyState message={t('history.empty')} icon={<History size={28} />} />;
  }

  return (
    <div className="space-y-1">
      {entries.map((entry) => {
        const open = expanded === entry.change_id;
        const hasDetail = entry.changes.length > 0 || Boolean(entry.reason);
        return (
          <div
            key={entry.change_id}
            className="rounded-lg border border-slate-200 dark:border-slate-700"
          >
            <button
              type="button"
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
              onClick={() => setExpanded(open ? null : entry.change_id)}
              aria-expanded={open}
            >
              {hasDetail ? (
                open ? <ChevronDown size={14} /> : <ChevronRight size={14} />
              ) : (
                <span className="w-[14px]" />
              )}
              <span className={`badge shrink-0 ${TONE[entry.action] ?? 'badge'}`}>
                {t(`history.action.${entry.action}`)}
              </span>
              <span className="truncate text-slate-600 dark:text-slate-300">
                {entry.username ?? '—'}
                {entry.role && (
                  <span className="ml-1 text-[11px] text-slate-400">
                    ({entry.role.replace(/_/g, ' ').toLowerCase()})
                  </span>
                )}
              </span>
              <span className="ml-auto shrink-0 text-xs text-slate-400">
                {formatDateTime(entry.created_at)}
              </span>
            </button>

            {open && hasDetail && (
              <div className="border-t border-slate-100 px-3 py-2 dark:border-slate-800">
                {entry.reason && (
                  <p className="mb-2 text-xs italic text-slate-500 dark:text-slate-400">
                    {t('history.reason')}: {entry.reason}
                  </p>
                )}
                {entry.changes.length > 0 && (
                  <div className="table-wrap">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-slate-500">
                          <th scope="col" className="py-1 pr-3 font-medium">
                            {t('history.field')}
                          </th>
                          <th scope="col" className="py-1 pr-3 font-medium">
                            {t('history.before')}
                          </th>
                          <th scope="col" className="py-1 font-medium">
                            {t('history.after')}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {entry.changes.map((change) => (
                          <tr
                            key={change.field}
                            className="border-t border-slate-100 dark:border-slate-800"
                          >
                            <td className="py-1 pr-3 text-slate-600 dark:text-slate-300">
                              {labelFor?.(change.field) ?? change.field}
                            </td>
                            <td className="py-1 pr-3 text-slate-500 line-through decoration-slate-300">
                              {display(change.old)}
                            </td>
                            <td className="py-1 font-medium">{display(change.new)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {total !== undefined && total > entries.length && (
        <p className="pt-1 text-center text-xs text-slate-400">
          {t('history.more', { count: String(total - entries.length) })}
        </p>
      )}
    </div>
  );
}
