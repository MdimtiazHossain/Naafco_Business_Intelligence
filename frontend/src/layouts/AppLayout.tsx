/**
 * The application shell: header, sidebar, content and footer.
 *
 * The sidebar collapses to an overlay drawer below `lg`, which is what makes
 * the app usable on a phone. Admin links are hidden for non-admins as a
 * convenience — the backend refuses those routes regardless.
 */

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Database,
  Bot,
  Boxes,
  ChevronDown,
  ClipboardCheck,
  GraduationCap,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Map,
  MapPin,
  Menu,
  Moon,
  Package,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  Sun,
  Target,
  TrendingUp,
  UploadCloud,
  User as UserIcon,
  UserCog,
  Users,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { LANGUAGES, useI18n } from '../contexts/I18nContext';
import { useTheme } from '../contexts/ThemeContext';
import { GlobalSearch } from '../components/GlobalSearch';
import { NotificationBell } from '../components/NotificationBell';
import { UploadDock } from '../components/UploadDock';
import type { Language, SectionKey, ThemePreference } from '../types/api';

interface NavItem {
  to: string;
  labelKey: string;
  icon: ReactNode;
  /** The section the backend checks for this area. */
  section: SectionKey;
}

const MAIN_NAV: NavItem[] = [
  { to: '/', labelKey: 'nav.dashboard', icon: <LayoutDashboard size={18} />, section: 'dashboard' },
  { to: '/ai', labelKey: 'nav.ai', icon: <Bot size={18} />, section: 'ai_assistant' },
  { to: '/sales', labelKey: 'nav.sales', icon: <BarChart3 size={18} />, section: 'sales' },
  { to: '/stock', labelKey: 'nav.stock', icon: <Boxes size={18} />, section: 'stock' },
  { to: '/target', labelKey: 'nav.target', icon: <Target size={18} />, section: 'target' },
  { to: '/performance', labelKey: 'nav.performance', icon: <TrendingUp size={18} />, section: 'performance' },
  { to: '/materials', labelKey: 'nav.materials', icon: <Package size={18} />, section: 'materials' },
  { to: '/customers', labelKey: 'nav.customers', icon: <Users size={18} />, section: 'customers' },
  { to: '/map', labelKey: 'nav.map', icon: <Map size={18} />, section: 'map' },
  { to: '/alerts', labelKey: 'nav.alerts', icon: <AlertTriangle size={18} />, section: 'alerts' },
  { to: '/data-quality', labelKey: 'nav.dataQuality', icon: <ClipboardCheck size={18} />, section: 'data_quality' },
];

const DATA_NAV: NavItem[] = [
  { to: '/data-upload', labelKey: 'nav.dataUpload', icon: <UploadCloud size={18} />, section: 'data_upload' },
  // One entry for both halves: the hub lists whichever of Master Data and
  // Transaction Data the user actually holds, so two links would be two ways
  // to reach the same page and one of them would often be a dead end.
  { to: '/data-management', labelKey: 'nav.dataManagement', icon: <Database size={18} />, section: 'master_data' },
  { to: '/data-management', labelKey: 'nav.dataManagement', icon: <Database size={18} />, section: 'transaction_data' },
];

const ADMIN_NAV: NavItem[] = [
  { to: '/admin', labelKey: 'nav.admin', icon: <ShieldCheck size={18} />, section: 'admin' },
  { to: '/admin/users', labelKey: 'nav.users', icon: <UserCog size={18} />, section: 'admin' },
  { to: '/admin/roles', labelKey: 'nav.roles', icon: <KeyRound size={18} />, section: 'admin' },
];

/**
 * Map Settings is its own section, so it appears for anyone granted it —
 * including a user who is not an administrator at all.
 */
const MAP_NAV: NavItem[] = [
  {
    to: '/admin/map-settings/markers',
    labelKey: 'nav.mapSettings',
    icon: <MapPin size={18} />,
    section: 'map_settings',
  },
  {
    // Its own section too, so it appears for a reviewer who owns the
    // assistant's vocabulary without being an administrator.
    to: '/admin/agent-learning',
    labelKey: 'nav.agentLearning',
    icon: <GraduationCap size={18} />,
    section: 'agent_learning',
  },
];

/**
 * Drop repeats of the same destination.
 *
 * A page reachable through more than one section is listed once per section
 * above, so that holding either one is enough to see it. Rendering both would
 * put the same link in the sidebar twice.
 */
