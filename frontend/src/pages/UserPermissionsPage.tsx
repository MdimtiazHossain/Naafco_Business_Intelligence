/**
 * Per-user section permissions: `/admin/users/:id/permissions`.
 *
 * One row per application section, with **radio buttons** for Allow and Deny —
 * not checkboxes. The two options are mutually exclusive by definition, and a
 * radio group says so; a checkbox would leave "neither" representable, which the
 * permission model has no meaning for.
 *
 * The section list comes from `GET /api/admin/sections`, which reads the backend
 * catalogue. Adding a section to the application makes it appear here without a
 * frontend change, so this screen can never fall behind what the API enforces.
 *
 * Saving writes the explicit overrides only. A section left at its role default
 * is stored as no row at all, so changing a role's default later still reaches
 * the users who never had an opinion recorded.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Info, Lock, RotateCcw, Save } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { adminService } from '../services';
import type { SectionAccess, SectionPermission } from '../types/api';

/** Where a decision came from, in words an administrator can act on. */
function sourceLabel(row: SectionPermission, t: (key: string) => string): string {
  if (row.locked) return t('permissions.sourceLocked');
  if (row.source === 'USER_PERMISSION') return t('permissions.sourceUser');
  if (row.source === 'ROLE_PERMISSION') return t('permissions.sourceRolePermission');
  return t('permissions.sourceRoleDefault');
}

