/**
 * Admin panel: users, roles, data scopes, audit logs and integration status.
 *
 * Every action here calls an endpoint that re-checks the caller's role. The
 * route guard in the router is a convenience; it is not the control.
 */

import { UserPlus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { adminService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatDateTime } from '../utils/format';

type Tab = 'users' | 'audit' | 'integrations';

function UserForm({ onDone }: { onDone: () => void }) {
  const t = useT();
  const queryClient = useQueryClient();
  const { data: roleData } = useQuery({ queryKey: ['admin-roles'], queryFn: adminService.roles });
  const [form, setForm] = useState({
    username: '',
    password: '',
    role: 'VIEWER',
    display_name: '',
    email: '',
    phone_number: '',
    scope_level: '',
    scope_codes: '',
  });
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => adminService.createUser(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      onDone();
    },
    onError: (caught) => setError((caught as Error).message),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const scope =
      form.scope_level && form.scope_codes
        ? {
            [form.scope_level]: form.scope_codes
              .split(',')
              .map((code) => code.trim())
              .filter(Boolean),
          }
        : {};
    create.mutate({
      username: form.username,
      password: form.password,
      role: form.role,
      display_name: form.display_name || undefined,
      email: form.email || undefined,
      phone_number: form.phone_number || undefined,
      data_scope: scope,
    });
  }

  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2">
      <div>
        <label className="label" htmlFor="new-username">
          {t('auth.username')}
        </label>
        <input
          id="new-username"
          className="input"
          required
          value={form.username}
          onChange={(event) => setForm({ ...form, username: event.target.value })}
        />
      </div>
      <div>
        <label className="label" htmlFor="new-password">
          {t('auth.password')}
        </label>
        <input
          id="new-password"
          type="password"
          className="input"
          required
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
        />
      </div>
      <div>
        <label className="label" htmlFor="new-name">
          {t('profile.name')}
        </label>
        <input
          id="new-name"
          className="input"
          value={form.display_name}
          onChange={(event) => setForm({ ...form, display_name: event.target.value })}
        />
      </div>
      <div>
        <label className="label" htmlFor="new-role">
          {t('admin.role')}
        </label>
        <select
          id="new-role"
          className="input"
          value={form.role}
          onChange={(event) => setForm({ ...form, role: event.target.value })}
        >
          {(roleData?.roles ?? []).map((role) => (
            <option key={role.role} value={role.role}>
              {role.role}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label className="label" htmlFor="new-scope-level">
          {t('admin.dataScope')}
        </label>
        <select
          id="new-scope-level"
          className="input"
          value={form.scope_level}
          onChange={(event) => setForm({ ...form, scope_level: event.target.value })}
        >
          <option value="">{t('common.none')}</option>
          {(roleData?.scope_levels ?? []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label className="label" htmlFor="new-scope-codes">
          Codes
        </label>
        <input
          id="new-scope-codes"
          className="input"
          placeholder="REG001, REG002"
          value={form.scope_codes}
          onChange={(event) => setForm({ ...form, scope_codes: event.target.value })}
        />
      </div>

      {error && (
        <p className="sm:col-span-2 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="flex gap-2 sm:col-span-2">
        <button type="submit" className="btn-primary" disabled={create.isPending}>
          {create.isPending ? t('common.saving') : t('common.save')}
        </button>
        <button type="button" className="btn-secondary" onClick={onDone}>
          {t('common.cancel')}
        </button>
      </div>
    </form>
  );
}

function SummaryCards() {
  const t = useT();
  const { data } = useQuery({ queryKey: ['admin-summary'], queryFn: adminService.summary });
  const cards: [string, number][] = [
    [t('admin.totalUsers'), data?.total_users ?? 0],
    [t('admin.activeUsers'), data?.active_users ?? 0],
    [t('admin.inactiveUsers'), data?.inactive_users ?? 0],
    [t('admin.roles'), data?.roles ?? 0],
    [t('admin.pendingImports'), data?.pending_imports ?? 0],
    [t('admin.failedImports'), data?.failed_imports ?? 0],
    [t('admin.uploadsToday'), data?.uploads_today ?? 0],
  ];
  return (
    <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
      {cards.map(([label, value]) => (
        <div key={label} className="card p-3">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
          <p className="text-xl font-semibold tabular-nums">{value}</p>
        </div>
      ))}
    </div>
  );
}

export default function AdminPage() {
  const t = useT();
  const [tab, setTab] = useState<Tab>('users');
  const [creating, setCreating] = useState(false);

  const usersQuery = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => adminService.users({ limit: 200 }),
    enabled: tab === 'users',
  });
  const auditQuery = useQuery({
    queryKey: ['admin-audit'],
    queryFn: () => adminService.auditLogs({ limit: 100 }),
    enabled: tab === 'audit',
  });
  const whatsappQuery = useQuery({
    queryKey: ['whatsapp-status'],
    queryFn: () => adminService.whatsappStatus(),
    enabled: tab === 'integrations',
  });

  return (
    <>
      <PageHeader
        title={t('admin.title')}
        actions={
          <div className="flex gap-2">
            <Link to="/admin/users" className="btn-secondary">
              {t('admin.manageUsers')}
            </Link>
            <Link to="/admin/roles" className="btn-secondary">
              {t('admin.roles')}
            </Link>
          </div>
        }
      />

      <SummaryCards />

      <div className="card mb-4 flex flex-wrap gap-1 p-2">
        {(
          [
            ['users', t('admin.users')],
            ['audit', t('admin.auditLogs')],
            ['integrations', t('admin.whatsapp')],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded-lg px-3 py-1.5 text-xs font-medium ${
              tab === key
                ? 'bg-brand-600 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'users' && (
        <Section
          title={t('admin.users')}
          actions={
            <button
              type="button"
              className="btn-primary"
              onClick={() => setCreating((value) => !value)}
            >
              <UserPlus size={14} />
              {t('admin.newUser')}
            </button>
          }
        >
          {creating && (
            <div className="mb-4 rounded-lg border border-slate-200 p-4 dark:border-slate-700">
              <UserForm onDone={() => setCreating(false)} />
            </div>
          )}
          <QueryState
            isLoading={usersQuery.isLoading}
            error={usersQuery.error}
            onRetry={() => void usersQuery.refetch()}
            skeleton={<CardSkeleton rows={5} />}
          >
            <DataTable
              rows={(usersQuery.data?.users ?? []) as unknown as Record<string, any>[]}
              columns={[
                { key: 'username', header: t('auth.username') },
                { key: 'display_name', header: t('profile.name') },
                { key: 'role', header: t('admin.role') },
                {
                  key: 'data_scope',
                  header: t('admin.dataScope'),
                  render: (row) => {
                    const scope = row.data_scope as Record<string, string[]> | null;
                    if (!scope || Object.keys(scope).length === 0) return '—';
                    return Object.entries(scope)
                      .map(([level, codes]) => `${level}: ${codes.join(', ')}`)
                      .join('; ');
                  },
                },
                {
                  key: 'is_active',
                  header: t('admin.active'),
                  render: (row) => (row.is_active === false ? t('common.no') : t('common.yes')),
                },
                {
                  key: 'last_login_at',
                  header: 'Last login',
                  render: (row) => formatDateTime(row.last_login_at),
                },
              ]}
              rowKey={(row) => String(row.user_id)}
            />
          </QueryState>
        </Section>
      )}

      {tab === 'audit' && (
        <Section title={t('admin.auditLogs')}>
          <QueryState
            isLoading={auditQuery.isLoading}
            error={auditQuery.error}
            onRetry={() => void auditQuery.refetch()}
            skeleton={<CardSkeleton rows={6} />}
          >
            <DataTable
              rows={(auditQuery.data?.logs ?? []) as unknown as Record<string, any>[]}
              columns={[
                {
                  key: 'created_at',
                  header: t('common.period'),
                  render: (row) => formatDateTime(row.created_at),
                },
                { key: 'username', header: t('auth.username') },
                { key: 'action', header: t('admin.action') },
                { key: 'resource', header: t('admin.resource') },
                { key: 'ip_address', header: 'IP' },
                {
                  key: 'success',
                  header: t('common.status'),
                  render: (row) => (row.success ? '✓' : '✕'),
                },
              ]}
              pageSize={25}
              rowKey={(row) => String(row.audit_id)}
            />
          </QueryState>
        </Section>
      )}

      {tab === 'integrations' && (
        <Section title={t('admin.whatsapp')}>
          <QueryState
            isLoading={whatsappQuery.isLoading}
            error={whatsappQuery.error}
            onRetry={() => void whatsappQuery.refetch()}
          >
            <dl className="grid gap-3 sm:grid-cols-2">
              {Object.entries(whatsappQuery.data ?? {}).map(([key, value]) => (
                <div
                  key={key}
                  className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700"
                >
                  <dt className="text-[11px] uppercase text-slate-500">
                    {key.replace(/_/g, ' ')}
                  </dt>
                  <dd className="text-sm font-medium">
                    {typeof value === 'boolean' ? (value ? t('common.yes') : t('common.no')) : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-slate-500">
              Tokens are never returned by this endpoint.
            </p>
          </QueryState>
        </Section>
      )}
    </>
  );
}
