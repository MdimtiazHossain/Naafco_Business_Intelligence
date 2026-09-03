/**
 * One record in full: `/data-management/{master|transactions}/:entity/:id`.
 *
 * Grouped rather than dumped as one long list of fields, because the questions
 * people bring to a customer record are separable: who are they, where do they
 * sit in the organisation, where are they on the map, what are they worth, and
 * what has been done to them. The panels are exactly those questions, and any
 * panel with nothing to say is not rendered rather than shown empty.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Ban, Pencil, RotateCcw, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PageHeader, Section } from '../components/PageHeader';
import { RecordForm } from '../components/RecordForm';
import { RecordHistory } from '../components/RecordHistory';
import { CardSkeleton, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import {
  ApiError,
  masterRecordService,
  transactionRecordService,
} from '../services';
import { formatFieldValue, formatDateTime } from '../utils/format';
import type { ManagedEntity, ManagedRow } from '../types/api';

export default function RecordDetailPage({ kind }: { kind: 'master' | 'transaction' }) {
  const params = useParams();
  const entityKey = (kind === 'master' ? params.entity : params.type) ?? '';
  const recordId = params.id ?? '';
  const t = useT();
  const queryClient = useQueryClient();

  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const detail = useQuery({
    queryKey: ['record-detail', kind, entityKey, recordId],
    queryFn: () =>
      kind === 'master'
        ? masterRecordService.get(entityKey, recordId)
        : transactionRecordService.get(entityKey, recordId),
  });

  const entity = detail.data?.entity;
  const record = detail.data?.record;
  const permissions = detail.data?.permissions ?? {};

  function invalidate() {
    void detail.refetch();
    void queryClient.invalidateQueries({
      queryKey: [kind === 'master' ? 'master-records' : 'transaction-records'],
    });
  }

  const save = useMutation({
    mutationFn: ({ values, reason }: { values: Record<string, unknown>; reason: string }) =>
      kind === 'master'
        ? masterRecordService.update(entityKey, recordId, values, reason)
        : transactionRecordService.update(entityKey, recordId, values, reason),
    onSuccess: () => {
      setEditing(false);
      invalidate();
    },
  });

  const removeOrVoid = useMutation({
    mutationFn: (reason: string) =>
      kind === 'master'
        ? masterRecordService.remove(entityKey, recordId, reason || undefined)
        : transactionRecordService.void(entityKey, recordId, reason),
    onSuccess: () => {
      setConfirming(false);
      invalidate();
    },
  });

  const restore = useMutation({
    mutationFn: () =>
      kind === 'master'
        ? masterRecordService.restore(entityKey, recordId)
        : transactionRecordService.restore(entityKey, recordId),
    onSuccess: invalidate,
  });

  const inactive = Boolean(record?.is_deleted || record?.is_void);
  const listRoute =
    kind === 'master'
      ? `/data-management/master/${entityKey}`
      : `/data-management/transactions/${entityKey}`;

  return (
    <>
      <PageHeader
        title={title(entity, record) || t('record.detail')}
        description={entity?.label}
        actions={
          <div className="flex flex-wrap gap-2">
            <Link to={listRoute} className="btn-secondary">
              <ArrowLeft size={14} />
              {t('record.backToTable')}
            </Link>
            {permissions.EDIT && !inactive && (
              <button type="button" className="btn-secondary" onClick={() => setEditing(true)}>
                <Pencil size={14} />
                {kind === 'master' ? t('action.edit') : t('action.correct')}
              </button>
            )}
            {permissions.DELETE &&
              (inactive ? (
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => restore.mutate()}
                >
                  <RotateCcw size={14} />
                  {kind === 'master' ? t('action.restore') : t('action.reinstate')}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => setConfirming(true)}
                >
                  {kind === 'master' ? <Trash2 size={14} /> : <Ban size={14} />}
                  {kind === 'master' ? t('action.retire') : t('action.void')}
                </button>
              ))}
          </div>
        }
      />

      <QueryState
        isLoading={detail.isLoading}
        error={detail.error}
        onRetry={() => void detail.refetch()}
        skeleton={<CardSkeleton rows={8} />}
      >
        {entity && record && (
          <>
            {inactive && (
              <p className="card mb-4 border-l-4 border-l-red-500 p-3 text-sm">
                {record.is_void
                  ? t('record.voidedNotice', {
                      by: String(record.voided_by ?? '—'),
                      reason: String(record.void_reason ?? '—'),
                    })
                  : t('record.retiredNotice', {
                      by: String(record.deleted_by ?? '—'),
                    })}
              </p>
            )}

            <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
              <div className="space-y-4">
                <Section title={t('record.information')}>
                  <FieldGrid entity={entity} record={record} group="core" />
                </Section>

                {detail.data?.hierarchy &&
                  Object.keys(detail.data.hierarchy).length > 0 && (
                    <Section title={t('record.hierarchy')}>
                      <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
                        {Object.entries(detail.data.hierarchy).map(([level, code]) => (
                          <div key={level} className="flex justify-between gap-3">
                            <dt className="text-slate-500">
                              {level.replace('_code', '').replace(/_/g, ' ')}
                            </dt>
                            <dd className="font-mono text-xs">{code}</dd>
                          </div>
                        ))}
                      </dl>
                    </Section>
                  )}

                {detail.data?.location && (
                  <Section title={t('record.geo')}>
                    {detail.data.location.latitude == null ? (
                      <p className="text-sm text-slate-500">{t('record.noCoordinate')}</p>
                    ) : (
                      <p className="text-sm tabular-nums">
                        {detail.data.location.latitude.toFixed(5)},{' '}
                        {detail.data.location.longitude?.toFixed(5)}
                        <span className="ml-2 text-xs text-slate-400">
                          {detail.data.location.source}
                        </span>
                      </p>
                    )}
                  </Section>
                )}
              </div>

              <div className="space-y-4">
                {detail.data && detail.data.dependants.total > 0 && (
                  <Section title={t('record.businessSummary')}>
                    <dl className="space-y-1 text-sm">
                      {Object.entries(detail.data.dependants.counts).map(
                        ([label, count]) => (
                          <div key={label} className="flex justify-between gap-3">
                            <dt className="text-slate-500">{label}</dt>
                            <dd className="tabular-nums">{count.toLocaleString()}</dd>
                          </div>
                        ),
                      )}
                    </dl>
                  </Section>
                )}

                <Section
                  title={t('record.history')}
                  actions={
                    detail.data?.last_change ? (
                      <span className="text-[11px] text-slate-400">
                        {formatDateTime(detail.data.last_change.at)}
                      </span>
                    ) : undefined
                  }
                >
                  <RecordHistory
                    entries={detail.data?.history ?? []}
                    total={detail.data?.history_total}
                    labelFor={(field) =>
                      entity.fields.find((f) => f.name === field)?.label ?? field
                    }
                  />
                </Section>
              </div>
            </div>
          </>
        )}
      </QueryState>

      {entity && record && editing && (
        <RecordForm
          open
          entity={entity}
          record={record}
          busy={save.isPending}
          error={save.error}
          onSubmit={(values, reason) => save.mutate({ values, reason })}
          onCancel={() => {
            setEditing(false);
            save.reset();
          }}
        />
      )}

      {entity && record && confirming && (
        <ConfirmDialog
          open
          tone={kind === 'master' ? 'warning' : 'danger'}
          title={
            kind === 'master'
              ? t('confirm.retireTitle', { entity: entity.label })
              : t('confirm.voidTitle', { entity: entity.label })
          }
          consequence={
            kind === 'master'
              ? t('confirm.retireConsequence')
              : t('confirm.voidConsequence')
          }
          recordLabel={title(entity, record)}
          recordCode={recordId}
          entityLabel={entity.label}
          dependants={detail.data?.dependants ?? null}
          reason={kind === 'master' ? 'optional' : 'required'}
          confirmLabel={kind === 'master' ? t('action.retire') : t('action.void')}
          busy={removeOrVoid.isPending}
          error={removeOrVoid.error ? (removeOrVoid.error as ApiError).message : null}
          onConfirm={(reason) => removeOrVoid.mutate(reason)}
          onCancel={() => {
            setConfirming(false);
            removeOrVoid.reset();
          }}
        />
      )}
    </>
  );
}

function title(entity?: ManagedEntity, record?: ManagedRow): string {
  if (!entity || !record) return '';
  const label = entity.label_field ? record[entity.label_field] : null;
  const code = record[entity.key_fields[0]];
  return String(label || code || '');
}

function FieldGrid({
  entity,
  record,
  group,
}: {
  entity: ManagedEntity;
  record: ManagedRow;
  group: 'core';
}) {
  // Hierarchy codes have their own panel; repeating them here would say the
  // same thing twice and bury the fields that only appear once.
  const hierarchyFields = new Set(
    Object.keys(entity.parent_column ? { [entity.parent_column]: 1 } : {}),
  );
  const fields = entity.fields.filter(
    (field) => group === 'core' && !hierarchyFields.has(field.name),
  );

  return (
    <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
      {fields.map((field) => (
        <div key={field.name} className="flex justify-between gap-3 border-b border-slate-100 py-1 last:border-0 dark:border-slate-800">
          <dt className="shrink-0 text-slate-500">{field.label}</dt>
          <dd className="truncate text-right font-medium">
            {formatFieldValue(field.kind, record[field.name]) || '—'}
          </dd>
        </div>
      ))}
    </dl>
  );
}
