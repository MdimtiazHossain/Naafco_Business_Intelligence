/**
 * Role management: `/admin/roles`.
 *
 * A role is separate from a user: it sets the *default* section access and the
 * ceiling a per-user override can never lift. This screen shows the whole
 * role × section matrix at a glance, and lets a super administrator change one
 * role's defaults — which is a far broader change than a per-user override, so
 * only that one role may make it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Info, Lock, Save, Users } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useAuth } from '../contexts/AuthContext';
import { useT } from '../contexts/I18nContext';
import { adminService } from '../services';
import type { SectionAccess } from '../types/api';

export default function AdminRolesPage() {
  const t = useT();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const isSuperAdmin = user?.role === 'SUPER_ADMIN';

  const rolesQuery = useQuery({ queryKey: ['admin-roles'], queryFn: adminService.roles });
  const [selectedRole, setSelectedRole] = useState<string>('');

  const detailQuery = useQuery({
    queryKey: ['role-permissions', selectedRole],
    queryFn: () => adminService.rolePermissions(selectedRole),
    enabled: Boolean(selectedRole),
  });

  const [draft, setDraft] = useState<Record<string, SectionAccess>>({});
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!detailQuery.data) return;
    const next: Record<string, SectionAccess> = {};
    for (const row of detailQuery.data.sections) {
      if (!row.locked) next[row.key] = row.access;
    }
    setDraft(next);
  }, [detailQuery.data]);

  const save = useMutation({
    mutationFn: () => adminService.setRolePermissions(selectedRole, draft),
    onSuccess: () => {
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ['role-permissions', selectedRole] });
      void queryClient.invalidateQueries({ queryKey: ['admin-roles'] });
    },
  });

  /**
   * The section columns, unioned across every role.
   *
   * Read defensively: a role row that arrives without a `sections` map — an
   * older backend, or a role added while the page was open — must leave the
   * column out, not throw. A permission screen that crashes tells an
   * administrator nothing about who can do what.
   */
  const sectionKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const role of rolesQuery.data?.roles ?? []) {
      for (const key of Object.keys(role.sections ?? {})) keys.add(key);
    }
    return [...keys];
  }, [rolesQuery.data]);

  return (
    <>
      <PageHeader title={t('admin.roles')} description={t('admin.rolesSubtitle')} />

      <Section title={t('admin.roleMatrix')}>
        <p className="mb-3 flex items-start gap-2 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600 dark:bg-slate-800/60 dark:text-slate-300">
          <Info size={14} className="mt-0.5 shrink-0" />
          <span>{t('admin.roleMatrixHint')}</span>
        </p>

        <QueryState
          isLoading={rolesQuery.isLoading}
          error={rolesQuery.error}
          onRetry={() => void rolesQuery.refetch()}
          skeleton={<CardSkeleton rows={6} />}
        >
          <div className="table-wrap rounded-lg border border-slate-200 dark:border-slate-800">
            <table className="w-full min-w-max border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
                  <th scope="col" className="px-3 py-2 text-left font-medium text-slate-600 dark:text-slate-300">
                    {t('admin.role')}
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium text-slate-600 dark:text-slate-300">
                    {t('admin.userCount')}
                  </th>
                  {sectionKeys.map((key) => (
                    <th
                      key={key}
                      scope="col"
                      className="px-2 py-2 text-center text-[11px] font-medium text-slate-500"
                    >
                      {key.replace(/_/g, ' ')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(rolesQuery.data?.roles ?? []).map((role) => (
                  <tr
                    key={role.role}
                    onClick={() => setSelectedRole(role.role)}
                    className={`cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/50 ${
                      selectedRole === role.role ? 'bg-brand-50/60 dark:bg-slate-800' : ''
                    }`}
                  >
                    <th scope="row" className="px-3 py-2 text-left font-medium">
                      {role.role}
                      {role.is_admin && (
                        <span className="ml-1 text-[10px] uppercase text-brand-600">
                          {t('admin.adminRole')}
                        </span>
                      )}
                    </th>
                    <td className="px-3 py-2 text-right tabular-nums">
                      <span className="inline-flex items-center gap-1 text-slate-500">
                        <Users size={12} />
                        {role.user_count}
                      </span>
                    </td>
                    {sectionKeys.map((key) => {
                      const access = role.sections?.[key];
                      return (
                        <td key={key} className="px-2 py-2 text-center">
                          <span
                            className={
                              access === 'ALLOW'
                                ? 'text-emerald-600'
                                : 'text-slate-300 dark:text-slate-600'
                            }
                            title={access ?? 'unknown'}
                          >
                            {access === 'ALLOW' ? '●' : '○'}
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </QueryState>
      </Section>

      {selectedRole && (
        <Section
          title={`${t('admin.roleDefaults')}: ${selectedRole}`}
          className="mt-4"
          actions={
            isSuperAdmin ? (
              <button
                type="button"
                className="btn-primary"
                disabled={save.isPending}
                onClick={() => save.mutate()}
              >
                <Save size={14} />
                {save.isPending ? t('common.saving') : t('common.save')}
              </button>
            ) : undefined
          }
        >
          {!isSuperAdmin && (
            <p className="mb-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
              {t('admin.roleDefaultsReadOnly')}
            </p>
          )}
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

          <QueryState
            isLoading={detailQuery.isLoading}
            error={detailQuery.error}
            onRetry={() => void detailQuery.refetch()}
            skeleton={<CardSkeleton rows={6} />}
          >
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
                  </tr>
                </thead>
                <tbody>
                  {(detailQuery.data?.sections ?? []).map((row) => {
                    const current = row.locked ? 'DENY' : (draft[row.key] ?? row.access);
                    const name = `role-section-${row.key}`;
                    const disabled = row.locked || !isSuperAdmin;
                    return (
                      <tr
                        key={row.key}
                        className="border-b border-slate-100 last:border-0 dark:border-slate-800"
                      >
                        <th scope="row" className="px-3 py-2 text-left font-normal">
                          <span className="flex items-center gap-1.5 font-medium">
                            {row.label}
                            {row.locked && <Lock size={12} className="text-slate-400" />}
                          </span>
                          <span className="block text-[11px] text-slate-500">
                            {row.locked ? t('admin.sectionLocked') : row.description}
                          </span>
                        </th>
                        {(['ALLOW', 'DENY'] as const).map((access) => (
                          <td key={access} className="px-3 py-2 text-center">
                            <input
                              type="radio"
                              name={name}
                              value={access}
                              className={`h-4 w-4 ${
                                access === 'ALLOW' ? 'accent-emerald-600' : 'accent-red-600'
                              }`}
                              checked={current === access}
                              disabled={disabled}
                              onChange={() => {
                                setSaved(false);
                                setDraft((previous) => ({ ...previous, [row.key]: access }));
                              }}
                              aria-label={`${row.label} — ${access}`}
                            />
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </QueryState>
        </Section>
      )}
    </>
  );
}
