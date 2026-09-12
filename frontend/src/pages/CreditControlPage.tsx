/**
 * Credit Control: what is owed, how late it is, and against which invoices.
 *
 * **Every figure here is a function of a date, and the page says which.** An
 * invoice is Not Yet Due in June and Over Due in August; the status, the aging
 * bucket and the days overdue are all resolved server-side against the As On
 * date, which defaults to today and is a control rather than an assumption.
 * That is why the date sits in the header beside the period, and why it travels
 * with every one of the four requests — a page that sent it to the summary and
 * not to the table would show a KPI strip disagreeing with the rows beneath it.
 *
 * **This page computes nothing.** Outstanding, overdue, the eight aging buckets,
 * each customer's exposure share — all arrive aggregated. The one thing done
 * here is choosing which of them to draw.
 *
 * **A figure that cannot be computed reads `n/a`, never `0`.** A portfolio with
 * nothing outstanding has no overdue *proportion*; rendering `0%` would be good
 * news about a book that does not exist. Same for the outstanding trend, which
 * this platform genuinely cannot measure — the source states one aggregate
 * payment per invoice, so what was owed at a past month end is unrecorded, and
 * the panel prints that sentence rather than drawing an empty chart.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AGING_COLORS, CategoryBarChart, ComparisonBarChart, DonutChart } from '../charts/Charts';
import { AgingMatrix } from '../components/AgingMatrix';
import { ExportButtons } from '../components/ExportButtons';
import { InvoiceDetailPanel } from '../components/InvoiceDetailPanel';
import { StatCard } from '../components/KpiCard';
import { PageHeader, Section } from '../components/PageHeader';
import { KpiSkeleton, QueryState } from '../components/States';
import {
  CREDIT_FILTERS,
  FILTER_LABELS,
  HIERARCHY_ORDER,
  useFilters,
} from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { creditService } from '../services';
import { DataTable, renderTotalValue, type Column } from '../tables/DataTable';
import { sumWhere } from '../tables/totals';
import type {
  FilterLevel,
  CreditExposureRow,
  CreditCustomerPage,
  CreditCustomerRow,
  CreditInvoicePage,
  CreditInvoiceRow,
} from '../types/api';
import { formatAmount, formatDate, formatPercent, statusClass } from '../utils/format';

type View = 'customers' | 'invoices' | 'hierarchy';

/** One label per tab, so the button and the section heading cannot drift. */
const VIEW_LABEL: Record<View, string> = {
  customers: 'credit.customerView',
  invoices: 'credit.invoiceView',
  hierarchy: 'credit.hierarchyView',
};

const PAGE_SIZE = 25;

/**
 * The levels this page can cut receivables by.
 *
 * Derived from `HIERARCHY_ORDER` rather than listed, so a level added to the
 * warehouse appears in this selector without anybody editing a list here — and
 * it mirrors `reporting.credit.EXPOSURE_LEVELS`, which derives itself the same
 * way on the server. The server validates what arrives regardless; this list
 * decides what a reader is *offered*, and the two deriving from the same chain
 * is what keeps the offer and the refusal in step.
 *
 * `customer_code` is last because a customer is where credit exposure actually
 * lives: it is the entity whose supply gets stopped.
 */
const EXPOSURE_LEVELS: FilterLevel[] = [...HIERARCHY_ORDER, 'customer_code'];

