/**
 * One credit invoice, opened from the invoice list.
 *
 * A right-hand sheet rather than a centred dialog, because this is a detail view
 * of a row in the table behind it: somebody comparing three invoices reads the
 * list and the detail together, and a centred panel covers the row it came from.
 * Everything else about the dialog — Escape, the focus trap, the scroll lock —
 * is `Modal`'s, unchanged.
 *
 * **This component computes nothing.** Net, balance, days overdue, status and
 * aging bucket all arrive derived, resolved by the backend against the As On
 * date the page asked about. The Financial Summary below is laid out as an
 * arithmetic sequence because that is how a reader checks a balance, but every
 * figure in it is one the server sent.
 */

import { useQuery } from '@tanstack/react-query';
import { Modal } from './Modal';
import { QueryState } from './States';
import { useT } from '../contexts/I18nContext';
import { creditService, type CreditQuery } from '../services';
import { formatAmount, formatDate, statusClass } from '../utils/format';

interface Props {
  companyCode: string | null;
  invoiceNo: string | null;
  query: CreditQuery;
  onClose: () => void;
}

/** One label/value line. `value` is already formatted by the caller. */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="text-sm tabular-nums text-slate-900 dark:text-slate-50">{value}</dd>
    </div>
  );
}

/**
 * One line of the Financial Summary.
 *
 * `deduction` renders the figure with a leading minus so the sequence reads as
 * the subtraction it is, and `total` rules a line above the result. Neither
 * changes a number — the minus is punctuation on a positive figure the server
 * sent, not a negation applied here.
 */
function Money({
  label, value, deduction = false, total = false,
}: { label: string; value: number | null; deduction?: boolean; total?: boolean }) {
  return (
    <div
      className={`flex items-baseline justify-between gap-3 py-1 ${
        total ? 'mt-1 border-t border-slate-200 pt-2 dark:border-slate-800' : ''
      }`}
    >
      <span
        className={`text-xs ${
          total
            ? 'font-semibold text-slate-700 dark:text-slate-200'
            : 'text-slate-500 dark:text-slate-400'
        }`}
      >
        {label}
      </span>
      <span
        className={`tabular-nums ${
          total
            ? 'text-sm font-semibold text-slate-900 dark:text-slate-50'
            : 'text-sm text-slate-700 dark:text-slate-300'
        }`}
      >
        {deduction && value ? '− ' : ''}
        {formatAmount(value, false)}
      </span>
    </div>
  );
}

export function InvoiceDetailPanel({ companyCode, invoiceNo, query, onClose }: Props) {
  const t = useT();
  const open = Boolean(companyCode && invoiceNo);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['credit-invoice', companyCode, invoiceNo, query],
    queryFn: () => creditService.invoice(companyCode!, invoiceNo!, query),
    enabled: open,
  });

  const invoice = data?.invoice;

  return (
    <Modal
      open={open}
      placement="sheet"
      size="lg"
      title={invoiceNo ?? ''}
      description={
        invoice
          ? [invoice.customer_name ?? invoice.customer_code, invoice.plant_name]
              .filter(Boolean).join(' · ')
          : undefined
      }
      onClose={onClose}
      footer={
        <button type="button" className="btn-secondary" onClick={onClose}>
          {t('common.close')}
        </button>
      }
    >
      <QueryState isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {invoice && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge ${statusClass(invoice.credit_status)}`}>
                {t(`credit.status.${invoice.credit_status}`)}
              </span>
              {/*
                Only when it is actually late. "0 days overdue" on an invoice
                inside its terms reads as a countdown that has run out, which is
                the opposite of what it means.
              */}
              {invoice.days_overdue ? (
                <span className="text-xs text-red-600 dark:text-red-400">
                  {t('credit.overdueByDays', { count: invoice.days_overdue })}
                </span>
              ) : null}
              {data?.data_quality_flags.map((flag) => (
                <span
                  key={flag}
                  className="badge bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
                  title={t('credit.flagShownNotHidden')}
                >
                  {t(`credit.flag.${flag}`)}
                </span>
              ))}
            </div>

            <section>
              <h3 className="card-title mb-1">{t('credit.invoiceInformation')}</h3>
              <dl>
                <Field label={t('filters.company')} value={invoice.company_code} />
                <Field label={t('credit.plant')} value={invoice.plant_name ?? invoice.plant_code ?? '—'} />
                <Field label={t('credit.customer')} value={invoice.customer_name ?? '—'} />
                <Field label={t('credit.customerCode')} value={invoice.customer_code} />
                <Field label={t('credit.invoiceDate')} value={formatDate(invoice.invoice_date)} />
              </dl>
            </section>

            <section>
              <h3 className="card-title mb-1">{t('credit.creditTerms')}</h3>
              <dl>
                <Field
                  label={t('filters.creditDays')}
                  value={t('credit.days', { count: invoice.credit_days })}
                />
                <Field label={t('credit.dueDate')} value={formatDate(invoice.due_date)} />
                <Field label={t('filters.paymentMode')} value={invoice.payment_mode ?? '—'} />
                <Field
                  label={t('filters.agingBucket')}
                  /* `n/a` rather than a dash: a cleared invoice is not in an
                     aging bucket, and saying so beats an empty cell. */
                  value={
                    invoice.aging_bucket
                      ? t(`credit.bucket.${invoice.aging_bucket}`)
                      : t('common.notAvailable')
                  }
                />
              </dl>
            </section>

            <section>
              <h3 className="card-title mb-1">{t('credit.financialSummary')}</h3>
              <div>
                <Money label={t('credit.invoiceValue')} value={invoice.invoice_value} />
                <Money label={t('credit.returnAmount')} value={invoice.return_amount} deduction />
                <Money label={t('credit.netInvoice')} value={invoice.net_invoice_amount} total />
                <Money label={t('credit.payment')} value={invoice.payment_amount} deduction />
                <Money label={t('credit.discount')} value={invoice.discount_amount} deduction />
                <Money label={t('credit.adjustment')} value={invoice.adjustment_amount} deduction />
                <Money label={t('credit.balance')} value={invoice.balance_amount} total />
              </div>
            </section>

            <section>
              <h3 className="card-title mb-1">{t('credit.paymentClearing')}</h3>
              {data?.payment_events.length ? (
                <ol className="space-y-2">
                  {data.payment_events.map((event, index) => (
                    <li key={index} className="rounded-lg bg-slate-50 p-2 dark:bg-slate-900">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="text-sm text-slate-700 dark:text-slate-200">
                          {t(`credit.event.${event.kind}`)}
                        </span>
                        <span className="text-xs tabular-nums text-slate-500 dark:text-slate-400">
                          {/* An undated event says so rather than showing a
                              blank, because the money it names is real. */}
                          {event.date ? formatDate(event.date) : t('common.notAvailable')}
                        </span>
                      </div>
                      {event.amount !== null && (
                        <p className="text-sm tabular-nums text-slate-900 dark:text-slate-50">
                          {formatAmount(event.amount, false)}
                        </p>
                      )}
                      {event.note && (
                        <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                          {event.note}
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  {t('credit.noPaymentEvents')}
                </p>
              )}
              <dl className="mt-2">
                <Field
                  label={t('credit.clearingDate')}
                  value={invoice.clearing_date ? formatDate(invoice.clearing_date) : t('common.notAvailable')}
                />
                <Field
                  label={t('credit.clearingDocument')}
                  value={invoice.clearing_document ?? t('common.notAvailable')}
                />
              </dl>
            </section>
          </div>
        )}
      </QueryState>
    </Modal>
  );
}
