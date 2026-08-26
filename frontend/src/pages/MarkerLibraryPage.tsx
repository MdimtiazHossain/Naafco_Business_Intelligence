/**
 * Marker design library: `/admin/map-settings/markers`.
 *
 * Every design, what it draws, whether the map is using it, and the actions that
 * change that. Destructive actions confirm; a design that is in use says so
 * before it can be deleted, and the system default refuses outright.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Copy,
  Download,
  Eye,
  Link2,
  Pencil,
  Plus,
  Power,
  PowerOff,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { MarkerPreview } from '../components/MarkerPreview';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { markerService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatDateTime } from '../utils/format';
import type { MarkerDesign, MarkerDesignStatus } from '../types/api';

const STATUS_TONE: Record<MarkerDesignStatus, string> = {
  ACTIVE: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  DRAFT: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  INACTIVE: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
};

export default function MarkerLibraryPage() {
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [entityType, setEntityType] = useState('');
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const options = useQuery({ queryKey: ['marker-options'], queryFn: markerService.options });
  const designs = useQuery({
    queryKey: ['marker-designs', entityType, status, search],
    queryFn: () =>
      markerService.list({
        entity_type: entityType || undefined,
        status: status || undefined,
        search: search || undefined,
        limit: 200,
      }),
  });
  const assignments = useQuery({
    queryKey: ['marker-assignments'],
    queryFn: markerService.assignments,
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['marker-designs'] });
    void queryClient.invalidateQueries({ queryKey: ['marker-assignments'] });
    void queryClient.invalidateQueries({ queryKey: ['marker-legend'] });
  }

  function run<T>(promise: Promise<T>, success: string) {
    setError(null);
    setMessage(null);
    promise
      .then(() => {
        setMessage(success);
        refresh();
      })
      .catch((caught) => setError((caught as Error).message));
  }

  const duplicate = useMutation({
    mutationFn: (design: MarkerDesign) => markerService.duplicate(design.design_id),
    onSuccess: (copy) => {
      refresh();
      navigate(`/admin/map-settings/marker-designer/${copy.design_id}`);
    },
    onError: (caught) => setError((caught as Error).message),
  });

  function remove(design: MarkerDesign) {
    setError(null);
    markerService
      .remove(design.design_id)
      .then(() => {
        setMessage(t('marker.deleted'));
        refresh();
      })
      .catch((caught) => {
        const problem = caught as Error;
        // The backend refuses an in-use design until it is confirmed, and says
        // what will happen; repeat that to the user rather than paraphrasing.
        if (window.confirm(`${problem.message}\n\n${t('marker.deleteConfirm')}`)) {
          run(markerService.remove(design.design_id, true), t('marker.deleted'));
        }
      });
  }

  const entityLabel = (key: string) =>
    options.data?.entity_types.find((e) => e.key === key)?.label ?? key;

  const assignedTo = (designId: number) =>
    (assignments.data?.assignments ?? [])
      .filter((a) => a.design_id === designId)
      .map((a) => (a.entity_code ? `${entityLabel(a.entity_type)} · ${a.entity_code}` : entityLabel(a.entity_type)));

  return (
    <>
      <PageHeader
        title={t('marker.libraryTitle')}
        description={t('marker.librarySubtitle')}
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => void markerService.exportDesigns()}
            >
              <Download size={14} />
              {t('marker.export')}
            </button>
            <Link to="/admin/map-settings/marker-designer" className="btn-primary">
              <Plus size={14} />
              {t('marker.newDesign')}
            </Link>
          </div>
        }
      />

      {message && (
        <p className="mb-3 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
          {message}
        </p>
      )}
      {error && (
        <p className="mb-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <Section title={t('marker.designs')}>
        <div className="mb-3 flex flex-wrap gap-2">
          <input
            type="search"
            className="input sm:max-w-xs"
            placeholder={t('marker.searchDesigns')}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t('marker.searchDesigns')}
          />
          <select
            className="input sm:max-w-[14rem]"
            value={entityType}
            onChange={(event) => setEntityType(event.target.value)}
            aria-label={t('marker.entity')}
          >
            <option value="">{t('marker.allEntities')}</option>
            {(options.data?.entity_types ?? []).map((entity) => (
              <option key={entity.key} value={entity.key}>
                {entity.label}
              </option>
            ))}
          </select>
          <select
            className="input sm:max-w-[10rem]"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label={t('common.status')}
          >
            <option value="">{t('common.all')}</option>
            {(options.data?.statuses ?? []).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>

        <QueryState
          isLoading={designs.isLoading}
          error={designs.error}
          onRetry={() => void designs.refetch()}
          isEmpty={designs.data?.designs.length === 0}
          emptyMessage={t('marker.noDesigns')}
          skeleton={<CardSkeleton rows={6} />}
        >
          <DataTable
            tableId="markers.designs"
            rows={(designs.data?.designs ?? []) as unknown as Record<string, any>[]}
            searchable={false}
            pageSize={20}
            columns={[
              {
                key: 'preview',
                header: t('marker.preview'),
                sortable: false,
                width: '5rem',
                render: (row) => (
                  <MarkerPreview svg={row.preview?.svg} size={40} title={row.name} />
                ),
              },
              {
                key: 'name',
                header: t('marker.designName'),
                render: (row) => (
                  <span className="font-medium">
                    {row.name}
                    {row.is_system_default && (
                      <span className="ml-1 rounded bg-slate-100 px-1 text-[10px] uppercase text-slate-500 dark:bg-slate-800">
                        {t('marker.systemDefault')}
                      </span>
                    )}
                  </span>
                ),
              },
              {
                key: 'entity_type',
                header: t('marker.entity'),
                render: (row) => entityLabel(row.entity_type),
              },
              { key: 'design_type', header: t('marker.type') },
              {
                key: 'status',
                header: t('common.status'),
                render: (row) => (
                  <span
                    className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      STATUS_TONE[row.status as MarkerDesignStatus]
                    }`}
                  >
                    {row.status}
                  </span>
                ),
              },
              {
                key: 'assigned',
                header: t('marker.assignedTo'),
                sortable: false,
                render: (row) => {
                  const targets = assignedTo(row.design_id);
                  return targets.length ? targets.join(', ') : '—';
                },
              },
              { key: 'version', header: t('marker.version'), align: 'right' },
              { key: 'created_by', header: t('marker.createdBy') },
              {
                key: 'updated_at',
                header: t('marker.updated'),
                render: (row) => formatDateTime(row.updated_at),
              },
              {
                key: 'actions',
                header: t('common.actions'),
                sortable: false,
                render: (row) => {
                  const design = row as unknown as MarkerDesign;
                  return (
                    <div className="flex flex-wrap gap-1">
                      <Link
                        to={`/admin/map-settings/marker-designer/${design.design_id}`}
                        className="btn-ghost px-2 py-1"
                        title={t('common.edit')}
                      >
                        <Pencil size={14} />
                      </Link>
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1"
                        title={t('marker.duplicate')}
                        onClick={() => duplicate.mutate(design)}
                      >
                        <Copy size={14} />
                      </button>
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1"
                        title={t('marker.assign')}
                        onClick={() =>
                          run(
                            markerService.assign(design.design_id),
                            t('marker.assigned', { entity: entityLabel(design.entity_type) }),
                          )
                        }
                      >
                        <Link2 size={14} />
                      </button>
                      {design.status === 'ACTIVE' ? (
                        <button
                          type="button"
                          className="btn-ghost px-2 py-1"
                          title={t('marker.deactivate')}
                          disabled={design.is_system_default}
                          onClick={() =>
                            run(
                              markerService.deactivate(design.design_id),
                              t('marker.deactivated'),
                            )
                          }
                        >
                          <PowerOff size={14} />
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn-ghost px-2 py-1"
                          title={t('marker.activate')}
                          onClick={() =>
                            run(markerService.activate(design.design_id), t('marker.activated'))
                          }
                        >
                          <Power size={14} />
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1 text-red-600"
                        title={t('common.delete')}
                        disabled={design.is_system_default}
                        onClick={() => remove(design)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  );
                },
              },
            ]}
            rowKey={(row) => String(row.design_id)}
          />
        </QueryState>
      </Section>

      <Section title={t('marker.assignments')} className="mt-4">
        <p className="mb-3 text-xs text-slate-500">{t('marker.priorityHint')}</p>
        <QueryState
          isLoading={assignments.isLoading}
          error={assignments.error}
          onRetry={() => void assignments.refetch()}
          isEmpty={assignments.data?.assignments.length === 0}
          emptyMessage={t('marker.noAssignments')}
        >
          <DataTable
            tableId="markers.assignments"
            rows={(assignments.data?.assignments ?? []) as unknown as Record<string, any>[]}
            searchable={false}
            pageSize={15}
            dense
            columns={[
              {
                key: 'entity_type',
                header: t('marker.entity'),
                render: (row) => entityLabel(row.entity_type),
              },
              {
                key: 'entity_code',
                header: t('marker.scope'),
                render: (row) =>
                  row.entity_code ? `${t('marker.scopeEntity')}: ${row.entity_code}` : t('marker.scopeType'),
              },
              { key: 'design_name', header: t('marker.designName') },
              { key: 'design_status', header: t('common.status') },
              {
                key: 'reset',
                header: t('common.actions'),
                sortable: false,
                render: (row) => (
                  <button
                    type="button"
                    className="btn-ghost px-2 py-1"
                    title={t('marker.resetDefault')}
                    onClick={() => {
                      if (window.confirm(t('marker.resetConfirm'))) {
                        run(markerService.reset(row.entity_type), t('marker.reset'));
                      }
                    }}
                  >
                    <RotateCcw size={14} />
                  </button>
                ),
              },
            ]}
            rowKey={(row) => String(row.assignment_id)}
          />
        </QueryState>
      </Section>

      <Section title={t('marker.legend')} className="mt-4">
        <p className="mb-3 text-xs text-slate-500">{t('marker.legendHint')}</p>
        <LegendPreview />
      </Section>
    </>
  );
}

/** The legend exactly as a map would draw it, from the same resolver. */
function LegendPreview() {
  const t = useT();
  const legend = useQuery({ queryKey: ['marker-legend'], queryFn: () => markerService.legend() });

  return (
    <QueryState
      isLoading={legend.isLoading}
      error={legend.error}
      onRetry={() => void legend.refetch()}
      skeleton={<CardSkeleton rows={3} />}
    >
      <div className="flex flex-wrap gap-3">
        {(legend.data?.entries ?? [])
          .filter((entry) => entry.promoted)
          .map((entry) => (
            <div
              key={entry.entity_type}
              className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700"
            >
              <MarkerPreview svg={entry.preview_svg} size={32} title={entry.label} />
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{entry.label}</p>
                <p className="truncate text-[11px] text-slate-500">
                  {entry.design_name}
                  {entry.source === 'system_default' ? ` · ${t('marker.systemDefault')}` : ''}
                </p>
              </div>
              <Eye size={12} className="ml-auto shrink-0 text-slate-300" />
            </div>
          ))}
      </div>
    </QueryState>
  );
}
