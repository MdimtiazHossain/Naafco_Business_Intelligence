/**
 * User management: `/admin/users`.
 *
 * Creating a user walks the same order the backend enforces — identity, role,
 * data scope, then section permissions — so the form matches the security model
 * rather than presenting the four as interchangeable settings.
 *
 * Every action here calls an endpoint that re-checks the caller's role and
 * section. The route guard is a convenience; it is not the control.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, Pencil, ShieldCheck, UserPlus } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { adminService } from '../services';
import { DataTable } from '../tables/DataTable';
import { useDebounced } from '../hooks/useDebounced';
import { formatDateTime } from '../utils/format';
import type { User, UserStatus } from '../types/api';

const STATUS_TONE: Record<UserStatus, string> = {
  ACTIVE: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  INACTIVE: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  LOCKED: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
};

const EMPTY_FORM = {
  username: '',
  password: '',
  role: 'VIEWER',
  display_name: '',
  employee_id: '',
  email: '',
  phone_number: '',
  department: '',
  designation: '',
  status: 'ACTIVE' as UserStatus,
  scope_level: '',
  scope_codes: '',
};

function StatusBadge({ status }: { status: UserStatus }) {
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_TONE[status]}`}
    >
      {status}
    </span>
  );
}

function UserForm({
  editing,
  onDone,
}: {
  editing: User | null;
  onDone: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const { data: roleData } = useQuery({ queryKey: ['admin-roles'], queryFn: adminService.roles });

  const existingScope = Object.entries(editing?.data_scope ?? {})[0];
  const [form, setForm] = useState({
    ...EMPTY_FORM,
    ...(editing
      ? {
          username: editing.username,
          role: editing.role,
          display_name: editing.display_name ?? '',
          employee_id: editing.employee_id ?? '',
          email: editing.email ?? '',
          phone_number: editing.mobile ?? '',
          department: editing.department ?? '',
          designation: editing.designation ?? '',
          status: editing.status ?? 'ACTIVE',
          scope_level: existingScope?.[0] ?? '',
          scope_codes: existingScope?.[1]?.join(', ') ?? '',
        }
      : {}),
  });
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      editing
        ? adminService.updateUser(editing.user_id, body)
        : adminService.createUser(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      void queryClient.invalidateQueries({ queryKey: ['admin-summary'] });
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

    const shared = {
      role: form.role,
      display_name: form.display_name || undefined,
      employee_id: form.employee_id || undefined,
      email: form.email || undefined,
      phone_number: form.phone_number || undefined,
      department: form.department || undefined,
      designation: form.designation || undefined,
      status: form.status,
      data_scope: scope,
    };

    save.mutate(
      editing
        ? { ...shared, ...(form.password ? { password: form.password } : {}) }
        : { ...shared, username: form.username, password: form.password },
    );
  }

  const field = (
    id: keyof typeof EMPTY_FORM,
    label: string,
    props: Record<string, unknown> = {},
  ) => (
    <div>
      <label className="label" htmlFor={`user-${id}`}>
        {label}
      </label>
      <input
        id={`user-${id}`}
        className="input"
        value={form[id] as string}
        onChange={(event) => setForm({ ...form, [id]: event.target.value })}
        {...props}
      />
    </div>
  );

  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {editing ? (
        <div>
          <span className="label">{t('auth.username')}</span>
          <p className="input bg-slate-50 dark:bg-slate-800">{form.username}</p>
        </div>
      ) : (
        field('username', t('auth.username'), { required: true })
      )}
      {field('password', editing ? t('admin.resetPassword') : t('auth.password'), {
        type: 'password',
        required: !editing,
        autoComplete: 'new-password',
      })}
      {field('display_name', t('profile.name'))}
      {field('employee_id', t('profile.employeeId'))}
      {field('email', t('profile.email'), { type: 'email' })}
      {field('phone_number', t('admin.mobile'))}
      {field('department', t('admin.department'))}
      {field('designation', t('admin.designation'))}

      <div>
        <label className="label" htmlFor="user-role">
          {t('admin.role')}
        </label>
        <select
          id="user-role"
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
        <label className="label" htmlFor="user-status">
          {t('common.status')}
        </label>
        <select
          id="user-status"
          className="input"
          value={form.status}
          onChange={(event) =>
            setForm({ ...form, status: event.target.value as UserStatus })
          }
        >
          {(roleData?.statuses ?? ['ACTIVE', 'INACTIVE', 'LOCKED']).map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="label" htmlFor="user-scope-level">
          {t('admin.dataScope')}
        </label>
        <select
          id="user-scope-level"
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
      {field('scope_codes', t('admin.scopeCodes'), { placeholder: 'REG001, REG002' })}

      <p className="text-xs text-slate-500 sm:col-span-2 lg:col-span-3">
        {t('admin.scopeHint')}
      </p>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-sm text-red-700 sm:col-span-2 lg:col-span-3 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="flex gap-2 sm:col-span-2 lg:col-span-3">
        <button type="submit" className="btn-primary" disabled={save.isPending}>
          {save.isPending ? t('common.saving') : t('common.save')}
        </button>
        <button type="button" className="btn-secondary" onClick={onDone}>
          {t('common.cancel')}
        </button>
      </div>
    </form>
  );
}

export default function AdminUsersPage() {
  const t = useT();
  const [search, setSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const debounced = useDebounced(search, 300);
  const pageSize = 25;

  const { data: roleData } = useQuery({ queryKey: ['admin-roles'], queryFn: adminService.roles });
  const usersQuery = useQuery({
    queryKey: ['admin-users', debounced, roleFilter, statusFilter, page],
    queryFn: () =>
      adminService.users({
        search: debounced || undefined,
        role: roleFilter || undefined,
        status: statusFilter || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      }),
  });

  return (
    <>
      <PageHeader
        title={t('admin.users')}
        description={t('admin.usersSubtitle')}
        actions={
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              setEditing(null);
              setCreating((value) => !value);
            }}
          >
            <UserPlus size={14} />
            {t('admin.newUser')}
          </button>
        }
      />

      {(creating || editing) && (
        <Section
          title={editing ? `${t('admin.editUser')}: ${editing.username}` : t('admin.newUser')}
          className="mb-4"
        >
          <UserForm
            key={editing?.user_id ?? 'new'}
            editing={editing}
            onDone={() => {
              setCreating(false);
              setEditing(null);
            }}
          />
        </Section>
      )}

      <Section title={t('admin.users')}>
        <div className="mb-3 flex flex-wrap gap-2">
          <input
            type="search"
            className="input sm:max-w-xs"
            placeholder={t('admin.searchUsers')}
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(1);
            }}
            aria-label={t('admin.searchUsers')}
          />
          <select
            className="input sm:max-w-[12rem]"
            value={roleFilter}
            onChange={(event) => {
              setRoleFilter(event.target.value);
              setPage(1);
            }}
            aria-label={t('admin.role')}
          >
            <option value="">{t('admin.allRoles')}</option>
            {(roleData?.roles ?? []).map((role) => (
              <option key={role.role} value={role.role}>
                {role.role}
              </option>
            ))}
          </select>
          <select
            className="input sm:max-w-[10rem]"
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value);
              setPage(1);
            }}
            aria-label={t('common.status')}
          >
            <option value="">{t('common.all')}</option>
            {(roleData?.statuses ?? []).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>

        <QueryState
          isLoading={usersQuery.isLoading}
          error={usersQuery.error}
          onRetry={() => void usersQuery.refetch()}
          skeleton={<CardSkeleton rows={6} />}
        >
          <DataTable
            tableId="admin.user-list"
            rows={(usersQuery.data?.users ?? []) as unknown as Record<string, any>[]}
            serverMode
            page={page}
            pageSize={pageSize}
            total={usersQuery.data?.total}
            totalPages={Math.max(1, Math.ceil((usersQuery.data?.total ?? 0) / pageSize))}
            onPageChange={setPage}
            searchable={false}
            columns={[
              { key: 'username', header: t('auth.username') },
              { key: 'display_name', header: t('profile.name') },
              { key: 'employee_id', header: t('profile.employeeId') },
              { key: 'email', header: t('profile.email') },
              { key: 'mobile', header: t('admin.mobile'), hidden: true },
              { key: 'department', header: t('admin.department'), hidden: true },
              { key: 'designation', header: t('admin.designation'), hidden: true },
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
                key: 'status',
                header: t('common.status'),
                render: (row) => <StatusBadge status={(row.status ?? 'ACTIVE') as UserStatus} />,
              },
              {
                key: 'last_login_at',
                header: t('admin.lastLogin'),
                render: (row) => formatDateTime(row.last_login_at),
              },
              {
                key: 'actions',
                header: t('common.actions'),
                sortable: false,
                render: (row) => (
                  <div className="flex gap-1">
                    <button
                      type="button"
                      className="btn-ghost tap px-2 py-1"
                      title={t('admin.editUser')}
                      onClick={() => {
                        setCreating(false);
                        setEditing(row as unknown as User);
                      }}
                    >
                      <Pencil size={14} />
                    </button>
                    <Link
                      to={`/admin/users/${row.user_id}/permissions`}
                      className="btn-ghost tap px-2 py-1"
                      title={t('admin.permissions')}
                    >
                      <ShieldCheck size={14} />
                    </Link>
                  </div>
                ),
              },
            ]}
            rowKey={(row) => String(row.user_id)}
          />
        </QueryState>

        <p className="mt-3 flex items-center gap-1 text-xs text-slate-500">
          <KeyRound size={12} />
          {t('admin.permissionHint')}
        </p>
      </Section>
    </>
  );
}
