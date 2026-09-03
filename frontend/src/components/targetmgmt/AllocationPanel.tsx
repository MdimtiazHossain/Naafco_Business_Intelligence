/**
 * Allocation: starting a run, watching it, and reading what it produced.
 *
 * **The screen's most important job is the case where nothing happens.** With no
 * sales history the engine refuses to allocate, and this panel has to make that
 * legible as *waiting for data* rather than as a failure or, worse, as a
 * completed allocation of zeros. So a `NO_HISTORY` run gets its own presentation
 * — the years checked, the rows found, and which factors could not be
 * calculated — and never the word "Completed".
 *
 * The stage list is drawn from the job's own `stages`, so a stage added to the
 * engine appears here with no change in the browser. Progress is polled from
 * the job row, which is why it survives a page reload: an allocation's expensive
 * half holds no write lock, so its progress can be durable where an upload's
 * cannot.
 *
 * Nothing here computes a figure. Reconciliation, the totals and the warnings
 * all come from the backend, which reads them back out of the stored rows —
 * a browser-side sum would be a second opinion about numbers that must have
 * exactly one.
 */

import {
  AlertTriangle,
  Check,
  CircleDashed,
  Info,
  Loader2,
  Play,
  Scale,
} from 'lucide-react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import { formatQuantity } from '../../utils/format';
import type {
  TargetAllocationJob,
  TargetAllocationState,
  TargetFactorCatalogue,
  TargetReconciliation,
} from '../../types/api';

/** The job's result, non-null — a finished run always carries one. */
type AllocationResult = NonNullable<TargetAllocationJob['result']>;

/** A decimal string from the backend, rendered for reading. */
function decimal(value: string | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const parsed = Number(value);
  return Number.isNaN(parsed) ? value : formatQuantity(parsed);
}

export interface AllocationPanelProps {
  state: TargetAllocationState;
  catalogue?: TargetFactorCatalogue;
  job: TargetAllocationJob | null;
  canEdit: boolean;
  starting: boolean;
  onStart: () => void;
}