export default function UserPermissionsPage() {
  const t = useT();
  const { id } = useParams<{ id: string }>();
  const userId = Number(id);
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ['user-permissions', userId],
    queryFn: () => adminService.userPermissions(userId),
    enabled: Number.isFinite(userId),
  });

  /** The pending choice per section. Absent means "follow the role default". */
  const [draft, setDraft] = useState<Record<string, SectionAccess>>({});
  const [saved, setSaved] = useState(false);

  // Seed the draft from the stored overrides whenever the server answer changes.
  useEffect(() => {
    if (!query.data) return;
    const explicit: Record<string, SectionAccess> = {};
    for (const row of query.data.sections) {
      if (row.user_access) explicit[row.key] = row.user_access;
    }
    setDraft(explicit);
  }, [query.data]);

  const save = useMutation({
    mutationFn: () => adminService.setUserPermissions(userId, draft),
    onSuccess: () => {
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ['user-permissions', userId] });
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
    },
  });

  const rows = query.data?.sections ?? [];

  /** What each row shows right now: the draft override, else the role default. */
  const effective = useMemo(() => {
    const map: Record<string, SectionAccess> = {};
    for (const row of rows) {
      if (row.locked) {
        map[row.key] = 'DENY';
        continue;
      }
      map[row.key] = draft[row.key] ?? row.role_access;
    }
    return map;
  }, [rows, draft]);

  const grouped = useMemo(() => {
    const groups = new Map<string, SectionPermission[]>();
    for (const row of rows) {
      groups.set(row.group, [...(groups.get(row.group) ?? []), row]);
    }
    return [...groups.entries()];
  }, [rows]);

  function choose(sectionKey: string, access: SectionAccess, roleAccess: SectionAccess) {
    setSaved(false);
    setDraft((previous) => {
      const next = { ...previous };
      // Choosing the role's own answer removes the override rather than
      // freezing today's default into this user's record.
      if (access === roleAccess) delete next[sectionKey];
      else next[sectionKey] = access;
      return next;
    });
  }

  function resetAll() {
    setSaved(false);
    setDraft({});
  }

  const user = query.data?.user;

  return (
    <>
      <PageHeader
        title={t('permissions.title')}
        description={
          user
            ? `${user.display_name} (${user.username}) · ${user.role}`
            : t('permissions.subtitle')
        }
        actions={
          <Link to="/admin/users" className="btn-ghost">
            <ArrowLeft size={14} />
            {t('permissions.backToUsers')}
          </Link>
        }
      />

      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => void query.refetch()}
        skeleton={<CardSkeleton rows={8} />}
      >
        <Section
          title={t('permissions.sectionAccess')}
          actions={
            <div className="flex gap-2">
              <button type="button" className="btn-secondary" onClick={resetAll}>
                <RotateCcw size={14} />
                {t('permissions.useRoleDefaults')}
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={save.isPending}
                onClick={() => save.mutate()}
              >
                <Save size={14} />
                {save.isPending ? t('common.saving') : t('common.save')}
              </button>
            </div>
          }
        >
          <p className="mb-3 flex items-start gap-2 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600 dark:bg-slate-800/60 dark:text-slate-300">
            <Info size={14} className="mt-0.5 shrink-0" />
            <span>{t('permissions.explainer')}</span>
          </p>

          {save.error && (
            <p className="mb-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
              {(save.error as Error).message}
            </p>
          )}
          {saved && !save.isPending && (
            <p className="mb-3 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
              {t('permissions.saved')}
            </p>
          )}

          <div className="table-wrap rounded-lg border border-slate-200 dark:border-slate-800">
            <table className="w-full min-w-max border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
                  <th scope="col" className="px-3 py-2 text-left font-medium text-slate-600 dark:text-slate-300">
                    {t('permissions.section')}
                  </th>
                  <th scope="col" className="w-24 px-3 py-2 text-center font-medium text-emerald-700 dark:text-emerald-400">
                    {t('permissions.allow')}
                  </th>
                  <th scope="col" className="w-24 px-3 py-2 text-center font-medium text-red-700 dark:text-red-400">
                    {t('permissions.deny')}
                  </th>
                  <th scope="col" className="px-3 py-2 text-left font-medium text-slate-600 dark:text-slate-300">
                    {t('permissions.source')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {grouped.map(([group, sections]) => (
                  <>
                    <tr key={group} className="bg-slate-100/70 dark:bg-slate-800/50">
                      <td
                        colSpan={4}
                        className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500"
                      >
                        {group}
                      </td>
                    </tr>
                    {sections.map((row) => {
                      const current = effective[row.key];
                      const name = `section-${row.key}`;
                      return (
                        <tr
                          key={row.key}
                          className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                        >
                          <th scope="row" className="px-3 py-2 text-left font-normal">
                            <span className="flex items-center gap-1.5 font-medium text-slate-800 dark:text-slate-100">
                              {row.label}
                              {row.locked && (
                                <Lock size={12} className="text-slate-400" />
                              )}
                            </span>
                            <span className="block text-[11px] text-slate-500">
                              {row.description}
                            </span>
                          </th>
                          <td className="px-3 py-2 text-center">
                            <input
                              type="radio"
                              name={name}
                              value="ALLOW"
                              className="h-4 w-4 accent-emerald-600"
                              checked={current === 'ALLOW'}
                              disabled={row.locked}
                              onChange={() => choose(row.key, 'ALLOW', row.role_access)}
                              aria-label={`${row.label} — ${t('permissions.allow')}`}
                            />
                          </td>
                          <td className="px-3 py-2 text-center">
                            <input
                              type="radio"
                              name={name}
                              value="DENY"
                              className="h-4 w-4 accent-red-600"
                              checked={current === 'DENY'}
                              disabled={row.locked}
                              onChange={() => choose(row.key, 'DENY', row.role_access)}
                              aria-label={`${row.label} — ${t('permissions.deny')}`}
                            />
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-500">
                            {draft[row.key]
                              ? t('permissions.sourcePending')
                              : sourceLabel(row, t)}
                          </td>
                        </tr>
                      );
                    })}
                  </>
                ))}
              </tbody>
            </table>
          </div>

          <p className="mt-3 text-xs text-slate-500">{t('permissions.scopeNote')}</p>
        </Section>

        {user && (
          <Section title={t('admin.dataScope')} className="mt-4">
            <p className="text-sm text-slate-600 dark:text-slate-300">
              {Object.keys(user.data_scope ?? {}).length === 0
                ? t('permissions.noScope')
                : Object.entries(user.data_scope)
                    .map(([level, codes]) => `${level}: ${codes.join(', ')}`)
                    .join(' · ')}
            </p>
            <p className="mt-1 text-xs text-slate-500">{t('permissions.scopeSeparate')}</p>
            <Link to="/admin/users" className="btn-secondary mt-3 inline-flex">
              {t('permissions.editScope')}
            </Link>
          </Section>
        )}
      </QueryState>
    </>
  );
}
