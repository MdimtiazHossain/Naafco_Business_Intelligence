/**
 * Sign-in screen.
 *
 * Authentication is entirely the backend's decision — this form collects
 * credentials and shows whatever the server says. It never decides who is
 * allowed in, and never reveals whether a username exists.
 */

import { Eye, EyeOff, Loader2, LogIn } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { LANGUAGES, useI18n } from '../contexts/I18nContext';
import { useTheme } from '../contexts/ThemeContext';
import type { Language } from '../types/api';

export function LoginPage() {
  const { t, language, setLanguage } = useI18n();
  const { resolved, setTheme } = useTheme();
  const { user, signIn, signingIn, initialising } = useAuth();
  const location = useLocation();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!initialising && user) {
    const from = (location.state as { from?: string } | null)?.from ?? '/';
    return <Navigate to={from} replace />;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!username.trim() || !password) {
      setError(t('auth.required'));
      return;
    }
    try {
      await signIn(username.trim(), password, remember);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-100 to-slate-200 p-4 dark:from-slate-950 dark:to-slate-900">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-brand-600 text-lg font-bold text-white">
            BI
          </span>
          <h1 className="text-xl font-semibold">{t('auth.welcome')}</h1>
          <p className="mt-1 text-sm text-slate-500">{t('auth.subtitle')}</p>
        </div>

        <form onSubmit={handleSubmit} className="card space-y-4 p-5" noValidate>
          <div>
            <label className="label" htmlFor="username">
              {t('auth.username')}
            </label>
            <input
              id="username"
              name="username"
              className="input"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={signingIn}
            />
          </div>

          <div>
            <label className="label" htmlFor="password">
              {t('auth.password')}
            </label>
            <div className="relative">
              <input
                id="password"
                name="password"
                type={showPassword ? 'text' : 'password'}
                className="input pr-10"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={signingIn}
              />
              <button
                type="button"
                onClick={() => setShowPassword((value) => !value)}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-slate-400 hover:text-slate-600"
                aria-label={showPassword ? t('auth.hidePassword') : t('auth.showPassword')}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              disabled={signingIn}
            />
            {t('auth.remember')}
          </label>

          {error && (
            <p
              role="alert"
              className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
            >
              {error}
            </p>
          )}

          <button type="submit" className="btn-primary w-full" disabled={signingIn}>
            {signingIn ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                {t('auth.signingIn')}
              </>
            ) : (
              <>
                <LogIn size={16} />
                {t('auth.signIn')}
              </>
            )}
          </button>
        </form>

        <div className="mt-4 flex items-center justify-center gap-3 text-xs text-slate-500">
          {LANGUAGES.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setLanguage(option.value as Language)}
              className={language === option.value ? 'font-semibold text-brand-600' : ''}
            >
              {option.label}
            </button>
          ))}
          <span aria-hidden="true">·</span>
          <button
            type="button"
            onClick={() => setTheme(resolved === 'dark' ? 'light' : 'dark')}
          >
            {resolved === 'dark' ? t('common.themeLight') : t('common.themeDark')}
          </button>
        </div>
      </div>
    </div>
  );
}

export default LoginPage;
