/**
 * A master-data table: `/data-management/master/:entity`.
 *
 * One page serves every master entity, because the entity describes itself —
 * its fields, their labels, which are required, which identifies the record and
 * which is its status. Adding a dimension to the warehouse adds a table here
 * with no code.
 *
 * Everything that narrows the table is a server round trip: search, filters,
 * sort and paging. The browser never holds more than one page, which is the
 * only design that survives a customer master with tens of thousands of rows.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Download,
  Eye,
  MapPin,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PageHeader, Section } from '../components/PageHeader';
import { RecordForm } from '../components/RecordForm';
import { RowActions, type RowAction } from '../components/RowActions';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { useDebounced } from '../hooks/useDebounced';
import { ApiError, masterRecordService, type RecordQuery } from '../services';
import { DataTable, type Column } from '../tables/DataTable';
import { formatFieldValue } from '../utils/format';
import type { Dependants, ManagedEntity, ManagedRow } from '../types/api';

const PAGE_SIZES = [10, 25, 50, 100];

export default function MasterDataPage() {
  const { entity: entityKey = '' } = useParams();
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [sort, setSort] = useState<{ by?: string; dir: 'asc' | 'desc' }>({ dir: 'asc' });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // `null` until the entity arrives, because the initial hidden set *is* the
  // entity's own default-visible flags and there is nothing to derive it from
  // before then. Holding an empty set instead would show every column of a
  // forty-column dimension for one render and then collapse it.
  const [hiddenColumns, setHiddenColumns] = useState<Set<string> | null>(null);

  const [editing, setEditing] = useState<ManagedRow | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<ManagedRow | null>(null);
  const [dependants, setDependants] = useState<Dependants | null>(null);

  const debouncedSearch = useDebounced(search, 350);

  const query: RecordQuery = {
    search: debouncedSearch || undefined,
    sort_by: sort.by,
    sort_dir: sort.dir,
    page,
    page_size: pageSize,
    include_deleted: includeDeleted || undefined,
    filters,
  };

  const list = useQuery({
    queryKey: ['master-records', entityKey, query],
    queryFn: () => masterRecordService.list(entityKey, query),
    // Keeping the previous page on screen while the next loads is what makes
    // paging feel like paging rather than a reload.
    placeholderData: keepPreviousData,
  });

  const entity = list.data?.entity;
  const permissions = list.data?.permissions ?? {};
  const mayEdit = Boolean(permissions.EDIT);
  const mayDelete = Boolean(permissions.DELETE);
  const mayCreate = Boolean(permissions.CREATE);
  const mayExport = Boolean(permissions.EXPORT);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['master-records', entityKey] });
  }

  const save = useMutation({
    mutationFn: ({ values, reason }: { values: Record<string, unknown>; reason: string }) =>
      editing
        ? masterRecordService.update(entityKey, keyOf(entity, editing), values, reason)
        : masterRecordService.create(entityKey, values),
    onSuccess: () => {
      setEditing(null);
      setCreating(false);
      // Refetch rather than patch the row in place: an edit can change what the
      // current filter or sort selects, and a row left sitting where it no
      // longer belongs is worse than a brief spinner.
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: ({ code, reason }: { code: string; reason: string }) =>
      masterRecordService.remove(entityKey, code, reason || undefined),
    onSuccess: () => {
      setRemoving(null);
      setDependants(null);
      invalidate();
    },
  });

  const restore = useMutation({
    mutationFn: (code: string) => masterRecordService.restore(entityKey, code),
    onSuccess: invalidate,
  });

  const setStatus = useMutation({
    mutationFn: ({ code, status }: { code: string; status: string }) =>
      masterRecordService.setStatus(entityKey, code, status),
    onSuccess: invalidate,
  });

  const bulk = useMutation({
    mutationFn: ({ action, codes }: { action: string; codes: string[] }) =>
      masterRecordService.bulk(entityKey, action, codes),
    onSuccess: () => {
      setSelected(new Set());
      invalidate();
    },
  });

  async function askToRemove(row: ManagedRow) {
    setRemoving(row);
    setDependants(null);
    try {
      // Loaded when the dialog opens, never per row of the table: it is a
      // count over the fact tables and has no business running until someone
      // actually reaches for Delete.
      setDependants(await masterRecordService.dependants(entityKey, keyOf(entity, row)));
    } catch {
      // A failed count must not block the confirmation; the dialog simply
      // shows one less piece of information.
      setDependants(null);
    }
  }

  const visibleDefaults = useMemo(
    () =>
      new Set(
        (entity?.fields ?? [])
          .filter((field) => !field.default_visible)
          .map((field) => field.name),
      ),
    [entity],
  );

  const columns = useMemo<Column<ManagedRow>[]>(() => {
    if (!entity) return [];
    return entity.fields.map((field) => ({
      key: field.name,
      header: field.label,
      hidden: !field.default_visible,
      render:
        field.name === entity.status_field
          ? (row) => (
              <StatusCell
                row={row}
                entity={entity}
                editable={mayEdit && !row.is_deleted}
                onChange={(status) =>
                  setStatus.mutate({ code: keyOf(entity, row), status })
                }
              />
            )
          : field.is_key
            ? (row) => (
                <span className="flex items-center gap-1.5 font-mono text-xs">
                  {String(row[field.name] ?? '')}
                  {Boolean(row.is_deleted) && (
                    <span className="badge bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300">
                      {t('record.retired')}
                    </span>
                  )}
                </span>
              )
            : (row) => <>{formatFieldValue(field.kind, row[field.name])}</>,
    }));
  }, [entity, mayEdit, setStatus, t]);

  function rowActions(row: ManagedRow): RowAction[] {
    if (!entity) return [];
    const code = keyOf(entity, row);
    const retired = Boolean(row.is_deleted);
    const actions: RowAction[] = [
      {
        key: 'view',
        label: t('action.view'),
        icon: <Eye size={14} />,
        onSelect: () =>
          navigate(`/data-management/master/${entity.key}/${encodeURIComponent(code)}`),
      },
    ];
    if (mayEdit && !retired) {
      actions.push({
        key: 'edit',
        label: t('action.edit'),
        icon: <Pencil size={14} />,
        onSelect: () => setEditing(row),
      });
    }
    if (entity.supports_geo) {
      actions.push({
        key: 'map',
        label: t('action.viewOnMap'),
        icon: <MapPin size={14} />,
        onSelect: () => navigate(mapLink(entity, [code])),
      });
    }
    if (mayDelete) {
      actions.push(
        retired
          ? {
              key: 'restore',
              label: t('action.restore'),
              icon: <RotateCcw size={14} />,
              onSelect: () => restore.mutate(code),
            }
          : {
              key: 'delete',
              label: t('action.retire'),
              icon: <Trash2 size={14} />,
              danger: true,
              onSelect: () => void askToRemove(row),
            },
      );
    }
    return actions;
  }

  const removingCode = entity && removing ? keyOf(entity, removing) : '';

  return (
    <>
      <PageHeader
        title={entity?.label ?? t('dataManagement.master')}
        description={
          list.data
            ? `${entity?.description ?? ''} · ${t('table.scope', {
                scope: list.data.scope_description,
              })}`
            : undefined
        }
        actions={
          <div className="flex flex-wrap gap-2">
            <Link to="/data-management" className="btn-secondary">
              {t('dataManagement.allTables')}
            </Link>
            {mayExport && (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => void masterRecordService.export(entityKey, 'csv', query)}
                >
                  <Download size={14} />
                  CSV
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => void masterRecordService.export(entityKey, 'xlsx', query)}
                >
                  <Download size={14} />
                  Excel
                </button>
              </>
            )}
            {mayCreate && (
              <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
                <Plus size={14} />
                {t('action.create')}
              </button>
            )}
          </div>
        }
      />

      {entity && entity.filter_fields.length + (entity.status_field ? 1 : 0) > 0 && (
        <EntityFilters
          entity={entity}
          values={filters}
          onChange={(next) => {
            setFilters(next);
            setPage(1);
          }}
        />
      )}

      <Section title={t('dataManagement.records')}>
        <QueryState
          isLoading={list.isLoading}
          error={list.error}
          onRetry={() => void list.refetch()}
          skeleton={<CardSkeleton rows={8} />}
        >
          <DataTable<ManagedRow>
            // One page serves every entity, so the arrangement belongs to the entity.
            tableId={`master-data.${entityKey}`}
            serverMode
            rows={list.data?.rows ?? []}
            columns={columns}
            page={page}
            pageSize={pageSize}
            total={list.data?.total}
            totalPages={list.data?.total_pages}
            onPageChange={setPage}
            pageSizeOptions={PAGE_SIZES}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
            search={search}
            onSearchChange={(value) => {
              setSearch(value);
              setPage(1);
            }}
            // This page has no GlobalFilterBar; the table's own search and
            // column controls are its toolbar, so they are what stays in view.
            stickyToolbar
            sortBy={sort.by}
            sortDir={sort.dir}
            onSortChange={(by, dir) => setSort({ by, dir })}
            rowKey={(row) => String(row._key)}
            selectable={mayEdit || mayDelete}
            selected={selected}
            onSelectionChange={setSelected}
            renderRowActions={(row) => <RowActions actions={rowActions(row)} />}
            isMuted={(row) => Boolean(row.is_deleted)}
            hiddenColumns={hiddenColumns ?? visibleDefaults}
            onHiddenColumnsChange={setHiddenColumns}
            rangeLabel={
              list.data
                ? t('table.range', {
                    from: String(list.data.range_from),
                    to: String(list.data.range_to),
                    total: list.data.total.toLocaleString(),
                  })
                : undefined
            }
            emptyMessage={t('table.noRecords')}
            toolbar={
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-500">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-brand-600"
                  checked={includeDeleted}
                  onChange={(event) => {
                    setIncludeDeleted(event.target.checked);
                    setPage(1);
                  }}
                />
                {t('table.showRetired')}
              </label>
            }
            bulkBar={
              selected.size > 0 && entity ? (
                <BulkBar
                  entity={entity}
                  count={selected.size}
                  busy={bulk.isPending}
                  mayEdit={mayEdit}
                  mayDelete={mayDelete}
                  onAction={(action) =>
                    bulk.mutate({ action, codes: [...selected] })
                  }
                  onShowOnMap={() => navigate(mapLink(entity, [...selected]))}
                  onClear={() => setSelected(new Set())}
                />
              ) : null
            }
          />
        </QueryState>
      </Section>

      {entity && (creating || editing) && (
        <RecordForm
          open
          entity={entity}
          record={editing}
          busy={save.isPending}
          error={save.error}
          onSubmit={(values, reason) => save.mutate({ values, reason })}
          onCancel={() => {
            setEditing(null);
            setCreating(false);
            save.reset();
          }}
        />
      )}

      {entity && removing && (
        <ConfirmDialog
          open
          tone="warning"
          title={t('confirm.retireTitle', { entity: entity.label })}
          consequence={t('confirm.retireConsequence')}
          recordLabel={
            entity.label_field ? String(removing[entity.label_field] ?? '') : null
          }
          recordCode={removingCode}
          entityLabel={entity.label}
          dependants={dependants}
          reason="optional"
          confirmLabel={t('action.retire')}
          busy={remove.isPending}
          error={remove.error ? (remove.error as ApiError).message : null}
          onConfirm={(reason) => remove.mutate({ code: removingCode, reason })}
          onCancel={() => {
            setRemoving(null);
            remove.reset();
          }}
        />
      )}
    </>
  );
}

function keyOf(entity: ManagedEntity | undefined, row: ManagedRow): string {
  if (!entity) return String(row._key ?? '');
  return String(row[entity.key_fields[0]] ?? row._key ?? '');
}

/** Deep link into the map, focused on these records. */
export function mapLink(entity: ManagedEntity, codes: string[]): string {
  const type = entity.scope_level
    ? entity.scope_level.replace('_code', '')
    : entity.key.replace('dim_', '');
  return `/map?focus=${type}:${codes.map(encodeURIComponent).join(',')}`;
}

