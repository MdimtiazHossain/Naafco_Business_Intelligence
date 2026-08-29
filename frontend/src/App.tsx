/**
 * Router and providers.
 *
 * Report pages are lazily loaded so the initial bundle stays small — the login
 * screen does not need the charting library.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Lock } from 'lucide-react';
import { Suspense, lazy } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ErrorBoundary } from './components/ErrorBoundary';
import { CardSkeleton } from './components/States';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { FilterProvider } from './contexts/FilterContext';
import { I18nProvider, useT } from './contexts/I18nContext';
import { ThemeProvider } from './contexts/ThemeContext';
import { AppLayout } from './layouts/AppLayout';
import { LoginPage } from './pages/LoginPage';
import { ApiError } from './services';
import type { SectionKey } from './types/api';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const SalesPage = lazy(() => import('./pages/SalesPage'));
const StockPage = lazy(() => import('./pages/StockPage'));
const TargetPage = lazy(() => import('./pages/TargetPage'));
const TargetManagementPage = lazy(() => import('./pages/TargetManagementPage'));
const PerformancePage = lazy(() => import('./pages/PerformancePage'));
const MaterialsPage = lazy(() => import('./pages/MaterialsPage'));
const CustomersPage = lazy(() => import('./pages/CustomersPage'));
const CreditControlPage = lazy(() => import('./pages/CreditControlPage'));
const AiAssistantPage = lazy(() => import('./pages/AiAssistantPage'));
const AlertsPage = lazy(() => import('./pages/AlertsPage'));
const DataQualityPage = lazy(() => import('./pages/DataQualityPage'));
const DataUploadPage = lazy(() => import('./pages/DataUploadPage'));
const DataManagementPage = lazy(() => import('./pages/DataManagementPage'));
const MasterDataPage = lazy(() => import('./pages/MasterDataPage'));
const TransactionDataPage = lazy(() => import('./pages/TransactionDataPage'));
const RecordDetailPage = lazy(() => import('./pages/RecordDetailPage'));
const AdminPage = lazy(() => import('./pages/AdminPage'));
const AdminUsersPage = lazy(() => import('./pages/AdminUsersPage'));
const AdminRolesPage = lazy(() => import('./pages/AdminRolesPage'));
const UserPermissionsPage = lazy(() => import('./pages/UserPermissionsPage'));
const BusinessMapPage = lazy(() => import('./pages/BusinessMapPage'));
const MarkerLibraryPage = lazy(() => import('./pages/MarkerLibraryPage'));
const AgentLearningPage = lazy(() => import('./pages/AgentLearningPage'));
const MarkerDesignerPage = lazy(() => import('./pages/MarkerDesignerPage'));
const ProfilePage = lazy(() => import('./pages/ProfilePage'));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60 * 1000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Retrying an auth or permission failure only wastes the user's time.
        if (error instanceof ApiError && [401, 403, 404, 422].includes(error.status)) {
          return false;
        }
        return failureCount < 2;
      },
    },
  },
});

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, initialising } = useAuth();
  const location = useLocation();

  if (initialising) {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <CardSkeleton rows={3} />
      </div>
    );
  }
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  return <>{children}</>;
}

/**
 * Render a page only if the caller holds its section.
 *
 * A convenience guard: it turns a would-be 403 into a clear message instead of
 * an empty report. Every endpoint behind the page re-checks the same permission,
 * so removing this component would change what the user *sees*, never what they
 * can *get*.
 */
function RequireSection({
  section,
  children,
}: {
  section: SectionKey;
  children: React.ReactNode;
}) {
  const { hasSection } = useAuth();
  const t = useT();
  if (!hasSection(section)) {
    return (
      <div className="card p-8 text-center">
        <Lock size={28} className="mx-auto mb-2 text-amber-500" />
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {t('error.forbidden')}
        </p>
        <p className="mt-1 text-xs text-slate-400">{t('error.sectionDenied')}</p>
      </div>
    );
  }
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { isAdmin } = useAuth();
  const t = useT();
  // A convenience guard only — every admin API refuses non-admins server-side.
  if (!isAdmin) {
    return (
      <div className="card p-8 text-center">
        <p className="text-sm text-slate-600 dark:text-slate-300">{t('error.forbidden')}</p>
      </div>
    );
  }
  return (
    <RequireSection section="admin">
      <>{children}</>
    </RequireSection>
  );
}

/**
 * Render a page if the caller holds *any* of these sections.
 *
 * For a hub that lists several things: a user granted Transaction Data but not
 * Master Data should still reach the Data Management page and find one of its
 * two groups, rather than be refused the page that would have told them so.
 */
function RequireEitherSection({
  sections,
  children,
}: {
  sections: SectionKey[];
  children: React.ReactNode;
}) {
  const { hasSection } = useAuth();
  const t = useT();
  if (!sections.some((section) => hasSection(section))) {
    return (
      <div className="card p-8 text-center">
        <Lock size={28} className="mx-auto mb-2 text-amber-500" />
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {t('error.forbidden')}
        </p>
      </div>
    );
  }
  return <>{children}</>;
}

/** A lazily-loaded page wrapped in its section guard and suspense fallback. */
function Guarded({
  section,
  children,
}: {
  section: SectionKey;
  children: React.ReactNode;
}) {
  return (
    <RequireSection section={section}>
      <Suspense fallback={<PageFallback />}>{children}</Suspense>
    </RequireSection>
  );
}

function NotFound() {
  const t = useT();
  return (
    <div className="card p-10 text-center">
      <h1 className="text-lg font-semibold">{t('error.notFound')}</h1>
      <p className="mt-1 text-sm text-slate-500">{t('error.notFoundBody')}</p>
    </div>
  );
}

