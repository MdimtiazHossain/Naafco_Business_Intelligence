/**
 * Customer analytics.
 *
 * Only metrics the warehouse actually holds are shown — sales, quantity,
 * invoice count and last transaction. Nothing is invented to fill the page out,
 * and the backend's note about the missing customer master is displayed rather
 * than hidden.
 *
 * The receivables columns went with the Outstanding module: a customer's
 * balance, its overdue age and what they had paid are no longer knowable, so
 * the page shows fewer columns rather than emptier ones.
 */

import { useQuery } from '@tanstack/react-query';
import { CategoryBarChart } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { customerService } from '../services';
import { DataTable } from '../tables/DataTable';

export default function CustomersPage() {
  const t = useT();
  const { query } = useFilters();

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['customers-page', query],
    queryFn: () => customerService.page({ ...query, limit: 100 }),
  });

  const rows = data?.customers.rows ?? [];

  /**
   * The customer reads as its name; the code follows as secondary detail.
   *
   * `label` is the name and `code` is the code — the shape every grouped report
   * returns, and since revision 0024 the customer's label is its name like every
   * other dimension's. The code is still what the row is keyed on, exported and
   * filtered by; it is shown beside the name rather than instead of it, because
   * two customers can trade under similar names and the code is what an operator
   * quotes when they need to be exact.
   */
  const columns = [
    { key: 'label', header: t('filters.customer') },
    { key: 'code', header: t('customers.code') },
    { key: 'net_sales', header: t('sales.netSales') },
    { key: 'quantity', header: t('sales.quantity') },
    { key: 'invoice_count', header: t('customers.orderCount') },
    { key: 'last_transaction', header: t('customers.lastTransaction') },
  ];

  return (
    <>
      <PageHeader
        title={t('customers.title')}
        period={data?.period}
        description={data?.note}
        actions={
          <ExportButtons
            reportName={t('customers.title')}
            rows={rows}
            period={data?.period}
          />
        }
      />

      <GlobalFilterBar />

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<CardSkeleton rows={6} />}
        isEmpty={rows.length === 0}
      >
        <div className="space-y-4">
          <Section title={t('customers.top')}>
            <CategoryBarChart
              data={data?.top_customers ?? []}
              xKey="label"
              yKey="net_sales"
            />
          </Section>

          {/*
            One panel, full width. The High Outstanding table stood beside this
            one until the receivables module left; a two-column grid with one
            card in it reads as something failed to load, so the grid goes too.
          */}
          <Section title={t('customers.inactive')}>
            <DataTable
              rows={data?.inactive_customers ?? []}
              columns={[
                { key: 'label', header: t('filters.customer') },
                { key: 'code', header: t('customers.code') },
                { key: 'net_sales', header: t('sales.netSales') },
                { key: 'last_transaction', header: t('customers.lastTransaction') },
              ]}
              searchable={false}
              pageSize={10}
              emptyMessage={t('common.noData')}
              rowKey={(row, index) => `inactive-${row.code ?? index}`}
            />
          </Section>

          <Section title={t('customers.title')}>
            <DataTable
              rows={rows}
              columns={columns}
              pageSize={25}
              rowKey={(row, index) => String(row.code ?? index)}
            />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
