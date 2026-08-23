/**
 * The enterprise data table used by every report.
 *
 * Supports search, sort, pagination, column visibility, row click-through,
 * row details and export. It works in two modes:
 *
 *  - **client mode** for the small aggregated result sets a report returns
 *    (a region breakdown is tens of rows, not thousands);
 *  - **server mode** for transaction tables, where sorting, searching and
 *    paging are delegated to the backend so the browser never receives the
 *    fact table.
 */

import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Columns3,
  Search,
} from 'lucide-react';
import { Fragment, useMemo, useState, type ReactNode } from 'react';
import { useT } from '../contexts/I18nContext';
import { EmptyState } from '../components/States';
import {
  formatCell,
  humanizeColumn,
  isNumericColumn,
  stockStatusClass,
} from '../utils/format';

export interface Column<T> {
  key: string;
  header?: string;
  /** Custom renderer; falls back to `formatCell` on the raw value. */
  render?: (row: T) => ReactNode;
  align?: 'left' | 'right' | 'center';
  sortable?: boolean;
  /** Hidden by default but available from the column picker. */
  hidden?: boolean;
  width?: string;
}

export interface DataTableProps<T extends Record<string, any>> {
  rows: T[];
  columns: Column<T>[];
  /** Delegate search, sort and paging to the server. */
  serverMode?: boolean;
  page?: number;
  pageSize?: number;
  total?: number;
  totalPages?: number;
  onPageChange?: (page: number) => void;
  search?: string;
  onSearchChange?: (value: string) => void;
  sortBy?: string;
  sortDir?: 'asc' | 'desc';
  onSortChange?: (column: string, direction: 'asc' | 'desc') => void;
  onRowClick?: (row: T) => void;
  renderRowDetail?: (row: T) => ReactNode;
  emptyMessage?: string;
  /** Rendered in the toolbar — normally the export buttons. */
  toolbar?: ReactNode;
  /**
   * Keep the search / column / export row in view while the rows scroll past.
   *
   * Opt-in rather than always on, because most report pages already carry a
   * sticky `GlobalFilterBar`: two bars sticking to the same offset would land on
   * top of each other. This is for the pages that have no filter bar of their
   * own — Master Data and the Upload Centre — where this row *is* the page's
   * toolbar.
   *
   * `z-20` puts it over the table's own sticky `thead` (`z-10`) and under a
   * filter bar (`z-30`) on any page that later gains one.
   */
  stickyToolbar?: boolean;
  searchable?: boolean;
  pageSizeOptions?: number[];
  onPageSizeChange?: (size: number) => void;
  rowKey?: (row: T, index: number) => string;
  dense?: boolean;
  /**
   * Turn on row selection. The selected keys are owned by the caller, because
   * a selection has to survive a page change and a refetch — bulk actions and
   * "show selected on the map" both depend on that.
   */
  selectable?: boolean;
  selected?: Set<string>;
  onSelectionChange?: (keys: Set<string>) => void;
  /** Rendered in a sticky right-hand column. */
  renderRowActions?: (row: T) => ReactNode;
  /** Shown above the table when at least one row is selected. */
  bulkBar?: ReactNode;
  /** Rows that should read as inactive — retired or voided. */
  isMuted?: (row: T) => boolean;
  /** Show/hide state lifted out, so a page can remember the user's choice. */
  hiddenColumns?: Set<string>;
  onHiddenColumnsChange?: (hidden: Set<string>) => void;
  /** Replaces "Showing N of M" when the server knows the real range. */
  rangeLabel?: string;
}

