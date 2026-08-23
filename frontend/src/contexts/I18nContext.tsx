/**
 * Translation.
 *
 * UI text lives in `i18n/en.json` and `i18n/bn.json` and is looked up by key, so
 * no user-visible string is hardcoded inside a component. A missing key falls
 * back to English and then to the key itself, which makes a gap obvious in
 * development without ever rendering a blank label.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import en from '../i18n/en.json';
import bn from '../i18n/bn.json';
import { authService } from '../services';
import type { Language } from '../types/api';

const STORAGE_KEY = 'bi.language';

const DICTIONARIES: Record<Language, Record<string, string>> = {
  en: en as Record<string, string>,
  bn: bn as Record<string, string>,
};

export const LANGUAGES: { value: Language; label: string }[] = [
  { value: 'en', label: 'English' },
  { value: 'bn', label: 'বাংলা' },
];

interface I18nContextValue {
  language: Language;
  setLanguage: (language: Language) => void;
  /** Translate a key, with optional `{name}` placeholders. */
  t: (key: string, values?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

function readStored(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'en' || stored === 'bn') return stored;
  } catch {
    /* storage unavailable */
  }
  return 'en';
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(readStored);

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* the in-memory preference still applies */
    }
    void authService.updatePreferences({ preferred_language: next }).catch(() => undefined);
  }, []);

  const t = useCallback(
    (key: string, values?: Record<string, string | number>) => {
      const template = DICTIONARIES[language][key] ?? DICTIONARIES.en[key] ?? key;
      if (!values) return template;
      return Object.entries(values).reduce(
        (text, [name, value]) => text.replace(new RegExp(`\\{${name}\\}`, 'g'), String(value)),
        template,
      );
    },
    [language],
  );

  const value = useMemo(() => ({ language, setLanguage, t }), [language, setLanguage, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside an I18nProvider');
  return context;
}

/** Shorthand for components that only need the translate function. */
export function useT() {
  return useI18n().t;
}
