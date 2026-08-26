/**
 * A transaction table: `/data-management/transactions/:type`.
 *
 * The same shape as the master table, with three differences that come from
 * what a transaction *is*:
 *
 * * there is no Create — a transaction arrives through the validated import
 *   pipeline, and one typed in here would bypass every check that applies;
 * * Delete is a **void**, it demands a reason, and the backend refuses it
 *   outright when another record settles against this one;
 * * the period matters, so the shared date filter drives the query.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Ban, Download, Eye, Pencil, RotateCcw } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PageHeader, Section } from '../components/PageHeader';
import { RecordForm } from '../components/RecordForm';
import { RowActions, type RowAction } from '../components/RowActions';
import { CardSkeleton, QueryState } from '../components/States';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { useDebounced } from '../hooks/useDebounced';
import { ApiError, transactionRecordService, type RecordQuery } from '../services';
import { DataTable, type Column } from '../tables/DataTable';
import { formatFieldValue } from '../utils/format';
import type { Dependants, ManagedRow } from '../types/api';

const PAGE_SIZES = [10, 25, 50, 100];

export default function TransactionDataPage() {
  const { type: dataType = '' } = useParams();
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { query: filterQuery } = useFilters();

  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<{ by?: string; dir: 'asc' | 'desc' }>({ dir: 'desc' });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [includeVoided, setIncludeVoided] = useState(false);
  // `null` until the entity arrives: the initial hidden set is derived from its
  // own default-visible flags, and there is nothing to derive it from before.
  const [hiddenColumns, setHiddenColumns] = useState<Set<string> | null>(null);

  const [editing, setEditing] = useState<ManagedRow | null>(null);
  const [voiding, setVoiding] = useState<ManagedRow | null>(null);
  const [dependants, setDependants] = useState<Dependants | null>(null);

  const debouncedSearch = useDebounced(search, 350);

  const query: RecordQuery = {
    ...filterQuery,
    search: debouncedSearch || undefined,
    sort_by: sort.by,
    sort_dir: sort.dir,
    page,
    page_size: pageSize,
    include_voided: includeVoided || undefined,
  };

  const list = useQuery({
    queryKey: ['transaction-records', dataType, query],
    queryFn: () => transactionRecordService.list(dataType, query),
    placeholderData: keepPreviousData,
  });

  const entity = list.data?.entity;
  const permissions = list.data?.permissions ?? {};
  const mayEdit = Boolean(permissions.EDIT);
  const mayVoid = Boolean(permissions.DELETE);
  const mayExport = Boolean(permissions.EXPORT);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['transaction-records', dataType] });
  }

  const correct = useMutation({
    mutationFn: ({ id, values, reason }: {
      id: string; values: Record<string, unknown>; reason: string;
    }) => transactionRecordService.update(dataType, id, values, reason),
    onSuccess: () => {
      setEditing(null);
      invalidate();
    },
  });

  const voidRecord = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      transactionRecordService.void(dataType, id, reason),
    onSuccess: () => {
      setVoiding(null);
      setDependants(null);
      invalidate();
      // A void changes what every report shows, so their caches must go too —
      // leaving a dashboard on screen that still counts the reversed row would
      // contradict the table the user is looking at.
      void queryClient.invalidateQueries({ queryKey: ['page'] });
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
    },
  });

  const reinstate = useMutation({
    mutationFn: (id: string) => transactionRecordService.restore(dataType, id),
    onSuccess: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ['page'] });
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
    },
  });

  async function askToVoid(row: ManagedRow) {
    setVoiding(row);
    setDependants(null);
    try {
      const detail = await transactionRecordService.get(dataType, idOf(row));
      setDependants(detail.dependants);
    } catch {
      setDependants(null);
    }
  }

  function idOf(row: ManagedRow): string {
    return String(row._key ?? '');
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
      render: (row) => (
        <span className={row.is_void ? 'line-through' : undefined}>
          {formatFieldValue(field.kind, row[field.name])}
        </span>
      ),
    }));
  }, [entity]);

  function rowActions(row: ManagedRow): RowAction[] {
    const id = idOf(row);
    const voided = Boolean(row.is_void);
    const actions: RowAction[] = [
      {
        key: 'view',
        label: t('action.view'),
        icon: <Eye size={14} />,
        onSelect: () =>
          navigate(`/data-management/transactions/${dataType}/${id}`),
      },
    ];
    if (mayEdit && !voided) {
      actions.push({
        key: 'edit',
        label: t('action.correct'),
        icon: <Pencil size={14} />,
        onSelect: () => setEditing(row),
      });
    }
    if (mayVoid) {
      actions.push(
        voided
          ? {
              key: 'reinstate',
              label: t('action.reinstate'),
              icon: <RotateCcw size={14} />,
              onSelect: () => reinstate.mutate(id),
            }
          : {
              key: 'void',
              label: t('action.void'),
              icon: <Ban size={14} />,
              danger: true,
              onSelect: () => void askToVoid(row),
            },
      );
    }
    return actions;
  }

  return (
    <>
      <PageHeader
        title={entity?.label ?? t('dataManagement.transactions')}
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
                  onClick={() =>
                    void transactionRecordService.export(dataType, 'csv', query)
                  }
                >
                  <Download size={14} />
                  CSV
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() =>
                    void transactionRecordService.export(dataType, 'xlsx', query)
                  }
                >
                  <Download size={14} />
                  Excel
                </button>
              </>
            )}
          </div>
        }
      />

      <GlobalFilterBar />

      <Section title={t('dataManagement.records')}>
        <QueryState
          isLoading={list.isLoading}
          error={list.error}
          onRetry={() => void list.refetch()}
          skeleton={<CardSkeleton rows={8} />}
        >
          <DataTable<ManagedRow>
            // One page serves every dataset, so the arrangement belongs to the dataset.
            tableId={`transaction-data.${dataType}`}
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
            sortBy={sort.by}
            sortDir={sort.dir}
            onSortChange={(by, dir) => setSort({ by, dir })}
            rowKey={(row) => String(row._key)}
            renderRowActions={(row) => <RowActions actions={rowActions(row)} />}
            isMuted={(row) => Boolean(row.is_void)}
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
                  checked={includeVoided}
                  onChange={(event) => {
                    setIncludeVoided(event.target.checked);
                    setPage(1);
                  }}
                />
                {t('table.showVoided')}
              </label>
            }
          />
        </QueryState>
      </Section>

      {entity && editing && (
        <RecordForm
          open
          entity={entity}
          record={editing}
          busy={correct.isPending}
          error={correct.error}
          onSubmit={(values, reason) =>
            correct.mutate({ id: idOf(editing), values, reason })
          }
          onCancel={() => {
            setEditing(null);
            correct.reset();
          }}
        />
      )}

      {entity && voiding && (
        <ConfirmDialog
          open
          tone="danger"
          title={t('confirm.voidTitle', { entity: entity.label })}
          consequence={t('confirm.voidConsequence')}
          recordLabel={
            entity.label_field ? String(voiding[entity.label_field] ?? '') : null
          }
          recordCode={idOf(voiding)}
          entityLabel={entity.label}
          dependants={dependants}
          reason="required"
          confirmLabel={t('action.void')}
          busy={voidRecord.isPending}
          error={voidRecord.error ? errorMessage(voidRecord.error) : null}
          onConfirm={(reason) =>
            voidRecord.mutate({ id: idOf(voiding), reason })
          }
          onCancel={() => {
            setVoiding(null);
            voidRecord.reset();
          }}
        />
      )}
    </>
  );
}

/** A 409 from the void endpoint carries the refusal inside `detail`. */
function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return String(error);
  const detail = error.detail as { detail?: string } | string | undefined;
  if (typeof detail === 'object' && detail?.detail) return detail.detail;
  return error.message;
}