function dedupe(items: NavItem[]): NavItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.to)) return false;
    seen.add(item.to);
    return true;
  });
}

function SidebarLink({ item, onNavigate, collapsed = false }: {
  item: NavItem;
  onNavigate?: () => void;
  collapsed?: boolean;
}) {
  const { t } = useI18n();
  const label = t(item.labelKey);
  return (
    <NavLink
      to={item.to}
      end={item.to === '/' || item.to === '/admin'}
      onClick={onNavigate}
      // The label is the accessible name in both states, so a collapsed rail is
      // still navigable by screen reader and keyboard; `title` is what gives a
      // sighted user the hover tooltip once the text is hidden.
      title={collapsed ? label : undefined}
      aria-label={collapsed ? label : undefined}
      className={({ isActive }) =>
        `flex items-center rounded-lg py-2 text-sm font-medium transition-colors ${
          collapsed ? 'justify-center px-2' : 'gap-3 px-3'
        } ${
          isActive
            ? 'bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300'
            : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'
        }`
      }
    >
      {item.icon}
      {!collapsed && <span className="truncate">{label}</span>}
    </NavLink>
  );
}

function UserMenu() {
  const { t, language, setLanguage } = useI18n();
  const { theme, setTheme } = useTheme();
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, []);

  const themes: { value: ThemePreference; label: string; icon: ReactNode }[] = [
    { value: 'light', label: t('common.themeLight'), icon: <Sun size={14} /> },
    { value: 'dark', label: t('common.themeDark'), icon: <Moon size={14} /> },
    { value: 'system', label: t('common.themeSystem'), icon: <Activity size={14} /> },
  ];

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        className="btn-ghost gap-2 px-2"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-brand-600 text-xs font-semibold text-white">
          {(user?.display_name ?? user?.username ?? '?').slice(0, 1).toUpperCase()}
        </span>
        <span className="hidden max-w-[10rem] truncate text-sm sm:inline">
          {user?.display_name ?? user?.username}
        </span>
        <ChevronDown size={14} />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-1 w-64 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900"
        >
          <div className="border-b border-slate-200 px-3 py-2 dark:border-slate-800">
            <p className="truncate text-sm font-medium">{user?.display_name}</p>
            <p className="truncate text-xs text-slate-500">{user?.role}</p>
            {user?.scope_description && (
              <p className="mt-1 truncate text-[11px] text-slate-400">
                {user.scope_description}
              </p>
            )}
          </div>

          <div className="border-b border-slate-200 p-2 dark:border-slate-800">
            <p className="px-1 pb-1 text-[11px] font-medium uppercase text-slate-400">
              {t('common.language')}
            </p>
            <div className="flex gap-1">
              {LANGUAGES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setLanguage(option.value as Language)}
                  className={`flex-1 rounded px-2 py-1 text-xs ${
                    language === option.value
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <div className="border-b border-slate-200 p-2 dark:border-slate-800">
            <p className="px-1 pb-1 text-[11px] font-medium uppercase text-slate-400">
              {t('common.theme')}
            </p>
            <div className="flex gap-1">
              {themes.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setTheme(option.value)}
                  className={`flex flex-1 items-center justify-center gap-1 rounded px-2 py-1 text-xs ${
                    theme === option.value
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200'
                  }`}
                >
                  {option.icon}
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
            onClick={() => {
              setOpen(false);
              navigate('/profile');
            }}
          >
            <UserIcon size={14} />
            {t('nav.profile')}
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40"
            onClick={() => void signOut()}
          >
            <LogOut size={14} />
            {t('auth.signOut')}
          </button>
        </div>
      )}
    </div>
  );
}

const SIDEBAR_STORAGE_KEY = 'bi.sidebar_collapsed';

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true';
  } catch {
    /* storage unavailable; the sidebar simply starts expanded */
    return false;
  }
}

export function AppLayout() {
  const { t } = useI18n();
  const { user, isAdmin, hasSection } = useAuth();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  /**
   * Desktop rail state. Local to the shell and stored the same way the theme
   * is — a preference, not application state, so collapsing it re-renders the
   * layout and nothing else: filters, pagination, the date range and any
   * running upload all live elsewhere and never see this change.
   */
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const location = useLocation();

  // Close the mobile drawer whenever the route changes.
  useEffect(() => setSidebarOpen(false), [location.pathname]);

  const toggleCollapsed = () => {
    setCollapsed((previous) => {
      const next = !previous;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
      } catch {
        /* the in-memory preference still applies for this session */
      }
      return next;
    });
  };

  // The header's height is published as `--app-header-height` in `index.css`,
  // where it is a function of the breakpoint and needs no measuring. Anything
  // sticking below the header lines up against that variable.

  // Only sections the backend will actually serve are offered. Hiding a link is
  // presentation — the endpoint refuses a denied section regardless.
  const navItems = dedupe(
    [
      ...MAIN_NAV,
      ...DATA_NAV,
      ...MAP_NAV,
      ...(isAdmin ? ADMIN_NAV : []),
    ].filter((item) => hasSection(item.section)),
  );

  return (
    <div className="min-h-screen">
      {/* Height mirrored by `--app-header-height` in index.css — keep in step. */}
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95">
        <div className="flex h-14 items-center gap-3 px-3 sm:px-4">
          <button
            type="button"
            className="btn-ghost px-2 lg:hidden"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <Menu size={20} />
          </button>

          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
              BI
            </span>
            <div className="hidden sm:block">
              <p className="text-sm font-semibold leading-tight">
                {user?.company_name ?? t('app.name')}
              </p>
              <p className="text-[11px] leading-tight text-slate-500">{t('app.name')}</p>
            </div>
          </div>

          <div className="ml-auto hidden flex-1 justify-center px-4 md:flex">
            <GlobalSearch />
          </div>

          <div className="ml-auto flex items-center gap-1 md:ml-0">
            {/* Sits in the layout, not on a page, so a running import is visible
                from wherever the user navigates to. */}
            <UploadDock />
            <NotificationBell />
            <UserMenu />
          </div>
        </div>

        <div className="border-t border-slate-200 px-3 py-2 md:hidden dark:border-slate-800">
          <GlobalSearch />
        </div>
      </header>

      <div className="flex">
        {/*
          Desktop sidebar. Collapsing narrows this rail to its icons; the main
          content beside it is `flex-1`, so it reclaims the freed width on its
          own without either element knowing the other's size.
        */}
        <aside
          className={`sticky top-14 hidden h-[calc(100vh-3.5rem)] shrink-0 overflow-y-auto overflow-x-hidden border-r border-slate-200 bg-white p-3 transition-[width] duration-200 lg:block dark:border-slate-800 dark:bg-slate-900 ${
            collapsed ? 'w-16' : 'w-60'
          }`}
        >
          <div className={`mb-2 flex ${collapsed ? 'justify-center' : 'justify-end'}`}>
            <button
              type="button"
              className="btn-ghost px-2 py-1 text-slate-500"
              onClick={toggleCollapsed}
              title={collapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
              aria-label={collapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
              aria-expanded={!collapsed}
            >
              {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            </button>
          </div>
          <nav className="flex flex-col gap-0.5">
            {navItems.map((item) => (
              <SidebarLink key={item.to} item={item} collapsed={collapsed} />
            ))}
          </nav>
        </aside>

        {/* Mobile drawer */}
        {sidebarOpen && (
          <div className="fixed inset-0 z-50 lg:hidden">
            <div
              className="absolute inset-0 bg-slate-900/50"
              onClick={() => setSidebarOpen(false)}
              aria-hidden="true"
            />
            <aside className="animate-fade-in absolute left-0 top-0 h-full w-64 overflow-y-auto bg-white p-3 shadow-xl dark:bg-slate-900">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-sm font-semibold">{t('app.name')}</span>
                <button
                  type="button"
                  className="btn-ghost px-2"
                  onClick={() => setSidebarOpen(false)}
                  aria-label={t('common.close')}
                >
                  <X size={18} />
                </button>
              </div>
              <nav className="flex flex-col gap-0.5">
                {navItems.map((item) => (
                  <SidebarLink
                    key={item.to}
                    item={item}
                    onNavigate={() => setSidebarOpen(false)}
                  />
                ))}
              </nav>
            </aside>
          </div>
        )}

        <main className="min-w-0 flex-1 p-3 sm:p-4 lg:p-6">
          <Outlet />
          <footer className="mt-8 border-t border-slate-200 pt-4 text-center text-xs text-slate-400 dark:border-slate-800">
            {user?.company_name ?? t('app.name')} · {t('app.tagline')}
          </footer>
        </main>
      </div>
    </div>
  );
}
