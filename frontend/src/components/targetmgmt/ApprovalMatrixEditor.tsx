/**
 * The approval matrix: who signs at which level, in what order, within what limit.
 *
 * **Unlimited and 0% are different settings and are never both an empty cell.**
 * A blank adjustment limit means unlimited — the role may move any figure — and
 * zero means the opposite: may not change a figure at all. The input therefore
 * carries an explicit "unlimited" checkbox rather than relying on emptiness, and
 * the read-only column shows the backend's spelled-out `limit_label`.
 *
 * **A role outside the chain has no sequence, and that is not step zero.** An
 * administrator configures the workflow and signs off on nothing; the row shows
 * an em dash and is drawn beneath the chain rather than at the head of it.
 *
 * **Only changed fields are sent.** Each edited row carries `fields_present`
 * naming what the user actually touched, so a role the form never displayed
 * cannot be blanked, and "leave the sequence alone" cannot arrive looking like
 * "put this role outside the chain".
 */

import { RotateCcw, Save } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import type { TargetMatrixResponse, TargetMatrixRow } from '../../types/api';

type Draft = Record<string, Partial<TargetMatrixRow> & { touched: Set<string> }>;

function roleLabel(role: string): string {
  return role
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function ApprovalMatrixEditor({
  data,
  editable,
  onSave,
  busy,
  error,
}: {
  data: TargetMatrixResponse;
  editable: boolean;
  onSave: (
    entries: Array<Partial<TargetMatrixRow> & { role: string; fields_present: string[] }>,
  ) => void;
  busy: boolean;
  error: string | null;
}) {
  const t = useT();
  const [draft, setDraft] = useState<Draft>({});

  const rows = useMemo(() => {
    return data.rows.map((row) => ({ ...row, ...(draft[row.role] ?? {}) }));
  }, [data.rows, draft]);

  const dirty = Object.values(draft).some((entry) => entry.touched.size > 0);

  const edit = <K extends keyof TargetMatrixRow>(
    role: string,
    field: K,
    value: TargetMatrixRow[K],
  ) => {
    setDraft((current) => {
      const entry = current[role] ?? { touched: new Set<string>() };
      const touched = new Set(entry.touched);
      touched.add(field as string);
      return { ...current, [role]: { ...entry, [field]: value, touched } };
    });
  };

  const save = () => {
    onSave(
      Object.entries(draft)
        .filter(([, entry]) => entry.touched.size > 0)
        .map(([role, entry]) => {
          const { touched, ...fields } = entry;
          return { role, ...fields, fields_present: [...touched] };
        }),
    );
    setDraft({});
  };

  const restore = () => {
    const next: Draft = {};
    for (const row of data.defaults) {
      next[row.role] = {
        ...row,
        touched: new Set([
          'hierarchy_level', 'approval_sequence', 'can_edit', 'can_approve',
          'can_reject', 'can_revise', 'adjustment_limit_percent', 'is_active',
        ]),
      };
    }
    setDraft(next);
  };

  return (
    <Section title={t('targetMgmt.matrix.title')}>
      <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
        {t('targetMgmt.matrix.description')}
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-left dark:border-slate-700">
              <th className="px-3 py-2 font-medium">{t('targetMgmt.matrix.role')}</th>
              <th className="px-3 py-2 font-medium">{t('targetMgmt.matrix.level')}</th>
              <th className="px-3 py-2 font-medium">{t('targetMgmt.matrix.sequence')}</th>
              <th className="px-3 py-2 text-center font-medium">
                {t('targetMgmt.matrix.canApprove')}
              </th>
              <th className="px-3 py-2 text-center font-medium">
                {t('targetMgmt.matrix.canReject')}
              </th>
              <th className="px-3 py-2 text-center font-medium">
                {t('targetMgmt.matrix.canRevise')}
              </th>
              <th className="px-3 py-2 font-medium">{t('targetMgmt.matrix.limit')}</th>
              <th className="px-3 py-2 text-center font-medium">
                {t('targetMgmt.matrix.active')}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.role}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td className="px-3 py-2 font-medium text-slate-900 dark:text-slate-100">
                  {roleLabel(row.role)}
                </td>
                <td className="px-3 py-2">
                  {editable ? (
                    <select
                      value={row.hierarchy_level}
                      onChange={(event) =>
                        edit(row.role, 'hierarchy_level', event.target.value)
                      }
                      className="rounded border border-slate-300 px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800"
                    >
                      {data.levels.map((level) => (
                        <option key={level} value={level}>
                          {level.replace(/_/g, ' ')}
                        </option>
                      ))}
                    </select>
                  ) : (
                    row.hierarchy_level.replace(/_/g, ' ')
                  )}
                </td>
                <td className="px-3 py-2 tabular-nums">
                  {row.approval_sequence ?? (
                    <span
                      className="text-slate-400"
                      title={t('targetMgmt.matrix.outsideChain')}
                    >
                      —
                    </span>
                  )}
                </td>
                {(['can_approve', 'can_reject', 'can_revise'] as const).map(
                  (field) => (
                    <td key={field} className="px-3 py-2 text-center">
                      <input
                        type="checkbox"
                        aria-label={`${row.role} ${field}`}
                        checked={Boolean(row[field])}
                        disabled={!editable}
                        onChange={(event) =>
                          edit(row.role, field, event.target.checked)
                        }
                      />
                    </td>
                  ),
                )}
                <td className="px-3 py-2">
                  {editable ? (
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        min={0}
                        step={0.5}
                        aria-label={`${row.role} limit`}
                        value={row.adjustment_limit_percent ?? ''}
                        disabled={row.adjustment_limit_percent === null}
                        onChange={(event) =>
                          edit(
                            row.role,
                            'adjustment_limit_percent',
                            event.target.value === ''
                              ? 0
                              : Number(event.target.value),
                          )
                        }
                        className="w-20 rounded border border-slate-300 px-2 py-1 text-sm tabular-nums disabled:bg-slate-100 dark:border-slate-600 dark:bg-slate-800 dark:disabled:bg-slate-900"
                      />
                      <label className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                        <input
                          type="checkbox"
                          aria-label={`${row.role} unlimited`}
                          checked={row.adjustment_limit_percent === null}
                          onChange={(event) =>
                            edit(
                              row.role,
                              'adjustment_limit_percent',
                              event.target.checked ? null : 0,
                            )
                          }
                        />
                        {t('targetMgmt.matrix.unlimited')}
                      </label>
                    </div>
                  ) : (
                    row.limit_label
                  )}
                </td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    aria-label={`${row.role} active`}
                    checked={Boolean(row.is_active)}
                    disabled={!editable}
                    onChange={(event) =>
                      edit(row.role, 'is_active', event.target.checked)
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editable && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={!dirty || busy}
            onClick={save}
            className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" />
            {t('common.save')}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={restore}
            className="flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t('targetMgmt.matrix.restoreDefaults')}
          </button>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {t('targetMgmt.matrix.restoreHint')}
          </span>
        </div>
      )}

      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}
    </Section>
  );
}
