/**
 * Admin screens must degrade, never crash.
 *
 * A permission screen that throws tells an administrator nothing about who can
 * do what — and because a render crash is caught by the app-wide error boundary,
 * it replaces the whole page with "Something went wrong". These tests pin the
 * two shapes that caused exactly that: a roles payload from a backend that
 * predates section permissions, and an empty roles list.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import * as services from '../services';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 1, username: 'root', role: 'SUPER_ADMIN', is_admin: true },
    isAdmin: true,
    hasSection: () => true,
  }),
}));

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** The payload a backend without section permissions returns. */
const LEGACY_ROLES = {
  roles: [
    { role: 'SUPER_ADMIN', unrestricted: true, is_admin: true },
    { role: 'VIEWER', unrestricted: false, is_admin: false },
  ],
  scope_levels: ['region_code'],
};

const CURRENT_ROLES = {
  roles: [
    {
      role: 'SUPER_ADMIN',
      unrestricted: true,
      is_admin: true,
      user_count: 1,
      sections: { sales: 'ALLOW', admin: 'ALLOW' },
    },
    {
      role: 'VIEWER',
      unrestricted: false,
      is_admin: false,
      user_count: 3,
      sections: { sales: 'ALLOW', admin: 'DENY' },
    },
  ],
  scope_levels: ['region_code'],
  statuses: ['ACTIVE', 'INACTIVE', 'LOCKED'],
};

describe('AdminRolesPage', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('renders the role and section matrix', async () => {
    vi.spyOn(services.adminService, 'roles').mockResolvedValue(CURRENT_ROLES as never);
    const { default: AdminRolesPage } = await import('../pages/AdminRolesPage');

    wrap(<AdminRolesPage />);

    await waitFor(() => expect(screen.getByText('SUPER_ADMIN')).toBeInTheDocument());
    expect(screen.getByText('VIEWER')).toBeInTheDocument();
    // One filled marker per ALLOW, one hollow per DENY.
    expect(screen.getAllByTitle('ALLOW')).toHaveLength(3);
    expect(screen.getAllByTitle('DENY')).toHaveLength(1);
  });

  it('survives a roles payload with no section map', async () => {
    vi.spyOn(services.adminService, 'roles').mockResolvedValue(LEGACY_ROLES as never);
    const { default: AdminRolesPage } = await import('../pages/AdminRolesPage');

    wrap(<AdminRolesPage />);

    // The roles still list; the section columns are simply absent.
    await waitFor(() => expect(screen.getByText('SUPER_ADMIN')).toBeInTheDocument());
    expect(screen.getByText('VIEWER')).toBeInTheDocument();
    expect(screen.queryAllByTitle('ALLOW')).toHaveLength(0);
  });

  it('survives an empty roles list', async () => {
    vi.spyOn(services.adminService, 'roles').mockResolvedValue({
      roles: [],
      scope_levels: [],
    } as never);
    const { default: AdminRolesPage } = await import('../pages/AdminRolesPage');

    wrap(<AdminRolesPage />);

    await waitFor(() =>
      expect(screen.getByText('Role and section matrix')).toBeInTheDocument(),
    );
  });

  it('shows the API error rather than crashing when the endpoint fails', async () => {
    vi.spyOn(services.adminService, 'roles').mockRejectedValue(
      new services.ApiError(500, 'Something went wrong. Please try again.'),
    );
    const { default: AdminRolesPage } = await import('../pages/AdminRolesPage');

    wrap(<AdminRolesPage />);

    await waitFor(() =>
      expect(
        screen.getByText('Something went wrong. Please try again.'),
      ).toBeInTheDocument(),
    );
    // An error state, not a blank page: the retry affordance is present.
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});
