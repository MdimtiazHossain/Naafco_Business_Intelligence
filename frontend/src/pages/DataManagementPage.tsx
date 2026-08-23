/**
 * The Data Management hub: `/data-management`.
 *
 * Two groups — Master Data and Transaction Data — each listing the entities the
 * caller may actually open. The list comes from the backend catalogue rather
 * than a hard-coded menu, so a dimension added to the warehouse appears here
 * without a frontend change, and an entity the user cannot see is simply not in
 * the response.
 */

import { useQuery } from '@tanstack/react-query';
import { Database, Layers, Lock, Receipt } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { dataManagementService } from '../services';
import type { EntityGroup, ManagedEntity } from '../types/api';

export default function DataManagementPage() {
  const t = useT();
  const catalogue = useQuery({
    queryKey: ['data-catalogue'],
    queryFn: dataManagementService.catalogue,
  });

  const groups = catalogue.data?.groups ?? [];
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
          <div className="grid gap-4 lg:grid-cols-2">
            {groups.map((group) => (
              <GroupCard key={group.key} group={group} />
            ))}
          </div>
        )}
      </QueryState>
    </>
  );
}

function GroupCard({ group }: { group: EntityGroup }) {
  const t = useT();
  const master = group.key === 'MASTER';

  return (
    <Section
      title={master ? t('dataManagement.master') : t('dataManagement.transactions')}
      actions={
        master ? (
          <Database size={16} className="text-slate-400" />
        ) : (
          <Receipt size={16} className="text-slate-400" />
        )
      }
    >
      <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
        {group.description}
      </p>

      {group.entities.length === 0 ? (
        <EmptyState message={t('dataManagement.noEntities')} icon={<Lock size={24} />} />
      ) : (
        <ul className="grid gap-1.5 sm:grid-cols-2">
          {group.entities.map((entity) => (
            <li key={entity.key}>
              <EntityLink entity={entity} master={master} />
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function EntityLink({ entity, master }: { entity: ManagedEntity; master: boolean }) {
  const t = useT();
  const to = master
    ? `/data-management/master/${entity.key}`
    : `/data-management/transactions/${entity.key}`;

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
      className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm transition-colors hover:border-brand-400 hover:bg-brand-50/50 dark:border-slate-700 dark:hover:bg-slate-800"
    >
      <Layers size={14} className="shrink-0 text-slate-400" />
      <span className="min-w-0 flex-1 truncate font-medium">{entity.label}</span>
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-400">
        {capabilities.length ? capabilities.join(' · ') : t('action.viewOnly')}
      </span>
    </Link>
  );
}