export function AllocationPanel({
  state,
  catalogue,
  job,
  canEdit,
  starting,
  onStart,
}: AllocationPanelProps) {
  const t = useT();
  const running = job?.status === 'QUEUED' || job?.status === 'PROCESSING';
  const noHistory = job?.status === 'NO_HISTORY';

  return (
    <div className="mt-4 space-y-4">
      <Section
        title={t('targetMgmt.allocationTitle')}
        actions={
          canEdit &&
          state.editable && (
            <button
              type="button"
              className="btn-primary"
              disabled={starting || running}
              onClick={onStart}
            >
              {running ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              {running ? t('targetMgmt.allocating') : t('targetMgmt.generate')}
            </button>
          )
        }
      >
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          {t('targetMgmt.allocationHelp')}
        </p>

        {!job && (
          <p className="rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {t('targetMgmt.neverAllocated')}
          </p>
        )}

        {job && noHistory && <NoHistory job={job} />}
        {job && !noHistory && <JobProgress job={job} />}
      </Section>

      {job && !noHistory && job.status === 'COMPLETED' && (
        <ReconciliationBar reconciliation={state.reconciliation} job={job} />
      )}

      {job?.result?.factors && (
        <FactorSummary factors={job.result.factors} catalogue={catalogue} />
      )}

      {job?.result?.warnings && job.result.warnings.length > 0 && (
        <Section title={t('targetMgmt.warnings')}>
          <ul className="space-y-1">
            {job.result.warnings.map((warning) => (
              <li
                key={warning}
                className="flex gap-2 text-xs text-amber-700 dark:text-amber-400"
              >
                <AlertTriangle size={14} className="mt-px shrink-0" />
                {warning}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

/**
 * A run in flight, or the one that finished.
 *
 * The stage list comes from the job, not from a copy here: a stage added to the
 * engine appears with no change in the browser.
 */
function JobProgress({ job }: { job: TargetAllocationJob }) {
  const t = useT();
  const stages = job.stages ?? [];
  const currentIndex = stages.findIndex((stage) => stage.key === job.current_stage);
  const failed = job.status === 'FAILED';

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
          {t(`targetMgmt.jobStatus.${job.status}`)}
          {job.current_stage_label && !failed && ` — ${job.current_stage_label}`}
        </span>
        <span className="text-lg font-semibold tabular-nums text-brand-700 dark:text-brand-300">
          {job.progress_percent}%
        </span>
      </div>

      <div className="h-2.5 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
        <div
          className={`h-full rounded-full transition-all ${
            failed ? 'bg-red-500' : 'bg-brand-600'
          }`}
          style={{ width: `${job.progress_percent}%` }}
          role="progressbar"
          aria-valuenow={job.progress_percent}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>

      <ol className="mt-3 space-y-1.5">
        {stages.map((stage, index) => {
          const done =
            job.status === 'COMPLETED' ||
            (currentIndex >= 0 && index < currentIndex);
          const active = !failed && index === currentIndex && job.status !== 'COMPLETED';
          return (
            <li
              key={stage.key}
              className={`flex items-center gap-2 text-xs ${
                done
                  ? 'text-emerald-700 dark:text-emerald-400'
                  : active
                    ? 'font-medium text-brand-700 dark:text-brand-300'
                    : 'text-slate-400 dark:text-slate-500'
              }`}
            >
              {done ? (
                <Check size={14} />
              ) : active ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <CircleDashed size={14} />
              )}
              {stage.label}
            </li>
          );
        })}
      </ol>

      {failed && job.error_message && (
        <p
          role="alert"
          className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300"
        >
          {job.error_message}
        </p>
      )}

      {job.status === 'COMPLETED' && <RunCounts job={job} />}
    </div>
  );
}

function RunCounts({ job }: { job: TargetAllocationJob }) {
  const t = useT();
  const result: AllocationResult = job.result ?? ({} as AllocationResult);
  // Row count is deliberately absent: the reconciliation bar below carries it,
  // beside the volumes it has to be read against. The same figure twice on one
  // screen invites a reader to wonder which one is authoritative.
  const cells: [string, string][] = [
    [t('targetMgmt.customerCount'), formatQuantity(result.customer_count ?? 0)],
    [t('targetMgmt.materialCount'), formatQuantity(result.material_count ?? 0)],
    [t('targetMgmt.monthCount'), formatQuantity(result.months?.length ?? 0)],
    [t('targetMgmt.nodeCount'), formatQuantity(result.node_count ?? 0)],
  ];
  /*
   * What the run came to, beside what it was over. Two of these carry a
   * tooltip because the honest number invites a question: rows saved always
   * equals rows generated (the write stores every row or fails), and
   * duplicates rejected is always zero (the unique key makes one impossible) —
   * and "zero" is a different claim from "we did not look".
   */
  const summary = job.summary;
  const outcome: [string, string, string | undefined][] = summary
    ? [
        [t('targetMgmt.summary.generated'),
         formatQuantity(summary.rows_generated), undefined],
        [t('targetMgmt.summary.saved'),
         formatQuantity(summary.rows_saved),
         t('targetMgmt.summary.savedHint')],
        [t('targetMgmt.summary.duplicates'),
         formatQuantity(summary.duplicates_rejected),
         t('targetMgmt.summary.duplicatesHint')],
        [t('targetMgmt.summary.failures'),
         formatQuantity(summary.validation_failures), undefined],
        [t('targetMgmt.summary.time'),
         summary.processing_seconds === null
           ? 'n/a'
           : t('targetMgmt.summary.seconds', {
               seconds: String(summary.processing_seconds),
             }),
         undefined],
      ]
    : [];

  return (
    <>
      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cells.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
            <dd className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-50">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      {outcome.length > 0 && (
        <>
          <h4 className="mt-4 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            {t('targetMgmt.summary.title')}
          </h4>
          <dl className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-5">
            {outcome.map(([label, value, hint]) => (
              <div key={label}>
                <dt
                  className={`text-xs text-slate-500 dark:text-slate-400${hint ? ' cursor-help' : ''}`}
                  title={hint}
                >
                  {label}
                </dt>
                <dd className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-50">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </>
  );
}

/**
 * The state every deployment starts in, and the one this panel exists for.
 *
 * Deliberately not styled as an error. The engine did the right thing; it is
 * waiting for transactional data, and a planner should close this understanding
 * that rather than filing a bug.
 */
function NoHistory({ job }: { job: TargetAllocationJob }) {
  const t = useT();
  const result: AllocationResult = job.result ?? ({} as AllocationResult);
  const rows: [string, string][] = [
    [t('targetMgmt.salesRowsFound'), String(result.sales_rows_found ?? 0)],
    [
      t('targetMgmt.periodChecked'),
      (result.basis_years ?? []).join(' · ') || '—',
    ],
    [t('targetMgmt.allocationStatus'), t('targetMgmt.notAllocated')],
  ];

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-4 dark:border-amber-900 dark:bg-amber-950/20">
      <div className="flex items-center gap-2">
        <Info size={18} className="text-amber-700 dark:text-amber-400" />
        <h3 className="text-sm font-semibold text-amber-900 dark:text-amber-200">
          {t('targetMgmt.noHistoryTitle')}
        </h3>
      </div>
      <dl className="mt-3 grid gap-2 sm:grid-cols-3">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-amber-800/70 dark:text-amber-300/70">
              {label}
            </dt>
            <dd className="text-sm font-medium text-amber-900 dark:text-amber-200">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-3 text-xs text-amber-900/80 dark:text-amber-200/80">
        {job.error_message ?? t('targetMgmt.noHistoryReason')}
      </p>
    </div>
  );
}

/**
 * Balanced, or not, and by how much.
 *
 * Prominent when balanced because that is the gate everything downstream
 * depends on — final approval is blocked while a mismatch exists, so a planner
 * needs to see at a glance which side of it they are on.
 */
function ReconciliationBar({
  reconciliation,
  job,
}: {
  reconciliation: TargetReconciliation;
  job: TargetAllocationJob;
}) {
  const t = useT();
  const balanced = reconciliation.balanced;
  const remaining = Number(reconciliation.country_target_volume) -
    Number(reconciliation.allocated_volume);

  return (
    <div
      className={`rounded-xl border p-4 ${
        balanced
          ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/20'
          : 'border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/20'
      }`}
    >
      <div className="flex flex-wrap items-center gap-3">
        {balanced ? (
          <Scale size={20} className="text-emerald-700 dark:text-emerald-400" />
        ) : (
          <AlertTriangle size={20} className="text-red-600 dark:text-red-400" />
        )}
        <span
          className={`text-sm font-bold uppercase tracking-wide ${
            balanced
              ? 'text-emerald-800 dark:text-emerald-300'
              : 'text-red-700 dark:text-red-300'
          }`}
        >
          {balanced ? t('targetMgmt.balanced') : t('targetMgmt.mismatch')}
        </span>
        <span className="ml-auto text-xs text-slate-600 dark:text-slate-400">
          {t('targetMgmt.nodesChecked', {
            count: String(reconciliation.node_count),
          })}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure
          label={t('targetMgmt.totalVolume')}
          value={decimal(reconciliation.country_target_volume)}
        />
        <Figure
          label={t('targetMgmt.allocatedVolume')}
          value={decimal(reconciliation.allocated_volume)}
        />
        <Figure
          label={t('targetMgmt.remainingVolume')}
          value={formatQuantity(remaining)}
          tone={remaining === 0 ? 'good' : 'bad'}
        />
        <Figure
          label={t('targetMgmt.rowsGenerated')}
          value={formatQuantity(job.rows_processed)}
        />
      </dl>

      {!balanced && reconciliation.mismatches.length > 0 && (
        <ul className="mt-3 space-y-1">
          {reconciliation.mismatches.slice(0, 5).map((mismatch, index) => (
            <li
              key={`${mismatch.kind}-${index}`}
              className="text-xs text-red-700 dark:text-red-300"
            >
              {mismatch.kind} · {mismatch.node_code ?? '—'} ·{' '}
              {t('targetMgmt.expectedVsActual', {
                expected: mismatch.expected,
                actual: mismatch.actual,
              })}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Figure({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: 'good' | 'bad';
}) {
  return (
    <div>
      <dt className="text-xs text-slate-600 dark:text-slate-400">{label}</dt>
      <dd
        className={`text-base font-semibold tabular-nums ${
          tone === 'bad'
            ? 'text-red-700 dark:text-red-300'
            : tone === 'good'
              ? 'text-emerald-700 dark:text-emerald-300'
              : 'text-slate-900 dark:text-slate-50'
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * Which factors drove this run, and which could not be calculated.
 *
 * The second half is the point. Two of the eight have no data source in this
 * platform at all, and a factor that silently contributed nothing would leave a
 * planner unable to explain the split.
 */
function FactorSummary({
  factors,
  catalogue,
}: {
  factors: NonNullable<TargetAllocationJob['result']>['factors'];
  catalogue?: TargetFactorCatalogue;
}) {
  const t = useT();
  const described = new Map(
    (catalogue?.factors ?? []).map((factor) => [factor.key, factor]),
  );
  return (
    <Section title={t('targetMgmt.factorsTitle')}>
      <ul className="space-y-2">
        {(factors ?? []).map((factor) => {
          const active = factor.enabled && factor.available;
          return (
            <li key={factor.key} className="flex items-start gap-2 text-sm">
              <span
                className={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold ${
                  active
                    ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300'
                    : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                }`}
              >
                {active ? '✓' : '—'}
              </span>
              <span className="min-w-0">
                <span
                  className={
                    active
                      ? 'font-medium text-slate-800 dark:text-slate-100'
                      : 'text-slate-500 dark:text-slate-400'
                  }
                >
                  {factor.label}
                </span>
                {active && (
                  <span className="ml-2 text-xs tabular-nums text-slate-500 dark:text-slate-400">
                    {factor.weight}
                  </span>
                )}
                {!factor.available && factor.reason && (
                  <span className="mt-0.5 block text-xs italic text-slate-500 dark:text-slate-400">
                    {factor.reason}
                  </span>
                )}
                {factor.available && !factor.enabled && (
                  <span className="mt-0.5 block text-xs italic text-slate-400 dark:text-slate-500">
                    {described.get(factor.key)?.description ??
                      t('targetMgmt.factorOff')}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}