export function DataTable<T extends Record<string, any>>({
  rows,
  columns,
  serverMode = false,
  page = 1,
  pageSize = 20,
  total,
  totalPages,
  onPageChange,
  search,
  onSearchChange,
  sortBy,
  sortDir = 'desc',
  onSortChange,
  onRowClick,
  renderRowDetail,
  emptyMessage,
  toolbar,
  stickyToolbar = false,
  searchable = true,
  pageSizeOptions,
  onPageSizeChange,
  rowKey,
  dense = false,
  selectable = false,
  selected,
  onSelectionChange,
  renderRowActions,
  bulkBar,
  isMuted,
  hiddenColumns,
  onHiddenColumnsChange,
  rangeLabel,
}: DataTableProps<T>) {
  const t = useT();
  const [clientSearch, setClientSearch] = useState('');
  const [clientPage, setClientPage] = useState(1);
  const [clientSort, setClientSort] = useState<{ key: string; dir: 'asc' | 'desc' } | null>(
    null,
  );
  const [ownHidden, setOwnHidden] = useState<Set<string>>(
    () => new Set(columns.filter((column) => column.hidden).map((column) => column.key)),
  );
  const [pickerOpen, setPickerOpen] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Controlled when the page wants to persist the choice, uncontrolled
  // otherwise, so every existing caller keeps working untouched.
  const hidden = hiddenColumns ?? ownHidden;
  const setHidden = (next: Set<string>) => {
    if (onHiddenColumnsChange) onHiddenColumnsChange(next);
    else setOwnHidden(next);
  };

  const visibleColumns = columns.filter((column) => !hidden.has(column.key));

  const activeSearch = serverMode ? (search ?? '') : clientSearch;

  const processed = useMemo(() => {
    if (serverMode) return rows;

    let result = rows;
    if (clientSearch.trim()) {
      const needle = clientSearch.trim().toLowerCase();
      result = result.filter((row) =>
        visibleColumns.some((column) =>
          String(row[column.key] ?? '')
            .toLowerCase()
            .includes(needle),
        ),
      );
    }
    if (clientSort) {
      const { key, dir } = clientSort;
      result = [...result].sort((left, right) => {
        const a = left[key];
        const b = right[key];
        if (a === b) return 0;
        if (a === null || a === undefined) return 1;
        if (b === null || b === undefined) return -1;
        const comparison =
          typeof a === 'number' && typeof b === 'number'
            ? a - b
            : String(a).localeCompare(String(b));
        return dir === 'asc' ? comparison : -comparison;
      });
    }
    return result;
  }, [rows, serverMode, clientSearch, clientSort, visibleColumns]);

  const clientTotalPages = Math.max(1, Math.ceil(processed.length / pageSize));
  const currentPage = serverMode ? page : Math.min(clientPage, clientTotalPages);
  const pageCount = serverMode ? (totalPages ?? 1) : clientTotalPages;
  const rowCount = serverMode ? (total ?? rows.length) : processed.length;

  const visibleRows = serverMode
    ? processed
    : processed.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  function handleSort(column: Column<T>) {
    if (column.sortable === false) return;
    const nextDir: 'asc' | 'desc' =
      (serverMode ? sortBy : clientSort?.key) === column.key &&
      (serverMode ? sortDir : clientSort?.dir) === 'desc'
        ? 'asc'
        : 'desc';
    if (serverMode) onSortChange?.(column.key, nextDir);
    else setClientSort({ key: column.key, dir: nextDir });
  }

  function handleSearch(value: string) {
    if (serverMode) onSearchChange?.(value);
    else {
      setClientSearch(value);
      setClientPage(1);
    }
  }

  function goToPage(next: number) {
    const bounded = Math.max(1, Math.min(next, pageCount));
    if (serverMode) onPageChange?.(bounded);
    else setClientPage(bounded);
  }

  const activeSortKey = serverMode ? sortBy : clientSort?.key;
  const activeSortDir = serverMode ? sortDir : clientSort?.dir;

  // ``_key`` and ``id`` are real row identities. ``code`` is not: it is unique
  // in a grouped report and repeats freely in a detail one, so it only
  // contributes to the key alongside the index. Callers that have a true
  // identity pass ``rowKey`` and bypass all of this.
  const keyOf = (row: T, index: number) =>
    rowKey?.(row, index) ?? String(row._key ?? row.id ?? `${row.code ?? 'row'}-${index}`);

  const pageKeys = visibleRows.map((row, index) => keyOf(row, index));
  const allOnPageSelected =
    pageKeys.length > 0 && pageKeys.every((key) => selected?.has(key));

  function toggleRow(key: string) {
    const next = new Set(selected ?? []);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelectionChange?.(next);
  }

  function togglePage() {
    const next = new Set(selected ?? []);
    // "Select all" means this page, not the whole result set: a checkbox that
    // silently selected 12,450 unseen rows would be a trap on a bulk action.
    if (allOnPageSelected) pageKeys.forEach((key) => next.delete(key));
    else pageKeys.forEach((key) => next.add(key));
    onSelectionChange?.(next);
  }

  return (
    <div className="flex flex-col gap-3">
      {bulkBar}
      {/*
        The background is not decoration: a sticky row with a transparent
        background lets the table rows scroll visibly underneath it.
      */}
      <div
        className={`flex flex-wrap items-center justify-between gap-2 ${
          stickyToolbar
            ? 'sticky top-[var(--app-header-height,3.5rem)] z-20 -mx-1 bg-white px-1 py-2 dark:bg-slate-900'
            : ''
        }`}
      >
        {searchable && (
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <Search
              size={16}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
            />
            <input
              type="search"
              className="input pl-9"
              placeholder={t('common.search')}
              value={activeSearch}
              onChange={(event) => handleSearch(event.target.value)}
              aria-label={t('common.search')}
            />
          </div>
        )}

        <div className="flex items-center gap-2">
          {toolbar}
          <div className="relative">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setPickerOpen((open) => !open)}
              aria-expanded={pickerOpen}
              aria-haspopup="true"
            >
              <Columns3 size={14} />
              <span className="hidden sm:inline">{t('common.columns')}</span>
            </button>
            {pickerOpen && (
              <div className="absolute right-0 z-20 mt-1 max-h-72 w-56 overflow-y-auto rounded-lg border border-slate-200 bg-white p-2 shadow-lg dark:border-slate-700 dark:bg-slate-900">
                {columns.map((column) => (
                  <label
                    key={column.key}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
                  >
                    <input
                      type="checkbox"
                      checked={!hidden.has(column.key)}
                      onChange={() => {
                        const next = new Set(hidden);
                        if (next.has(column.key)) next.delete(column.key);
                        else next.add(column.key);
                        setHidden(next);
                      }}
                    />
                    <span className="truncate">
                      {column.header ?? humanizeColumn(column.key)}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {visibleRows.length === 0 ? (
        <EmptyState message={emptyMessage} />
      ) : (
        <div className="table-wrap rounded-lg border border-slate-200 dark:border-slate-800">
          <table className="w-full min-w-max border-collapse text-sm">
            {/* Sticky so the header survives scrolling a long page of rows. */}
            <thead className="sticky top-0 z-10">
              <tr className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
                {selectable && (
                  <th scope="col" className="w-10 px-3 py-2">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-brand-600"
                      checked={allOnPageSelected}
                      onChange={togglePage}
                      aria-label={t('table.selectPage')}
                    />
                  </th>
                )}
                {visibleColumns.map((column) => {
                  const numeric =
                    column.align === 'right' ||
                    (column.align === undefined &&
                      isNumericColumn(column.key, visibleRows[0]?.[column.key]));
                  const isSorted = activeSortKey === column.key;
                  // A stock status column carries its colour in the heading as
                  // well as in the cells below it: the heading *is* the label
                  // half of the metric here, and colouring only the figures
                  // would say the number is a status and its name is not.
                  const status = stockStatusClass(column.key);
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      style={column.width ? { width: column.width } : undefined}
                      className={`whitespace-nowrap px-3 py-2 font-medium ${
                        status ?? 'text-slate-600 dark:text-slate-300'
                      } ${numeric ? 'text-right' : 'text-left'}`}
                    >
                      <button
                        type="button"
                        onClick={() => handleSort(column)}
                        disabled={column.sortable === false}
                        className={`inline-flex items-center gap-1 ${
                          column.sortable === false ? 'cursor-default' : 'hover:text-brand-600'
                        }`}
                      >
                        {column.header ?? humanizeColumn(column.key)}
                        {isSorted &&
                          (activeSortDir === 'asc' ? (
                            <ChevronUp size={12} />
                          ) : (
                            <ChevronDown size={12} />
                          ))}
                      </button>
                    </th>
                  );
                })}
                {renderRowActions && (
                  <th
                    scope="col"
                    className="w-px whitespace-nowrap px-3 py-2 text-right font-medium text-slate-600 dark:text-slate-300"
                  >
                    {t('table.actions')}
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((row, index) => {
                const key = keyOf(row, index);
                const isExpanded = expanded === key;
                const muted = isMuted?.(row) ?? false;
                return (
                  // The key belongs on the outermost element the callback
                  // returns. On the inner <tr> React sees an unkeyed fragment
                  // and warns — and loses row identity across re-renders.
                  <Fragment key={key}>
                    <tr
                      onClick={() => {
                        if (renderRowDetail) setExpanded(isExpanded ? null : key);
                        onRowClick?.(row);
                      }}
                      className={`border-b border-slate-100 last:border-0 dark:border-slate-800 ${
                        onRowClick || renderRowDetail
                          ? 'cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50'
                          : ''
                      } ${muted ? 'opacity-60' : ''}`}
                    >
                      {selectable && (
                        <td className="px-3 py-2">
                          <input
                            type="checkbox"
                            className="h-4 w-4 accent-brand-600"
                            checked={selected?.has(key) ?? false}
                            onClick={(event) => event.stopPropagation()}
                            onChange={() => toggleRow(key)}
                            aria-label={t('table.selectRow')}
                          />
                        </td>
                      )}
                      {visibleColumns.map((column) => {
                        const numeric =
                          column.align === 'right' ||
                          (column.align === undefined &&
                            isNumericColumn(column.key, row[column.key]));
                        const status = stockStatusClass(column.key);
                        return (
                          <td
                            key={column.key}
                            className={`px-3 ${dense ? 'py-1.5' : 'py-2'} ${
                              numeric ? 'text-right tabular-nums' : 'text-left'
                            } ${status ?? 'text-slate-700 dark:text-slate-200'}`}
                          >
                            {column.render
                              ? column.render(row)
                              : formatCell(column.key, row[column.key])}
                          </td>
                        );
                      })}
                      {renderRowActions && (
                        <td className="px-3 py-1.5">{renderRowActions(row)}</td>
                      )}
                    </tr>
                    {isExpanded && renderRowDetail && (
                      <tr className="bg-slate-50 dark:bg-slate-900/60">
                        <td
                          colSpan={
                            visibleColumns.length +
                            (selectable ? 1 : 0) +
                            (renderRowActions ? 1 : 0)
                          }
                          className="px-4 py-3"
                        >
                          {renderRowDetail(row)}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500 dark:text-slate-400">
        <span>
          {rangeLabel ?? (
            <>
              {t('common.showing')} {visibleRows.length} {t('common.of')} {rowCount}{' '}
              {t('common.rows')}
            </>
          )}
        </span>

        <div className="flex items-center gap-2">
          {pageSizeOptions && onPageSizeChange && (
            <select
              className="rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              aria-label={t('common.rows')}
            >
              {pageSizeOptions.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            className="btn-ghost px-2 py-1"
            onClick={() => goToPage(currentPage - 1)}
            disabled={currentPage <= 1}
            aria-label={t('common.previous')}
          >
            <ChevronLeft size={16} />
          </button>
          <span className="tabular-nums">
            {t('common.page')} {currentPage} {t('common.of')} {pageCount}
          </span>
          <button
            type="button"
            className="btn-ghost px-2 py-1"
            onClick={() => goToPage(currentPage + 1)}
            disabled={currentPage >= pageCount}
            aria-label={t('common.next')}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

/** Build table columns straight from a tool result's rows. */
export function columnsFromRows<T extends Record<string, any>>(
  rows: T[],
  preferred?: string[],
): Column<T>[] {
  if (rows.length === 0) return [];
  const available = Object.keys(rows[0]);
  const ordered = preferred
    ? preferred.filter((key) => available.includes(key))
    : available;
  return ordered.map((key) => ({ key, header: humanizeColumn(key) }));
}