export default function CreditControlPage() {
  const t = useT();
  const { queryFor } = useFilters();
  const [params, setParams] = useSearchParams();

  /*
    Tab, As On date, paging, sort and search all live in the URL, the way every
    other filter on this application does. "V1 of the invoice list, sorted by
    balance, as at month end" is then a link somebody can send, and the back
    button steps through it.
  */
  const view = (params.get('view') as View) ?? 'customers';
  /*
    Which organisational level the matrix, the exposure chart and the hierarchy
    tab are cut at. In the URL like everything else on this page, so "receivables
    by territory as at month end" is a link — and read by the *server*, which
    validates it against the levels the credit view can actually group by and
    refuses an unknown one rather than quietly falling back.
  */
  const level = params.get('level') ?? 'region_code';
  const asOn = params.get('as_on') ?? undefined;
  const page = Number(params.get('page') ?? '1');
  const search = params.get('q') ?? '';
  const sortBy = params.get('sort') ?? undefined;
  const sortDir = (params.get('dir') as 'asc' | 'desc') ?? 'desc';

  const [openInvoice, setOpenInvoice] = useState<
    { company: string; invoice: string } | null
  >(null);

  function update(next: Record<string, string | undefined>) {
    const merged = new URLSearchParams(params);
    Object.entries(next).forEach(([key, value]) => {
      if (value === undefined || value === '') merged.delete(key);
      else merged.set(key, value);
    });
    setParams(merged, { replace: true });
  }

  // Only the filters this page can honour. Filter state is global and lives in
  // the URL, so arriving from Sales carries a region and a material the credit
  // view has no column for; sending them would make the page look narrowed while
  // every figure stayed the total.
  const base = queryFor(CREDIT_FILTERS);
  const query = { ...base, as_on_date: asOn, group_level: level };
  const tableQuery = {
    ...query,
    search: search || undefined,
    sort_by: sortBy,
    sort_dir: sortDir,
    page,
    page_size: PAGE_SIZE,
  };

  const summary = useQuery({
    queryKey: ['credit-summary', query],
    queryFn: () => creditService.summary(query),
  });

  // The union is stated rather than inferred: the two endpoints return the same
  // envelope around different rows, and without it TypeScript narrows to
  // whichever branch it sees first and rejects the other.
  const table = useQuery<CreditInvoicePage | CreditCustomerPage>({
    queryKey: ['credit-table', view, tableQuery],
    queryFn: () =>
      view === 'invoices'
        ? creditService.invoices(tableQuery)
        : creditService.customers(tableQuery),
  });

  const metrics = summary.data?.metrics;
  /*
    The stack's second segment. Not a business calculation — both figures come
    from the server and this is only the subtraction a stacked bar implies, done
    once here rather than in the chart so the tooltip can name it. Floored at
    zero: a group whose overdue figure somehow exceeded its outstanding one is a
    data-quality problem, and drawing a negative segment would render it as a bar
    growing downwards rather than as the nothing it should be.
  */
  const exposureRows = (summary.data?.exposure_by_level?.rows ?? []).map((row) => ({
    ...row,
    not_overdue_amount: Math.max(
      (row.outstanding_amount ?? 0) - (row.overdue_amount ?? 0),
      0,
    ),
  }));
  const trend = summary.data?.outstanding_trend;

  /*
    The hierarchy table's columns, and **one `tableId` for every level**.

    `credit-control.hierarchy`, not `credit-control.hierarchy.${level}`. Every
    level lists exactly these columns — only what the code and name refer to
    changes — so a reader who hid Customers and widened Outstanding while looking
    at regions means that arrangement to survive switching to territories. This
    is the rule `performance.breakdown` follows across its drill levels, and the
    opposite of `master-data.${entityKey}`, where each entity genuinely has
    different columns and an arrangement could not transfer.

    `reconcileOrder` is what makes it safe: a stored order naming a column this
    table no longer has drops it, and a column the table has *gained* is inserted
    beside the one it was declared after rather than silently omitted — which
    would leave a column with no control anywhere to bring it back.
  */
  const hierarchyColumns: Column<CreditExposureRow>[] = [
    { key: 'name', header: t('credit.group'), sortable: true },
    // Hidden by default rather than absent: the code is what an export and a
    // support conversation need, and what a reader scanning regions does not.
    // Hidden is a starting arrangement, not a decision — the column panel
    // brings it back, and `reconcileOrder` keeps it reachable.
    { key: 'code', header: t('credit.groupCode'), sortable: true, hidden: true },
    {
      key: 'outstanding_amount',
      header: t('credit.outstanding'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.outstanding_amount),
      total: (rows: CreditExposureRow[]) =>
        renderTotalValue(
          sumWhere(rows, 'outstanding_amount', (row) =>
            (row.outstanding_amount ?? 0) >= 0),
          'outstanding_amount',
          t,
        ),
    },
    {
      key: 'overdue_amount',
      header: t('credit.overdue'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.overdue_amount),
      total: (rows: CreditExposureRow[]) =>
        renderTotalValue(
          sumWhere(rows, 'overdue_amount', () => true),
          'overdue_amount',
          t,
        ),
    },
    {
      key: 'overdue_share_percent',
      header: t('credit.overdueShare'),
      align: 'right',
      sortable: true,
      // `n/a`, never 0%. A group with nothing outstanding has no overdue
      // proportion, and the server sends null rather than a zero for exactly
      // that reason — rendering it as 0% here would undo the distinction.
      render: (row) =>
        row.overdue_share_percent === null
          ? t('common.notAvailable')
          : formatPercent(row.overdue_share_percent),
      // No footer. A total of percentages is not a percentage, and the
      // portfolio-wide share is already on the card above.
    },
    {
      key: 'open_invoice_count',
      header: t('credit.openInvoicesColumn'),
      align: 'right',
      sortable: true,
    },
    {
      key: 'customer_count',
      header: t('credit.customers'),
      align: 'right',
      sortable: true,
    },
    {
      key: 'invoice_count',
      header: t('credit.invoices'),
      align: 'right',
      sortable: true,
      hidden: true,
    },
  ];

  const invoiceColumns: Column<CreditInvoiceRow>[] = [
    { key: 'invoice_no', header: t('credit.invoiceNo'), sortable: true },
    { key: 'company_code', header: t('filters.company'), sortable: true, hidden: true },
    { key: 'plant_name', header: t('credit.plant'), hidden: true },
    { key: 'customer_name', header: t('credit.customer'), sortable: true },
    { key: 'customer_code', header: t('credit.customerCode'), hidden: true },
    {
      key: 'invoice_date',
      header: t('credit.invoiceDate'),
      sortable: true,
      render: (row) => formatDate(row.invoice_date),
    },
    { key: 'credit_days', header: t('filters.creditDays'), align: 'right', sortable: true },
    {
      key: 'invoice_value',
      header: t('credit.invoiceValue'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.invoice_value),
    },
    {
      key: 'net_invoice_amount',
      header: t('credit.netInvoice'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.net_invoice_amount),
      total: 'sum' as const,
    },
    {
      key: 'payment_amount',
      header: t('credit.payment'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.payment_amount),
    },
    {
      key: 'balance_amount',
      header: t('credit.balance'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.balance_amount),
    },
    {
      key: 'due_date',
      header: t('credit.dueDate'),
      sortable: true,
      // The days-overdue note rides with the due date rather than taking a
      // column of its own: it is a reading of that date, and a reader looking
      // at one wants the other.
      render: (row) => (
        <span>
          {formatDate(row.due_date)}
          {row.days_overdue ? (
            <span className="ml-1 text-xs text-red-600 dark:text-red-400">
              {t('credit.overdueByDays', { count: row.days_overdue })}
            </span>
          ) : null}
        </span>
      ),
    },
    { key: 'payment_mode', header: t('filters.paymentMode'), hidden: true },
    {
      key: 'last_payment_date',
      header: t('credit.lastPayment'),
      hidden: true,
      render: (row) => (row.last_payment_date ? formatDate(row.last_payment_date) : '—'),
    },
    {
      key: 'clearing_date',
      header: t('credit.clearingDate'),
      hidden: true,
      render: (row) => (row.clearing_date ? formatDate(row.clearing_date) : '—'),
    },
    { key: 'clearing_document', header: t('credit.clearingDocument'), hidden: true },
    {
      key: 'credit_status',
      header: t('credit.status'),
      render: (row) => (
        <span className={`badge ${statusClass(row.credit_status)}`}>
          {t(`credit.status.${row.credit_status}`)}
        </span>
      ),
    },
  ];

  const customerColumns: Column<CreditCustomerRow>[] = [
    { key: 'customer_name', header: t('credit.customer'), sortable: true },
    { key: 'customer_code', header: t('credit.customerCode'), sortable: true },
    { key: 'company_code', header: t('filters.company'), hidden: true },
    {
      key: 'invoice_count',
      header: t('credit.invoices'),
      align: 'right',
      sortable: true,
      total: 'sum' as const,
    },
    {
      key: 'total_invoice_amount',
      header: t('credit.invoiceValue'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.total_invoice_amount),
      total: 'sum' as const,
    },
    {
      key: 'net_invoice_amount',
      header: t('credit.netInvoice'),
      align: 'right',
      sortable: true,
      hidden: true,
      render: (row) => formatAmount(row.net_invoice_amount),
    },
    {
      key: 'payment_amount',
      header: t('credit.payment'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.payment_amount),
    },
    {
      key: 'outstanding_amount',
      header: t('credit.outstanding'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.outstanding_amount),
      // The backend's exposure total leaves out a negative balance
      // (BALANCE_NEGATIVE) so an over-adjusted invoice cannot net off
      // against real debt. This footer sits under that KPI, so it applies
      // the same exclusion rather than disagreeing with the card above it.
      total: (totalled: CreditCustomerRow[]) =>
        renderTotalValue(
          sumWhere(totalled, 'outstanding_amount', (row) =>
            (row.outstanding_amount ?? 0) >= 0),
          'outstanding_amount',
          t,
        ),
    },
    {
      key: 'overdue_amount',
      header: t('credit.overdue'),
      align: 'right',
      sortable: true,
      render: (row) => formatAmount(row.overdue_amount),
      total: 'sum' as const,
    },
    {
      key: 'credit_exposure_percent',
      header: t('credit.exposure'),
      align: 'right',
      // `n/a` when nothing is outstanding, never 0%. There is no credit limit
      // in the Customer Master, so this is a share of the portfolio — a real
      // ratio of two figures rather than an invented limit.
      render: (row) => formatPercent(row.credit_exposure_percent),
    },
    {
      key: 'oldest_due_date',
      header: t('credit.oldestDue'),
      sortable: true,
      render: (row) =>
        row.oldest_due_date ? formatDate(row.oldest_due_date) : t('common.notAvailable'),
    },
    {
      key: 'last_payment_date',
      header: t('credit.lastPayment'),
      sortable: true,
      hidden: true,
      render: (row) =>
        row.last_payment_date ? formatDate(row.last_payment_date) : t('common.notAvailable'),
    },
  ];

  return (
    <>
      <PageHeader
        title={t('credit.title')}
        description={
          summary.data
            ? t('credit.asOnNote', {
                date: formatDate(summary.data.as_on_date),
                count: metrics?.invoice_count ?? 0,
              })
            : undefined
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-slate-500">
              {t('credit.asOn')}
              <input
                type="date"
                className="input tap-y py-1 text-xs"
                value={asOn ?? summary.data?.as_on_date ?? ''}
                onChange={(event) => update({ as_on: event.target.value, page: undefined })}
              />
            </label>
            <ExportButtons
              reportName={t('credit.title')}
              // Whichever table is on screen is what gets exported, so an export
              // carries the columns the reader was looking at rather than a
              // second, invisible query's.
              rows={(table.data?.rows ?? []) as unknown as Record<string, unknown>[]}
            />
          </div>
        }
      />

      <GlobalFilterBar levels={[]} showIndependent={false} pageFilters={CREDIT_FILTERS} />

      <QueryState
        isLoading={summary.isLoading}
        error={summary.error}
        onRetry={() => void summary.refetch()}
        skeleton={<KpiSkeleton count={7} />}
      >
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 min-[360px]:grid-cols-2 lg:grid-cols-7">
            <StatCard
              label={t('credit.totalInvoice')}
              value={formatAmount(metrics?.total_invoice_amount)}
              hint={t('credit.invoiceCount', { count: metrics?.invoice_count ?? 0 })}
            />
            {/*
              "Net Invoice" promised a reduction this data does not always
              deliver: returns are posted negative and subtracted, so the net
              figure can exceed the gross one — 1.59 Cr against 1.41 Cr on the
              first real file, which reads as a bug to anyone who has not been
              told about the sign convention.

              The label is now literal about the operation rather than about its
              expected direction, and the hint carries the returns total signed,
              so the number that caused the surprise is on the card beside it.
              The column heading in the tables below is untouched: a per-row net
              is read next to that row's own return, where the arithmetic is
              already visible.
            */}
            <StatCard
              label={t('credit.invoiceLessReturns')}
              value={formatAmount(metrics?.net_invoice_amount)}
              hint={t('credit.returnsHint', {
                amount: formatAmount(metrics?.return_amount),
              })}
            />
            <StatCard
              label={t('credit.totalPayment')}
              value={formatAmount(metrics?.payment_amount)}
              hint={
                // `n/a` rather than 0% when nothing has been invoiced.
                metrics?.payment_rate_percent === null
                  ? t('common.notAvailable')
                  : t('credit.ofNetInvoice', {
                      percent: formatPercent(metrics?.payment_rate_percent),
                    })
              }
            />
            <StatCard
              label={t('credit.outstanding')}
              value={formatAmount(metrics?.outstanding_amount)}
              hint={t('credit.openInvoices', { count: metrics?.open_invoice_count ?? 0 })}
            />
            <StatCard
              label={t('credit.overdue')}
              value={formatAmount(metrics?.overdue_amount)}
              tone="danger"
              hint={
                metrics?.overdue_share_percent === null
                  ? t('credit.invoiceCount', { count: metrics?.overdue_invoice_count ?? 0 })
                  : t('credit.overdueHint', {
                      count: metrics?.overdue_invoice_count ?? 0,
                      percent: formatPercent(metrics?.overdue_share_percent),
                    })
              }
            />
            <StatCard
              label={t('credit.dueSoon')}
              value={formatAmount(metrics?.due_soon_amount)}
              tone="warning"
              hint={t('credit.dueSoonHint', {
                count: metrics?.due_soon_invoice_count ?? 0,
                days: summary.data?.due_soon_days ?? 0,
              })}
            />
            {/*
              The seventh card, and the one that makes the other two add up.

              Overdue and Due Soon were shown without it, so the two largest
              figures on this strip did not account for Outstanding and nothing
              on screen said what the remainder was. On the real book it is the
              *largest* of the three — money that is neither late nor imminent,
              which the source's own columns do not publish either. Neutral tone
              deliberately: it is not a warning, it is the rest of the book.
            */}
            <StatCard
              label={t('credit.dueLater')}
              value={formatAmount(metrics?.due_later_amount)}
              hint={t('credit.dueLaterHint', {
                count: metrics?.due_later_invoice_count ?? 0,
                days: summary.data?.due_soon_days ?? 0,
              })}
            />
          </div>

          {/*
            The backend's notes, printed rather than summarised. Currently one:
            how many invoices carry a data-quality flag. It belongs here rather
            than only on the Data Quality page because a negative balance changes
            what the outstanding total means, and whoever reads the total is owed
            that.
          */}
          {summary.data?.notes.map((note) => (
            <p
              key={note.code}
              className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
            >
              {note.message}
            </p>
          ))}

          <Section title={t('credit.aging')}>
            <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
              {t('credit.agingNote')}
            </p>
            {/*
              `colorByIndex={false}` is what makes the bars pick up
              `AGING_COLORS`, so the ramp runs green through red with severity
              rather than cycling the categorical palette.
            */}
            <CategoryBarChart
              data={summary.data?.aging ?? []}
              xKey="bucket"
              yKey="outstanding_amount"
              colorByIndex={false}
              // Horizontal, because eight bucket names do not fit across a
              // phone and were being rotated to the point of illegibility. Read
              // down the side they need no rotation at all, and the ramp still
              // runs top to bottom in severity order — which is the reading
              // direction anyway.
              horizontal
              valueLabel={t('credit.outstanding')}
              emptyMessage={t('common.noData')}
            />
          </Section>

          {/*
            Where the debt is, cut at whichever level the reader chose. One
            selector drives all three sections below and the hierarchy tab, so
            the matrix, the chart and the table can never be showing different
            levels at the same moment.
          */}
          <Section
            title={t('credit.agingByLevel', {
              level: t(FILTER_LABELS[level as FilterLevel]),
            })}
            actions={
              <label className="flex items-center gap-2 text-xs text-slate-500">
                {t('credit.groupBy')}
                <select
                  className="input tap-y py-1 text-xs"
                  value={level}
                  onChange={(event) => update({ level: event.target.value })}
                >
                  {EXPOSURE_LEVELS.map((option) => (
                    <option key={option} value={option}>
                      {t(FILTER_LABELS[option])}
                    </option>
                  ))}
                </select>
              </label>
            }
          >
            <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
              {t('credit.agingByLevelNote')}
            </p>
            <AgingMatrix
              matrix={summary.data?.aging_by_level}
              levelLabel={t(FILTER_LABELS[level as FilterLevel])}
              emptyMessage={t('common.noData')}
            />
          </Section>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Section title={t('credit.exposureByLevel')}>
              <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
                {t('credit.exposureNote')}
              </p>
              {/*
                Stacked rather than grouped, and that is a claim rather than a
                style: the two parts are mutually exclusive and add to the bar's
                total, so the height of each bar *is* that group's outstanding.
                Grouped, the reader would have to add two bars by eye to get the
                figure the ranking is actually ordered by.
              */}
              <ComparisonBarChart
                data={exposureRows}
                xKey="name"
                stacked
                series={[
                  {
                    key: 'overdue_amount',
                    label: t('credit.overdue'),
                    color: AGING_COLORS['121-180'],
                  },
                  {
                    key: 'not_overdue_amount',
                    label: t('credit.notOverdue'),
                    color: AGING_COLORS.NOT_YET_DUE,
                  },
                ]}
                emptyMessage={t('common.noData')}
              />
            </Section>

            <Section title={t('credit.dueProfile')}>
              <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
                {/*
                  The chart draws what is *not yet* late, so it has to say what
                  it is leaving out — otherwise these five bars read as the whole
                  book. The overdue total is named in the sentence rather than
                  drawn as a sixth bar: the same taka on two charts is taka a
                  reader will add.
                */}
                {t('credit.dueProfileNote', {
                  amount: formatAmount(summary.data?.due_profile?.overdue_amount),
                })}
              </p>
              <CategoryBarChart
                data={summary.data?.due_profile?.buckets ?? []}
                xKey="bucket"
                yKey="due_amount"
                valueLabel={t('credit.dueProfileValue')}
                emptyMessage={t('common.noData')}
              />
            </Section>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Section title={t('credit.statusSplit')}>
              <DonutChart
                data={summary.data?.status ?? []}
                xKey="status"
                yKey="invoice_count"
                emptyMessage={t('common.noData')}
              />
            </Section>
            <Section title={t('credit.topOverdue')}>
              <CategoryBarChart
                data={summary.data?.top_overdue_customers ?? []}
                xKey="customer_name"
                yKey="overdue_amount"
                horizontal
                valueLabel={t('credit.overdue')}
                emptyMessage={t('common.noData')}
              />
            </Section>
          </div>

          <Section title={t('credit.outstandingTrend')}>
            {/*
              Deliberately not a chart. The state is NOT_AVAILABLE and the reason
              is printed, because a trend line built on an assumed payment
              pattern is indistinguishable on screen from a measured one.
            */}
            <p className="text-sm text-slate-500 dark:text-slate-400">
              {trend?.reason ?? t('common.noData')}
            </p>
          </Section>

          <Section
            title={t(VIEW_LABEL[view])}
            actions={
              <div className="flex gap-1" role="tablist">
                {(['customers', 'invoices', 'hierarchy'] as const).map((option) => (
                  <button
                    key={option}
                    type="button"
                    role="tab"
                    aria-selected={view === option}
                    className={view === option ? 'btn-primary py-1 text-xs' : 'btn-ghost py-1 text-xs'}
                    onClick={() =>
                      update({ view: option, page: undefined, sort: undefined, q: undefined })
                    }
                  >
                    {t(VIEW_LABEL[option])}
                  </button>
                ))}
              </div>
            }
          >
            <QueryState
              isLoading={view === 'hierarchy' ? summary.isLoading : table.isLoading}
              error={view === 'hierarchy' ? summary.error : table.error}
              onRetry={() =>
                void (view === 'hierarchy' ? summary.refetch() : table.refetch())
              }
            >
              {view === 'hierarchy' ? (
                /*
                  Read off the bundle rather than fetched again: the breakdown is
                  already in the summary the cards and the matrix above are drawn
                  from, and a second request could answer differently if a load
                  landed between them — a table disagreeing with the chart
                  directly above it is the failure the single bundle exists to
                  prevent. It is a ranked top-N rather than a page, so it sorts
                  and arranges in the browser and takes no paging controls.
                */
                <DataTable<CreditExposureRow>
                  key="credit-control.hierarchy"
                  tableId="credit-control.hierarchy"
                  rows={summary.data?.exposure_by_level?.rows ?? []}
                  columns={hierarchyColumns}
                  // Drill-down: a group opens the customer list narrowed to it,
                  // which is the question a reader asks next — "who inside this
                  // region owes it?"
                  onRowClick={(row) =>
                    row.code
                      ? update({
                          view: 'customers',
                          q: row.code,
                          page: undefined,
                          sort: undefined,
                        })
                      : undefined
                  }
                  emptyMessage={t('common.noData')}
                />
              ) : view === 'invoices' ? (
                <DataTable<CreditInvoiceRow>
                  key="credit-control.invoices"
                  tableId="credit-control.invoices"
                  rows={(table.data?.rows as CreditInvoiceRow[]) ?? []}
                  columns={invoiceColumns}
                  serverMode
                  page={table.data?.page ?? 1}
                  pageSize={PAGE_SIZE}
                  total={table.data?.total ?? 0}
                  totalPages={table.data?.total_pages ?? 0}
                  onPageChange={(next) => update({ page: String(next) })}
                  search={search}
                  onSearchChange={(value) => update({ q: value, page: undefined })}
                  sortBy={sortBy}
                  sortDir={sortDir}
                  onSortChange={(column, direction) =>
                    update({ sort: column, dir: direction, page: undefined })
                  }
                  onRowClick={(row) =>
                    setOpenInvoice({ company: row.company_code, invoice: row.invoice_no })
                  }
                  emptyMessage={t('common.noData')}
                />
              ) : (
                <DataTable<CreditCustomerRow>
                  key="credit-control.customers"
                  tableId="credit-control.customers"
                  rows={(table.data?.rows as CreditCustomerRow[]) ?? []}
                  columns={customerColumns}
                  serverMode
                  page={table.data?.page ?? 1}
                  pageSize={PAGE_SIZE}
                  total={table.data?.total ?? 0}
                  totalPages={table.data?.total_pages ?? 0}
                  onPageChange={(next) => update({ page: String(next) })}
                  search={search}
                  onSearchChange={(value) => update({ q: value, page: undefined })}
                  sortBy={sortBy}
                  sortDir={sortDir}
                  onSortChange={(column, direction) =>
                    update({ sort: column, dir: direction, page: undefined })
                  }
                  // Drill-down: a customer row opens the invoice view already
                  // narrowed to that customer, which is the question a reader
                  // asks next — "which invoices make up that figure?"
                  onRowClick={(row) =>
                    update({
                      view: 'invoices',
                      q: row.customer_code,
                      page: undefined,
                      sort: undefined,
                    })
                  }
                  emptyMessage={t('common.noData')}
                />
              )}
            </QueryState>
          </Section>
        </div>
      </QueryState>

      <InvoiceDetailPanel
        companyCode={openInvoice?.company ?? null}
        invoiceNo={openInvoice?.invoice ?? null}
        query={query}
        onClose={() => setOpenInvoice(null)}
      />
    </>
  );
}
