/**
 * The Data Management hub: `/data-management`.
 *
 * The entities the caller may actually open, in collapsible groups. The list
 * comes from the backend catalogue rather than a hard-coded menu, so a
 * dimension added to the warehouse appears here without a frontend change, and
 * an entity the user cannot see is simply not in the response.
 *
 * **Grouping and routing are two different questions.** A record's group is
 * where a reader expects to find it — Sales, Market, People, Material,
 * Transactions — and comes from `entity.group`. Its route is decided by
 * `entity.category`, which is MASTER or TRANSACTIONAL and is what the backend
 * branches on. They are deliberately not the same: Market lists master
 * dimensions, Transactions lists transactional ones, and reading the route off
 * the heading would send half of them to the wrong URL.
 */

import { useQuery } from '@tanstack/react-query';
import { ChevronRight, Database, Layers, Lock } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { dataManagementService } from '../services';
import type { ManagedEntity } from '../types/api';

/**
 * The order the groups are listed in, outermost concern first.
 *
 * Order only — never membership. A group the server sends that is missing here
 * is appended rather than dropped, so a new heading can never make a dataset
 * unreachable; the backend's `GROUP_BY_KEY` is what decides what exists.
 */
const GROUP_ORDER = ['SALES', 'MARKET', 'PEOPLE', 'MATERIAL', 'TRANSACTIONS'];

export default function DataManagementPage() {
  const t = useT();
  const location = useLocation();
  const catalogue = useQuery({
    queryKey: ['data-catalogue'],
    queryFn: dataManagementService.catalogue,
  });

  /**
   * Every entity the caller may open, regrouped by display group.
   *
   * The API still answers in MASTER / TRANSACTION groups, because that is what
   * it processes by; this flattens them and re-files each entity under the
   * heading a reader looks for it under, keeping the category on the entity so
   * the link still knows where to point.
   */
  const groups = useMemo(() => {
    const entities = (catalogue.data?.groups ?? []).flatMap((g) => g.entities);
    const byGroup = new Map<string, ManagedEntity[]>();
    for (const entity of entities) {
      const key = entity.group ?? 'SALES';
      byGroup.set(key, [...(byGroup.get(key) ?? []), entity]);
    }
    const known = GROUP_ORDER.filter((key) => byGroup.has(key));
    const extra = [...byGroup.keys()].filter((key) => !GROUP_ORDER.includes(key));
    return [...known, ...extra].map((key) => ({
      key,
      entities: byGroup.get(key) ?? [],
    }));
  }, [catalogue.data]);

  // Collapsed by default, and any number may be open at once: these are
  // reference lists somebody scans, not steps in a sequence.
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const toggle = (key: string) =>
    setOpen((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const nothing = groups.every((group) => group.entities.length === 0);

  return (
    <>
      <PageHeader
        title={t('dataManagement.title')}
        description={t('dataManagement.description')}
      />

      <QueryState
        isLoading={catalogue.isLoading}
        error={catalogue.error}
        onRetry={() => void catalogue.refetch()}
        skeleton={<CardSkeleton rows={6} />}
      >
        {nothing ? (
          <EmptyState message={t('dataManagement.noAccess')} icon={<Lock size={30} />} />
        ) : (
          <div className="space-y-2">
            {groups.map((group) => (
              <GroupAccordion
                key={group.key}
                groupKey={group.key}
                entities={group.entities}
                open={open.has(group.key)}
                onToggle={() => toggle(group.key)}
                pathname={location.pathname}
              />
            ))}
          </div>
        )}
      </QueryState>
    </>
  );
}

function GroupAccordion({
  groupKey, entities, open, onToggle, pathname,
}: {
  groupKey: string;
  entities: ManagedEntity[];
  open: boolean;
  onToggle: () => void;
  pathname: string;
}) {
  const t = useT();
  const label = t(`dataManagement.group.${groupKey}`);
  const panelId = `data-group-${groupKey}`;

  return (
    <div className="card overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/60"
      >
        <ChevronRight
          size={15}
          aria-hidden="true"
          className={`shrink-0 text-slate-400 transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <Database size={15} className="shrink-0 text-slate-400" />
        <span className="flex-1 text-sm font-semibold">{label}</span>
        <span className="shrink-0 text-xs tabular-nums text-slate-400">
          {entities.length}
        </span>
      </button>

      {open && (
        <ul id={panelId} className="grid gap-1.5 border-t border-slate-200 p-3 sm:grid-cols-2 dark:border-slate-800">
          {entities.map((entity) => (
            <li key={`${entity.category}:${entity.key}`}>
              <EntityLink entity={entity} pathname={pathname} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EntityLink({ entity, pathname }: { entity: ManagedEntity; pathname: string }) {
  const t = useT();
  // The route follows the *category*, not the heading it is listed under.
  const master = entity.category === 'MASTER';
  const to = master
    ? `/data-management/master/${entity.key}`
    : `/data-management/transactions/${entity.key}`;
  const active = pathname === to;

  // What the user may do, so the hub says whether a table is read-only before
  // they open it and find the buttons missing.
  const capabilities = [
    entity.permissions?.EDIT && t('action.edit'),
    entity.permissions?.DELETE && (master ? t('action.retire') : t('action.void')),
    entity.permissions?.EXPORT && t('action.export'),
  ].filter(Boolean) as string[];

  return (
    <Link
      to={to}
      aria-current={active ? 'page' : undefined}
      className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
        active
          ? 'border-brand-500 bg-brand-50 font-medium text-brand-700 dark:bg-slate-800 dark:text-brand-300'
          : 'border-slate-200 hover:border-brand-400 hover:bg-brand-50/50 dark:border-slate-700 dark:hover:bg-slate-800'
      }`}
    >
      <Layers size={14} className="shrink-0 text-slate-400" />
      <span className="min-w-0 flex-1 truncate font-medium">{entity.label}</span>
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-400">
        {capabilities.length ? capabilities.join(' · ') : t('action.viewOnly')}
      </span>
    </Link>
  );
}
