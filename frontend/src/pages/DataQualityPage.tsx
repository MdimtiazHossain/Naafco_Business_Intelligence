/**
 * Data quality: import history and per-batch rejection detail.
 *
 * Uses the Phase 2 ETL endpoints directly — the same numbers the import script
 * prints.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { PageHeader, Section } from '../components/PageHeader';
import { StatCard } from '../components/KpiCard';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { dataQualityService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatCount, formatDateTime, statusClass } from '../utils/format';

export default function DataQualityPage() {
  const t = useT();
  const [selectedBatch, setSelectedBatch] = useState<number | null>(null);

  const batchesQuery = useQuery({
    queryKey: ['etl-batches'],
    queryFn: () => dataQualityService.batches({ limit: 100 }),
  });

  const detailQuery = useQuery({
    queryKey: ['batch-quality', selectedBatch],
    queryFn: () => dataQualityService.batchQuality(selectedBatch as number, true),
    enabled: selectedBatch !== null,
  });

  const batches = batchesQuery.data?.batches ?? [];
  const latest = batches[0];
  const detail = detailQuery.data;

  return (
    <>
      <PageHeader title={t('dataQuality.title')} />

      <QueryState
        isLoading={batchesQuery.isLoading}
        error={batchesQuery.error}
        onRetry={() => void batchesQuery.refetch()}
        skeleton={<CardSkeleton rows={5} />}
      >
        <div className="space-y-4">
          {latest ? (
            <>
              <p className="text-xs text-slate-500">
                {t('dataQuality.latestImport')}: {latest.data_type} ·{' '}
                {formatDateTime(latest.started_at)}
              </p>
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard
                  label={t('dataQuality.processed')}
                  value={formatCount(latest.total_rows)}
                />
                <StatCard
                  label={t('dataQuality.successful')}
                  value={formatCount(latest.successful_rows)}
                  tone="success"
                />
                <StatCard
                  label={t('dataQuality.rejected')}
                  value={formatCount(latest.failed_rows)}
                  tone={latest.failed_rows > 0 ? 'danger' : 'default'}
                />
                <StatCard
                  label={t('dataQuality.duplicate')}
                  value={formatCount(latest.duplicate_rows)}
                />
              </div>
            </>
          ) : (
            <EmptyState message={t('common.noData')} />
          )}

          <Section title={t('dataQuality.history')}>
            <DataTable
              tableId="data-quality.batches"
              rows={batches as unknown as Record<string, any>[]}
              columns={[
                { key: 'batch_id', header: t('dataQuality.batch') },
                { key: 'source_system', header: t('dataQuality.source') },
                { key: 'data_type', header: t('dataQuality.type') },
                {
                  key: 'started_at',
                  header: t('common.period'),
                  render: (row) => formatDateTime(row.started_at),
                },
                { key: 'total_rows', header: t('common.total') },
                { key: 'successful_rows', header: t('dataQuality.successful') },
                { key: 'failed_rows', header: t('dataQuality.rejected') },
                {
                  key: 'status',
                  header: t('common.status'),
                  render: (row) => (
                    <span className={`badge ${statusClass(String(row.status))}`}>
                      {row.status}
                    </span>
                  ),
                },
              ]}
              onRowClick={(row) => setSelectedBatch(Number(row.batch_id))}
              rowKey={(row) => String(row.batch_id)}
            />
          </Section>

          {selectedBatch !== null && (
            <Section
              title={`${t('dataQuality.rejections')} — ${t('dataQuality.batch')} ${selectedBatch}`}
              actions={
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => setSelectedBatch(null)}
                >
                  {t('common.close')}
                </button>
              }
            >
              <QueryState
                isLoading={detailQuery.isLoading}
                error={detailQuery.error}
                onRetry={() => void detailQuery.refetch()}
              >
                {detail && (
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                      {Object.entries(detail.by_category).map(([category, count]) => (
                        <StatCard
                          key={category}
                          label={category.replace(/_/g, ' ')}
                          value={formatCount(count)}
                          tone={count > 0 ? 'warning' : 'default'}
                        />
                      ))}
                    </div>

                    <DataTable
                      tableId="data-quality.error-codes"
                      rows={detail.by_error_code as unknown as Record<string, any>[]}
                      columns={[
                        { key: 'error_code', header: t('admin.action') },
                        { key: 'category', header: t('alerts.category') },
                        { key: 'count', header: t('common.total') },
                        { key: 'description', header: t('common.details') },
                      ]}
                      searchable={false}
                      rowKey={(row) => String(row.error_code)}
                    />

                    {detail.rejected_records && detail.rejected_records.total > 0 && (
                      <DataTable
                        tableId="data-quality.rejected"
                        rows={detail.rejected_records.records as unknown as Record<string, any>[]}
                        columns={[
                          { key: 'source_row_number', header: t('common.page') },
                          { key: 'error_code', header: t('admin.action') },
                          { key: 'field_name', header: t('alerts.metric') },
                          { key: 'field_value', header: t('alerts.value') },
                          { key: 'error_message', header: t('common.details') },
                        ]}
                        pageSize={10}
                        rowKey={(row) => String(row.id)}
                      />
                    )}
                  </div>
                )}
              </QueryState>
            </Section>
          )}
        </div>
      </QueryState>
    </>
  );
}
