/**
 * Row-level transaction table with server-side paging, search and sort.
 *
 * The fact tables are never shipped to the browser: only the current page is
 * fetched, and the backend applies the caller's data scope to every query.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { ExportButtons } from '../components/ExportButtons';
import { QueryState } from '../components/States';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { transactionService } from '../services';
import { useDebounced } from '../hooks/useDebounced';
import { humanizeColumn } from '../utils/format';
import { DataTable } from './DataTable';

const PAGE_SIZES = [25, 50, 100, 200];

export function TransactionTable({
  dataType,
  title,
}: {
  dataType: 'sales' | 'collection' | 'outstanding' | 'material_stock' | 'target';
  title?: string;
}) {
  const t = useT();
  const { query } = useFilters();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<{ by?: string; dir: 'asc' | 'desc' }>({ dir: 'desc' });
  const debouncedSearch = useDebounced(search, 400);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: [
      'transactions',
      dataType,
      query,
      page,
      pageSize,
      debouncedSearch,
      sort.by,
      sort.dir,
    ],
    queryFn: () =>
      transactionService.list(dataType, {
        ...query,
        page,
        page_size: pageSize,
        search: debouncedSearch || undefined,
        sort_by: sort.by,
        sort_dir: sort.dir,
      }),
    // Keeping the previous page visible avoids a flash of empty table while
    // the next page loads.
    placeholderData: keepPreviousData,
  });

  const columns = (data?.columns ?? []).map((key) => ({
    key,
    header: humanizeColumn(key),
  }));

  return (
    <QueryState isLoading={isLoading} error={error} onRetry={() => void refetch()}>
      <DataTable
        // The dataset decides which columns the server returns.
        tableId={`transactions.${dataType}`}
        rows={data?.rows ?? []}
        columns={columns}
        serverMode
        page={data?.page ?? page}
        pageSize={pageSize}
        total={data?.total}
        totalPages={data?.total_pages}
        onPageChange={setPage}
        search={search}
        onSearchChange={(value) => {
          setSearch(value);
          setPage(1);
        }}
        sortBy={sort.by}
        sortDir={sort.dir}
        onSortChange={(by, dir) => {
          setSort({ by, dir });
          setPage(1);
        }}
        pageSizeOptions={PAGE_SIZES}
        onPageSizeChange={(size) => {
          setPageSize(size);
          setPage(1);
        }}
        dense
        toolbar={
          <>
            {isFetching && (
              <span className="text-xs text-slate-400">{t('common.loading')}</span>
            )}
            <ExportButtons
              reportName={title ?? `${dataType} transactions`}
              rows={data?.rows ?? []}
              period={data?.period}
              compact
            />
          </>
        }
        // An invoice number is not a row identity: a sales invoice has one row
        // per line, which is why the schema carries invoice_line_no at all. The
        // page-local index closes the remaining cases (stock and target lines
        // share no single identifying column), so keys are unique per page.
        rowKey={(row, index) =>
          [row.invoice_no ?? row.material_code ?? 'row',
           row.invoice_line_no ?? '',
           index].join('|')
        }
      />
    </QueryState>
  );
}
