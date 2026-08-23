/**
 * Authentication state.
 *
 * The frontend holds a token and a profile for display and routing only. Every
 * request is authorised again by the backend from the user's role and data
 * scope, so hiding a menu item here is convenience, never security.
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
import { authService } from '../services';
import { setUnauthorizedHandler } from '../services/apiClient';
import type { SectionAction, SectionKey, User } from '../types/api';

interface AuthContextValue {
  user: User | null;
  /** True while the stored session is being restored on first load. */
  initialising: boolean;
  signingIn: boolean;
  error: string | null;
  signIn: (username: string, password: string, remember: boolean) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  updateUser: (user: User) => void;
  isAdmin: boolean;
  /**
   * Whether the backend will serve this section.
   *
   * Used to hide navigation and to render a clear "no access" page instead of a
   * blank report. It is a convenience only: every endpoint re-checks the same
   * permission, so a user who types the URL still gets 403.
   */
  hasSection: (section: SectionKey) => boolean;
  /**
   * Whether the backend will let this user perform an action inside a section.
   *
   * Same status as `hasSection`: it decides which controls are drawn, never
   * what the API will do. Every write endpoint resolves the same action again
   * before it acts.
   */
  can: (section: SectionKey, action: SectionAction) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [initialising, setInitialising] = useState(true);
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clearSession = useCallback(() => {
    authService.clearToken();
    setUser(null);
  }, []);

  // A 401 from any request drops the session so the router falls back to login.
  useEffect(() => {
    setUnauthorizedHandler(clearSession);
    return () => setUnauthorizedHandler(null);
  }, [clearSession]);

  // Restore a stored session on first load.
  useEffect(() => {
    let cancelled = false;
    async function restore() {
      if (!authService.hasToken()) {
        setInitialising(false);
        return;
      }
      try {
        const profile = await authService.me();
        if (!cancelled) setUser(profile);
      } catch {
        // An expired or rejected token simply means "signed out".
        if (!cancelled) clearSession();
      } finally {
        if (!cancelled) setInitialising(false);
      }
    }
    void restore();
    return () => {
      cancelled = true;
    };
  }, [clearSession]);

  const signIn = useCallback(
    async (username: string, password: string, remember: boolean) => {
      setSigningIn(true);
      setError(null);
      try {
        const response = await authService.login(username, password, remember);
        authService.storeToken(response.access_token, remember);
        // Re-read the profile so scope description and company settings arrive.
        setUser(await authService.me().catch(() => response.user));
      } catch (caught) {
        setError((caught as Error).message);
        throw caught;
      } finally {
        setSigningIn(false);
      }
    },
    [],
  );

  const signOut = useCallback(async () => {
    try {
      await authService.logout();
    } catch {
      // Signing out locally must succeed even if the server call does not.
    }
    clearSession();
  }, [clearSession]);

  const refresh = useCallback(async () => {
    if (!authService.hasToken()) return;
    setUser(await authService.me());
  }, []);

  const hasSection = useCallback(
    (section: SectionKey) => {
      // No map means an older token or a profile that predates section
      // permissions; fall back to showing the link rather than locking the user
      // out of an app the backend would happily serve.
      if (!user?.sections) return true;
      return user.sections[section] !== false;
    },
    [user],
  );

  const can = useCallback(
    (section: SectionKey, action: SectionAction) => {
      if (!hasSection(section)) return false;
      // Unlike sections, an absent action map means "assume not permitted".
      // Offering a Delete button the API would refuse is worse than hiding one
      // it would have allowed.
      return user?.actions?.[section]?.[action] === true;
    },
    [user, hasSection],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initialising,
      signingIn,
      error,
      signIn,
      signOut,
      refresh,
      updateUser: setUser,
      isAdmin: Boolean(user?.is_admin),
      hasSection,
      can,
    }),
    [user, initialising, signingIn, error, signIn, signOut, refresh, hasSection,
     can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside an AuthProvider');
  return context;
}
