/** Profile: identity, data access, password change and preferences. */

import { useMutation } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { PageHeader, Section } from '../components/PageHeader';
import { useAuth } from '../contexts/AuthContext';
import { LANGUAGES, useI18n } from '../contexts/I18nContext';
import { useTheme } from '../contexts/ThemeContext';
import { authService } from '../services';
import { formatDateTime } from '../utils/format';
import type { Language, ThemePreference } from '../types/api';

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700">
      <p className="text-[11px] uppercase text-slate-500">{label}</p>
      <p className="text-sm font-medium">{value || '—'}</p>
    </div>
  );
}

export default function ProfilePage() {
  const { t, language, setLanguage } = useI18n();
  const { theme, setTheme } = useTheme();
  const { user } = useAuth();

  const [form, setForm] = useState({ current: '', next: '', confirm: '' });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: () => authService.changePassword(form.current, form.next),
    onSuccess: () => {
      setMessage(t('auth.passwordChanged'));
      setError(null);
      setForm({ current: '', next: '', confirm: '' });
    },
    onError: (caught) => {
      setError((caught as Error).message);
      setMessage(null);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (form.next !== form.confirm) {
      setError(t('auth.passwordMismatch'));
      return;
    }
    setError(null);
    change.mutate();
  }

  const scope = user?.data_scope ?? {};
  const scopeText =
    Object.keys(scope).length === 0
      ? (user?.scope_description ?? '—')
      : Object.entries(scope)
          .map(([level, codes]) => `${level.replace('_code', '')}: ${codes.join(', ')}`)
          .join(' · ');

  return (
    <>
      <PageHeader title={t('profile.title')} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title={t('profile.title')}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('profile.name')} value={user?.display_name ?? ''} />
            <Field label={t('auth.username')} value={user?.username ?? ''} />
            <Field label={t('profile.email')} value={user?.email ?? ''} />
            <Field label={t('profile.role')} value={user?.role ?? ''} />
            <Field label={t('profile.employeeId')} value={user?.employee_id ?? ''} />
            <Field
              label="Last login"
              value={user?.last_login_at ? formatDateTime(user.last_login_at) : '—'}
            />
          </div>
          <div className="mt-3">
            <Field label={t('profile.access')} value={scopeText} />
          </div>
        </Section>

        <Section title={t('profile.preferences')}>
          <div className="space-y-4">
            <div>
              <p className="label">{t('common.language')}</p>
              <div className="flex gap-2">
                {LANGUAGES.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setLanguage(option.value as Language)}
                    className={
                      language === option.value ? 'btn-primary' : 'btn-secondary'
                    }
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <p className="label">{t('common.theme')}</p>
              <div className="flex gap-2">
                {(
                  [
                    ['light', t('common.themeLight')],
                    ['dark', t('common.themeDark')],
                    ['system', t('common.themeSystem')],
                  ] as [ThemePreference, string][]
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setTheme(value)}
                    className={theme === value ? 'btn-primary' : 'btn-secondary'}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </Section>

        <Section title={t('auth.changePassword')} className="lg:col-span-2">
          <form onSubmit={submit} className="grid max-w-xl gap-3 sm:grid-cols-3">
            <div>
              <label className="label" htmlFor="current-password">
                {t('auth.currentPassword')}
              </label>
              <input
                id="current-password"
                type="password"
                className="input"
                autoComplete="current-password"
                value={form.current}
                onChange={(event) => setForm({ ...form, current: event.target.value })}
                required
              />
            </div>
            <div>
              <label className="label" htmlFor="new-password">
                {t('auth.newPassword')}
              </label>
              <input
                id="new-password"
                type="password"
                className="input"
                autoComplete="new-password"
                value={form.next}
                onChange={(event) => setForm({ ...form, next: event.target.value })}
                required
              />
            </div>
            <div>
              <label className="label" htmlFor="confirm-password">
                {t('auth.confirmPassword')}
              </label>
              <input
                id="confirm-password"
                type="password"
                className="input"
                autoComplete="new-password"
                value={form.confirm}
                onChange={(event) => setForm({ ...form, confirm: event.target.value })}
                required
              />
            </div>

            {message && (
              <p className="sm:col-span-3 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                {message}
              </p>
            )}
            {error && (
              <p className="sm:col-span-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
                {error}
              </p>
            )}

            <div className="sm:col-span-3">
              <button type="submit" className="btn-primary" disabled={change.isPending}>
                {change.isPending ? t('common.saving') : t('auth.changePassword')}
              </button>
            </div>
          </form>
        </Section>
      </div>
    </>
  );
}