function PageFallback() {
  return (
    <div className="space-y-3">
      <CardSkeleton rows={2} />
      <CardSkeleton rows={4} />
    </div>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <FilterProvider>
              <AppLayout />
            </FilterProvider>
          </RequireAuth>
        }
      >
        <Route
          path="/"
          element={
            <Guarded section="dashboard">
              <Dashboard />
            </Guarded>
          }
        />
        <Route
          path="/ai"
          element={
            <Guarded section="ai_assistant">
              <AiAssistantPage />
            </Guarded>
          }
        />
        <Route
          path="/sales"
          element={
            <Guarded section="sales">
              <SalesPage />
            </Guarded>
          }
        />
        <Route
          path="/stock"
          element={
            <Guarded section="stock">
              <StockPage />
            </Guarded>
          }
        />
        <Route
          path="/target"
          element={
            <Guarded section="target">
              <TargetPage />
            </Guarded>
          }
        />
        <Route
          path="/target-management"
          element={
            <Guarded section="target_management">
              <TargetManagementPage />
            </Guarded>
          }
        />
        <Route
          path="/performance"
          element={
            <Guarded section="performance">
              <PerformancePage />
            </Guarded>
          }
        />
        <Route
          path="/materials"
          element={
            <Guarded section="materials">
              <MaterialsPage />
            </Guarded>
          }
        />
        <Route
          path="/customers"
          element={
            <Guarded section="customers">
              <CustomersPage />
            </Guarded>
          }
        />
        <Route
          path="/credit-control"
          element={
            <Guarded section="credit_control">
              <CreditControlPage />
            </Guarded>
          }
        />
        <Route
          path="/alerts"
          element={
            <Guarded section="alerts">
              <AlertsPage />
            </Guarded>
          }
        />
        <Route
          path="/map"
          element={
            <Guarded section="map">
              <BusinessMapPage />
            </Guarded>
          }
        />
        <Route
          path="/data-quality"
          element={
            <Guarded section="data_quality">
              <DataQualityPage />
            </Guarded>
          }
        />
        <Route
          path="/data-upload"
          element={
            <Guarded section="data_upload">
              <DataUploadPage />
            </Guarded>
          }
        />
        <Route
          path="/data-upload/history"
          element={
            <Guarded section="data_upload">
              <DataUploadPage initialTab="history" />
            </Guarded>
          }
        />

        {/* Data Management. The hub is reachable with either half of it, so a
            user granted only transactions still has a way in; each table below
            is guarded by its own section. */}
        <Route
          path="/data-management"
          element={
            <RequireEitherSection sections={['master_data', 'transaction_data']}>
              <Suspense fallback={<PageFallback />}>
                <DataManagementPage />
              </Suspense>
            </RequireEitherSection>
          }
        />
        <Route
          path="/data-management/master/:entity"
          element={
            <Guarded section="master_data">
              <MasterDataPage />
            </Guarded>
          }
        />
        <Route
          path="/data-management/master/:entity/:id"
          element={
            <Guarded section="master_data">
              <RecordDetailPage kind="master" />
            </Guarded>
          }
        />
        <Route
          path="/data-management/transactions/:type"
          element={
            <Guarded section="transaction_data">
              <TransactionDataPage />
            </Guarded>
          }
        />
        <Route
          path="/data-management/transactions/:type/:id"
          element={
            <Guarded section="transaction_data">
              <RecordDetailPage kind="transaction" />
            </Guarded>
          }
        />
        <Route
          path="/admin"
          element={
            <RequireAdmin>
              <Suspense fallback={<PageFallback />}>
                <AdminPage />
              </Suspense>
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/users"
          element={
            <RequireAdmin>
              <Suspense fallback={<PageFallback />}>
                <AdminUsersPage />
              </Suspense>
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/users/:id/permissions"
          element={
            <RequireAdmin>
              <Suspense fallback={<PageFallback />}>
                <UserPermissionsPage />
              </Suspense>
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/roles"
          element={
            <RequireAdmin>
              <Suspense fallback={<PageFallback />}>
                <AdminRolesPage />
              </Suspense>
            </RequireAdmin>
          }
        />
        {/*
          Agent Learning sits beside Map Settings and for the same reason: it is
          its own section, so a reviewer who owns what the assistant's words
          mean need not be a user administrator. The path is under /admin
          because that is where the section's route says it is.
        */}
        <Route
          path="/admin/agent-learning"
          element={
            <Suspense fallback={<PageFallback />}>
              <AgentLearningPage />
            </Suspense>
          }
        />
        {/*
          Map Settings lives under /admin but is guarded by its own section, not
          by RequireAdmin: it is a permission in its own right, so someone who
          owns how the map looks need not be a user administrator.
        */}
        <Route
          path="/admin/map-settings/markers"
          element={
            <Guarded section="map_settings">
              <MarkerLibraryPage />
            </Guarded>
          }
        />
        <Route
          path="/admin/map-settings/marker-designer"
          element={
            <Guarded section="map_settings">
              <MarkerDesignerPage />
            </Guarded>
          }
        />
        <Route
          path="/admin/map-settings/marker-designer/:id"
          element={
            <Guarded section="map_settings">
              <MarkerDesignerPage />
            </Guarded>
          }
        />
        <Route
          path="/profile"
          element={
            <Suspense fallback={<PageFallback />}>
              <ProfilePage />
            </Suspense>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ThemeProvider>
            <BrowserRouter>
              <AuthProvider>
                <AppRoutes />
              </AuthProvider>
            </BrowserRouter>
          </ThemeProvider>
        </I18nProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