function StatusCell({
  row,
  entity,
  editable,
  onChange,
}: {
  row: ManagedRow;
  entity: ManagedEntity;
  editable: boolean;
  onChange: (status: string) => void;
}) {
  const value = String(row[entity.status_field ?? ''] ?? '');
  const active = value.toLowerCase() === 'active';

  // Inline editing, but only for this one field: a status is a two-value
  // choice with no other consequence, while anything with dependencies or
  // validation belongs in the full form where its errors can be shown.
  if (!editable) {
    return (
      <span
        className={`badge ${
          active
            ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
            : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
        }`}
      >
        {value || '—'}
      </span>
    );
  }

  return (
    <select
      className="rounded border border-slate-300 bg-transparent px-1.5 py-0.5 text-xs dark:border-slate-700"
      value={active ? 'Active' : 'Inactive'}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => onChange(event.target.value)}
      aria-label={entity.status_field ?? 'status'}
    >
      {entity.status_values.map((option) => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  );
}

function EntityFilters({
  entity,
  values,
  onChange,
}: {
  entity: ManagedEntity;
  values: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  const t = useT();
  // The fields worth filtering on: the parent, so a table can be narrowed to one
  // branch of the hierarchy, plus any constrained-value field.
  const fields = entity.fields.filter(
    (field) =>
      field.name === entity.parent_column || field.choices.length > 0,
  );
  if (fields.length === 0) return null;

  const active = Object.entries(values).filter(([, value]) => value);

  return (
    <div className="card mb-3 flex flex-wrap items-center gap-2 p-3">
      {fields.map((field) => (
        <label key={field.name} className="flex items-center gap-1.5 text-xs">
          <span className="text-slate-500">{field.label}</span>
          {field.choices.length > 0 ? (
            <select
              className="input max-w-[9rem] py-1 text-xs"
              value={values[field.name] ?? ''}
              onChange={(event) =>
                onChange({ ...values, [field.name]: event.target.value })
              }
            >
              <option value="">{t('filters.all')}</option>
              {field.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="input max-w-[9rem] py-1 text-xs"
              value={values[field.name] ?? ''}
              placeholder={field.label}
              onChange={(event) =>
                onChange({ ...values, [field.name]: event.target.value })
              }
            />
          )}
        </label>
      ))}
      {active.length > 0 && (
        <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => onChange({})}>
          <X size={12} />
          {t('filters.clear')}
        </button>
      )}
    </div>
  );
}

function BulkBar({
  entity,
  count,
  busy,
  mayEdit,
  mayDelete,
  onAction,
  onShowOnMap,
  onClear,
}: {
  entity: ManagedEntity;
  count: number;
  busy: boolean;
  mayEdit: boolean;
  mayDelete: boolean;
  onAction: (action: string) => void;
  onShowOnMap: () => void;
  onClear: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800">
      <span className="font-medium">{t('bulk.selected', { count: String(count) })}</span>
      {mayEdit && entity.status_field && (
        <>
          <button
            type="button"
            className="btn-secondary py-1 text-xs"
            disabled={busy}
            onClick={() => onAction('ACTIVATE')}
          >
            {t('bulk.activate')}
          </button>
          <button
            type="button"
            className="btn-secondary py-1 text-xs"
            disabled={busy}
            onClick={() => onAction('DEACTIVATE')}
          >
            {t('bulk.deactivate')}
          </button>
        </>
      )}
      {entity.supports_geo && (
        <button type="button" className="btn-secondary py-1 text-xs" onClick={onShowOnMap}>
          <MapPin size={12} />
          {t('bulk.showOnMap')}
        </button>
      )}
      {mayDelete && (
        <button
          type="button"
          className="btn-secondary py-1 text-xs text-red-600 dark:text-red-400"
          disabled={busy}
          // Bulk retirement is a confirmed action on each record server-side and
          // reports per-record outcomes, so a partial result is visible rather
          // than swallowed.
          onClick={() => onAction('DELETE')}
        >
          {t('bulk.retire')}
        </button>
      )}
      <button type="button" className="btn-ghost ml-auto px-2 py-1 text-xs" onClick={onClear}>
        {t('bulk.clear')}
      </button>
    </div>
  );
}
