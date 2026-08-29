/**
 * The Data Upload Center.
 *
 * One page, five tabs: Master Data, Transactional Data, Upload History, Failed
 * Records and Data Quality. The upload itself is a wizard that never lets the
 * user skip a step — a file is validated and previewed before the Import button
 * exists at all, and importing needs an explicit confirmation.
 *
 * The browser never loads a whole file: it posts the file and renders the first
 * 50 rows the backend returns, so a 200 000-row upload is as light on the client
 * as a 10-row one.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, ArrowLeft, CheckCircle2, ChevronRight, Download, FileSpreadsheet, RotateCcw, Upload, XCircle } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { UploadActivity } from '../components/UploadActivity';
import { UploadCompletionSummary, UploadProgress } from '../components/UploadProgress';
import { useAuth } from '../contexts/AuthContext';
import { useT } from '../contexts/I18nContext';
import { useUploadProgress } from '../hooks/useUploadProgress';
import { ApiError } from '../services/apiClient';
import { dataQualityService, dataUploadService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatDateTime, formatCount } from '../utils/format';
import type {
  ImportMode,
  UploadBatch,
  UploadOutcome,
  UploadType,
  UploadStatusValue,
} from '../types/api';

type Tab = 'master' | 'transactional' | 'history' | 'failed' | 'quality';

/**
 * Headers for the preview's own columns.
 *
 * `__`-prefixed keys are what the validator worked out about each line —
 * resolved identity codes, the volume it will store, the verdict — as opposed
 * to the columns that came out of the uploaded file, which keep their own
 * names.
 */
const PREVIEW_HEADERS: Record<string, string> = {
  __row: '#',
  __status: '',
  __company_code: 'Company',
  __invoice_no: 'Invoice',
  __invoice_line_no: 'Line',
  __customer_code: 'Customer',
  __material_code: 'Material',
  __batch_code: 'Batch',
  __quantity: 'Quantity',
  __volume: 'Total Volume',
  __reason: 'Reason',
};

function previewHeader(key: string): string {
  return PREVIEW_HEADERS[key] ?? key;
}

const STATUS_TONE: Record<UploadStatusValue, string> = {
  UPLOADED: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  QUEUED: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  VALIDATING: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  VALIDATED: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  IMPORTING: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  COMPLETED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  PARTIAL: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  FAILED: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  CANCELLED: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
  ROLLED_BACK: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
};

function StatusBadge({ status }: { status: UploadStatusValue }) {
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium ${
        STATUS_TONE[status] ?? STATUS_TONE.UPLOADED
      }`}
    >
      {status.replace(/_/g, ' ')}
    </span>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700">
      <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`text-lg font-semibold tabular-nums ${tone ?? ''}`}>
        {formatCount(value)}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The upload wizard
// ---------------------------------------------------------------------------

function UploadWizard({
  uploadType,
  onBack,
}: {
  uploadType: UploadType;
  onBack: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<ImportMode>(uploadType.default_mode);
  const [outcome, setOutcome] = useState<UploadOutcome | null>(null);
  const [imported, setImported] = useState<UploadOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  /** Something worth saying that is not a failure, so it is not shown in red. */
  const [notice, setNotice] = useState<string | null>(null);
  const progress = useUploadProgress();

  const validate = useMutation({
    mutationFn: async () => {
      setNotice(null);
      // The job token is minted here, one per run, so a retry after a failure
      // watches the new attempt rather than the stale one.
      const jobId = progress.start();
      // 202: the file is staged and a worker will read it. The counts, preview
      // and errors do not exist yet, so the mutation is not finished until the
      // job settles and the batch can be read back.
      const queued = await dataUploadService.preview(
        file as File,
        { upload_type: uploadType.key, import_mode: mode, job_id: jobId },
        { onUploadProgress: progress.onUploadProgress },
      );
      // The server's id, which is the only one it answers about.
      progress.adopt(queued.upload.job_id);
      return dataUploadService.awaitOutcome(queued);
    },
    onSuccess: (result) => {
      setOutcome(result);
      setImported(null);
      setError(null);
      progress.finish({
        // A cancelled run did not complete either. Reporting it as a completed
        // one would tell the operator their data landed when it was rolled back.
        failed:
          result.upload.status === 'FAILED' || result.upload.status === 'CANCELLED',
        message: result.upload.message,
        totals: result.upload.totals,
      });
    },
    onError: (caught) => {
      setError((caught as Error).message);
      progress.finish({ failed: true, message: (caught as Error).message });
    },
  });

  const commit = useMutation({
    // Also 202, and for the same reason: the import runs on a worker as one
    // transaction, so the inserted/updated counts only exist once it lands.
    mutationFn: async () => {
      setNotice(null);
      const queued = await dataUploadService.commit(
        outcome!.upload.upload_id,
        progress.start(),
      );
      progress.adopt(queued.upload.job_id);
      return dataUploadService.awaitOutcome(queued);
    },
    onSuccess: (result) => {
      setImported(result);
      setOutcome(null);
      setError(null);
      progress.finish({
        // A cancelled run did not complete either. Reporting it as a completed
        // one would tell the operator their data landed when it was rolled back.
        failed:
          result.upload.status === 'FAILED' || result.upload.status === 'CANCELLED',
        message: result.upload.message,
        totals: result.upload.totals,
      });
      void queryClient.invalidateQueries({ queryKey: ['upload-history'] });
      void queryClient.invalidateQueries({ queryKey: ['upload-summary'] });
    },
    onError: (caught) => {
      setError((caught as Error).message);
      progress.finish({ failed: true, message: (caught as Error).message });
    },
  });

  /**
   * Stop the run that is in flight.
   *
   * Nothing here settles the wizard: the cancel only raises the server's flag.
   * The batch then reaches CANCELLED, the `awaitOutcome` poll that the validate
   * or commit mutation is already sitting on sees it, and that mutation resolves
   * with the real stored outcome — so there is exactly one path by which a run
   * finishes, whether it was cancelled or not.
   */
  const cancel = useMutation({
    mutationFn: async () => {
      const jobId = progress.currentJobId();
      if (!jobId) return;
      try {
        await dataUploadService.cancelJob(jobId);
      } catch (caught) {
        // 409 means it finished before the cancel landed. The run's own result
        // is about to arrive and is the truth, so this is not an error to show.
        if (caught instanceof ApiError && caught.status === 409) {
          setNotice(t('upload.cancelAlreadyFinished'));
          return;
        }
        throw caught;
      }
    },
    onSettled: () => setConfirmCancel(false),
    onError: (caught) => setError((caught as Error).message),
  });

  // One run at a time. Both mutations drive the same file and the same batch, so
  // a second click while either is in flight would either upload the file twice
  // or commit a batch that is already being committed.
  const busy = validate.isPending || commit.isPending;

  const preview = outcome?.preview;
  const totals = outcome?.upload.totals;

  return (
    <div className="space-y-4">
      <button type="button" className="btn-ghost px-2 text-sm" onClick={onBack}>
        <ArrowLeft size={14} />
        {t('upload.backToTypes')}
      </button>

      <Section
        title={uploadType.label}
        actions={
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => void dataUploadService.downloadTemplate(uploadType.key, 'xlsx')}
            >
              <Download size={14} />
              {t('upload.downloadTemplate')}
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => void dataUploadService.downloadTemplate(uploadType.key, 'csv')}
            >
              CSV
            </button>
          </div>
        }
      >
        <p className="text-sm text-slate-600 dark:text-slate-300">
          {uploadType.description}
        </p>
        <p className="mt-2 text-xs text-slate-500">
          <strong>{t('upload.recordIdentity')}:</strong>{' '}
          {uploadType.business_key_description}
        </p>
        {uploadType.note && (
          <p className="mt-1 text-xs text-slate-500">{uploadType.note}</p>
        )}

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="upload-file">
              {t('upload.chooseFile')}
            </label>
            <input
              id="upload-file"
              ref={fileInput}
              type="file"
              accept=".xlsx,.csv"
              className="input"
              disabled={busy}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setOutcome(null);
                setImported(null);
                setError(null);
                setNotice(null);
                progress.reset();
              }}
            />
            <p className="mt-1 text-[11px] text-slate-400">{t('upload.fileHint')}</p>
          </div>
          <div>
            <label className="label" htmlFor="upload-mode">
              {t('upload.importMode')}
            </label>
            <select
              id="upload-mode"
              className="input"
              value={mode}
              disabled={busy}
              onChange={(event) => setMode(event.target.value as ImportMode)}
            >
              {uploadType.supported_modes.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <p className="mt-1 text-[11px] text-slate-400">{t('upload.modeHint')}</p>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary"
            disabled={!file || busy}
            onClick={() => validate.mutate()}
          >
            <Upload size={14} />
            {validate.isPending ? t('upload.validating') : t('upload.validate')}
          </button>

          {/* Only while something is actually running: a cancel button with no
              run behind it would be asking the server to stop nothing. */}
          {busy && (
            <button
              type="button"
              className="btn-secondary"
              disabled={cancel.isPending}
              onClick={() => setConfirmCancel(true)}
            >
              <XCircle size={14} />
              {cancel.isPending ? t('upload.cancelling') : t('upload.cancel')}
            </button>
          )}
        </div>

        {/* Shown from the first byte until the run settles, and left on screen
            afterwards so the final counts stay readable. */}
        {progress.state.started && (
          <div className="mt-4">
            <UploadProgress
              state={progress.state}
              title={`${
                commit.isPending || imported
                  ? t('upload.progress.importing')
                  : t('upload.progress.validating')
              } — ${uploadType.label}`}
            />
            <UploadCompletionSummary
              state={progress.state}
              imported={Boolean(imported)}
            />
          </div>
        )}

        {error && (
          <p className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </p>
        )}

        {notice && (
          <p className="mt-3 rounded bg-slate-50 px-3 py-2 text-sm text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {notice}
          </p>
        )}
      </Section>

      <ConfirmDialog
        open={confirmCancel}
        title={t('upload.cancelTitle')}
        consequence={t('upload.cancelConsequence')}
        // The file goes in the question rather than the record rows: those are
        // labelled "Record"/"Type" for master data, and an uploaded file is
        // neither.
        question={t('upload.cancelQuestion', { file: file?.name ?? '' })}
        confirmLabel={t('upload.cancelConfirm')}
        tone="warning"
        busy={cancel.isPending}
        onConfirm={() => cancel.mutate()}
        onCancel={() => setConfirmCancel(false)}
      />

      {/* --- Step 2: preview and errors ---------------------------------- */}
      {outcome && (
        <Section title={t('upload.preview')}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label={t('upload.rows')} value={totals?.total_rows ?? 0} />
            <Stat label={t('upload.columns')} value={totals?.columns ?? 0} />
            <Stat
              label={t('upload.validRows')}
              value={totals?.valid_rows ?? 0}
              tone="text-emerald-600"
            />
            <Stat
              label={t('upload.invalidRows')}
              value={totals?.invalid_rows ?? 0}
              tone={totals?.invalid_rows ? 'text-red-600' : undefined}
            />
            <Stat label={t('upload.duplicateRows')} value={totals?.duplicate_rows ?? 0} />
            <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700">
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                {t('common.status')}
              </p>
              <p className="mt-1">
                <StatusBadge status={outcome.upload.status} />
              </p>
            </div>
          </div>

          <p className="mt-3 text-xs text-slate-500">
            {outcome.upload.file_name} · {t('upload.previewNote')}
          </p>

          {outcome.warnings.map((warning) => (
            <p
              key={warning}
              className="mt-2 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
            >
              <AlertCircle size={12} className="mr-1 inline" />
              {warning}
            </p>
          ))}

          {preview && preview.rows.length > 0 && (
            <div className="mt-4">
              <DataTable
                // The preview's columns are the file's, so the arrangement follows the
                // dataset.
                tableId={`upload.preview.${uploadType.key}`}
                rows={preview.rows as Record<string, unknown>[]}
                columns={preview.columns.map((key) => ({
                  key,
                  header: previewHeader(key),
                  render:
                    key === '__status'
                      ? (row) =>
                          row.__status === 'INVALID' ? (
                            <XCircle size={14} className="text-red-500" />
                          ) : row.__status === 'DUPLICATE' ? (
                            <AlertCircle size={14} className="text-amber-500" />
                          ) : (
                            <CheckCircle2 size={14} className="text-emerald-500" />
                          )
                      : undefined,
                }))}
                pageSize={10}
                searchable={false}
                dense
                rowKey={(row, index) => String(row.__row ?? index)}
              />
            </div>
          )}

          {outcome.errors.length > 0 && (
            <div className="mt-4">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-red-700 dark:text-red-300">
                  {t('upload.errors')} ({formatCount(outcome.error_count)})
                </h3>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() =>
                    void dataUploadService.downloadErrorReport(outcome.upload.upload_id)
                  }
                >
                  <Download size={14} />
                  {t('upload.downloadErrors')}
                </button>
              </div>
              <DataTable
                tableId="upload.errors"
                rows={outcome.errors as unknown as Record<string, unknown>[]}
                columns={[
                  { key: 'row', header: t('upload.row'), width: '5rem' },
                  { key: 'column', header: t('upload.column') },
                  { key: 'value', header: t('upload.value') },
                  { key: 'error', header: t('upload.error') },
                  { key: 'suggested_fix', header: t('upload.suggestedFix') },
                ]}
                pageSize={10}
                dense
                rowKey={(row, index) => `${row.row}-${row.column}-${index}`}
              />
            </div>
          )}

          {/* --- Step 3: explicit confirmation --------------------------- */}
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-slate-200 pt-4 dark:border-slate-700">
            <button
              type="button"
              className="btn-primary"
              disabled={!outcome.upload.can_commit || busy}
              onClick={() => commit.mutate()}
            >
              {commit.isPending ? t('upload.importing') : t('upload.confirmImport')}
            </button>
            <p className="text-xs text-slate-500">
              {outcome.upload.can_commit
                ? t('upload.confirmHint', {
                    rows: formatCount(totals?.valid_rows ?? 0),
                  })
                : t('upload.cannotImport')}
            </p>
          </div>
        </Section>
      )}

      {/* --- Step 4: result ---------------------------------------------- */}
      {imported && (
        <Section title={t('upload.result')}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat
              label={t('upload.inserted')}
              value={imported.upload.totals.inserted_rows}
              tone="text-emerald-600"
            />
            <Stat label={t('upload.updated')} value={imported.upload.totals.updated_rows} />
            <Stat
              label={t('upload.failed')}
              value={imported.upload.totals.invalid_rows}
              tone={imported.upload.totals.invalid_rows ? 'text-red-600' : undefined}
            />
            <Stat
              label={t('upload.duplicateRows')}
              value={imported.upload.totals.duplicate_rows}
            />
          </div>
          <p className="mt-3 text-sm">
            <StatusBadge status={imported.upload.status} />{' '}
            <span className="text-slate-600 dark:text-slate-300">
              {imported.upload.message}
            </span>
          </p>
          {imported.error_count > 0 && (
            <button
              type="button"
              className="btn-secondary mt-3"
              onClick={() =>
                void dataUploadService.downloadErrorReport(imported.upload.upload_id)
              }
            >
              <Download size={14} />
              {t('upload.downloadErrors')}
            </button>
          )}
        </Section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Type pickers
// ---------------------------------------------------------------------------

/**
 * The order the groups are listed in. Order only, never membership.
 *
 * A group the server sends that is missing here is appended rather than
 * dropped: the upload screen is the only way data enters the warehouse, so a
 * heading must never be able to hide a dataset. `GROUP_BY_KEY` in
 * `upload/registry.py` is what decides what exists.
 */
const GROUP_ORDER = ['SALES', 'MARKET', 'PEOPLE', 'MATERIAL', 'TRANSACTIONS'];

/** The upload types of one tab, in collapsible groups. */
function TypeGroups({
  types,
  onSelect,
}: {
  types: UploadType[];
  onSelect: (type: UploadType) => void;
}) {
  const t = useT();
  // Collapsed by default, and any number may be open: a person loading a file
  // is looking for one heading, not walking a sequence.
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());

  const groups = useMemo(() => {
    const byGroup = new Map<string, UploadType[]>();
    for (const type of types) {
      const key = type.group ?? 'SALES';
      byGroup.set(key, [...(byGroup.get(key) ?? []), type]);
    }
    const known = GROUP_ORDER.filter((key) => byGroup.has(key));
    const extra = [...byGroup.keys()].filter((key) => !GROUP_ORDER.includes(key));
    return [...known, ...extra].map((key) => ({ key, types: byGroup.get(key) ?? [] }));
  }, [types]);

  if (types.length === 0) return <EmptyState />;

  return (
    <div className="space-y-2">
      {groups.map((group) => {
        const isOpen = open.has(group.key);
        const panelId = `upload-group-${group.key}`;
        return (
          <div key={group.key} className="rounded-lg border border-slate-200 dark:border-slate-700">
            <button
              type="button"
              aria-expanded={isOpen}
              aria-controls={panelId}
              onClick={() =>
                setOpen((previous) => {
                  const next = new Set(previous);
                  if (next.has(group.key)) next.delete(group.key);
                  else next.add(group.key);
                  return next;
                })
              }
              className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/60"
            >
              <ChevronRight
                size={15}
                aria-hidden="true"
                className={`shrink-0 text-slate-400 transition-transform ${isOpen ? 'rotate-90' : ''}`}
              />
              <span className="flex-1 text-sm font-semibold">
                {t(`dataManagement.group.${group.key}`)}
              </span>
              <span className="shrink-0 text-xs tabular-nums text-slate-400">
                {group.types.length}
              </span>
            </button>
            {isOpen && (
              <div id={panelId} className="border-t border-slate-200 p-3 dark:border-slate-700">
                <TypeGrid types={group.types} onSelect={onSelect} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function TypeGrid({
  types,
  onSelect,
}: {
  types: UploadType[];
  onSelect: (type: UploadType) => void;
}) {
  const t = useT();
  if (types.length === 0) return <EmptyState />;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {types.map((type) => (
        <button
          key={type.key}
          type="button"
          onClick={() => onSelect(type)}
          className="rounded-lg border border-slate-200 p-3 text-left transition-colors hover:border-brand-500 hover:bg-brand-50/40 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          <div className="flex items-start gap-2">
            <FileSpreadsheet size={18} className="mt-0.5 shrink-0 text-brand-600" />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{type.label}</p>
              <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">
                {type.description}
              </p>
              <p className="mt-1 text-[11px] text-slate-400">
                {type.column_count} {t('upload.columns').toLowerCase()} ·{' '}
                {type.required_columns.length} {t('upload.required')}
                {type.pending_source ? ` · ${t('upload.pendingSource')}` : ''}
              </p>
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function DataUploadPage({ initialTab }: { initialTab?: Tab } = {}) {
  const t = useT();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>(initialTab ?? 'master');
  const [selected, setSelected] = useState<UploadType | null>(null);
  const [openBatch, setOpenBatch] = useState<number | null>(null);

  const typesQuery = useQuery({
    queryKey: ['upload-types'],
    queryFn: dataUploadService.types,
  });
  const summaryQuery = useQuery({
    queryKey: ['upload-summary'],
    queryFn: dataUploadService.summary,
  });
  const historyQuery = useQuery({
    queryKey: ['upload-history'],
    queryFn: () => dataUploadService.history({ limit: 100 }),
    enabled: tab === 'history',
  });
  const failedQuery = useQuery({
    queryKey: ['upload-failed'],
    queryFn: () => dataUploadService.failedRecords({ limit: 200 }),
    enabled: tab === 'failed',
  });
  const qualityQuery = useQuery({
    queryKey: ['upload-quality'],
    queryFn: () => dataQualityService.overview(),
    enabled: tab === 'quality',
  });
  const batchQuery = useQuery({
    queryKey: ['upload-batch', openBatch],
    queryFn: () => dataUploadService.batch(openBatch as number),
    enabled: openBatch !== null,
  });

  const rollback = useMutation({
    mutationFn: (uploadId: number) => dataUploadService.rollback(uploadId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['upload-history'] });
      void queryClient.invalidateQueries({ queryKey: ['upload-batch'] });
    },
  });

  const byCategory = useMemo(() => {
    const groups = typesQuery.data?.categories ?? [];
    return {
      MASTER: groups.find((group) => group.key === 'MASTER')?.types ?? [],
      TRANSACTIONAL:
        groups.find((group) => group.key === 'TRANSACTIONAL')?.types ?? [],
    };
  }, [typesQuery.data]);

  const summary = summaryQuery.data;

  return (
    <>
      <PageHeader title={t('upload.title')} description={t('upload.subtitle')} />

      <div className="mb-4 grid grid-cols-1 gap-3 min-[360px]:grid-cols-2 lg:grid-cols-5">
        <Stat label={t('upload.totalUploads')} value={summary?.total_uploads ?? 0} />
        <Stat label={t('upload.completed')} value={summary?.completed_imports ?? 0} />
        <Stat
          label={t('upload.pending')}
          value={summary?.pending_imports ?? 0}
          tone="text-sky-600"
        />
        <Stat
          label={t('upload.failedImports')}
          value={summary?.failed_imports ?? 0}
          tone={summary?.failed_imports ? 'text-red-600' : undefined}
        />
        <Stat label={t('upload.today')} value={summary?.uploads_last_24h ?? 0} />
      </div>

      {/*
        Above the tabs, so a run in flight is the first thing seen on returning
        to this page. It reads the server's job list rather than any state this
        component kept, which is what lets an import the user navigated away
        from — or refreshed the browser on — still be here when they come back.
      */}
      <UploadActivity />

      {/*
        The Upload Centre has no GlobalFilterBar; this tab strip is its control
        bar, so it is what stays in view while a long history or failed-record
        list scrolls. A direct child of the page root, so it has the whole page
        to travel through — see GlobalFilterBar on why a wrapper would break it.
      */}
      <div className="card sticky top-[var(--app-header-height,3.5rem)] z-30 mb-4 flex flex-wrap gap-1 p-2">
        {(
          [
            ['master', t('upload.masterData')],
            ['transactional', t('upload.transactionalData')],
            ['history', t('upload.history')],
            ['failed', t('upload.failedRecords')],
            ['quality', t('upload.dataQuality')],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              setTab(key);
              setSelected(null);
              setOpenBatch(null);
            }}
            className={`tap-y rounded-lg px-3 py-1.5 text-xs font-medium ${
              tab === key
                ? 'bg-brand-600 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {(tab === 'master' || tab === 'transactional') &&
        (selected ? (
          <UploadWizard uploadType={selected} onBack={() => setSelected(null)} />
        ) : (
          <Section
            title={tab === 'master' ? t('upload.masterData') : t('upload.transactionalData')}
          >
            <QueryState
              isLoading={typesQuery.isLoading}
              error={typesQuery.error}
              onRetry={() => void typesQuery.refetch()}
              skeleton={<CardSkeleton rows={4} />}
            >
              <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
                {tab === 'master'
                  ? typesQuery.data?.categories.find((c) => c.key === 'MASTER')
                      ?.description
                  : typesQuery.data?.categories.find((c) => c.key === 'TRANSACTIONAL')
                      ?.description}
              </p>
              <TypeGroups
                types={tab === 'master' ? byCategory.MASTER : byCategory.TRANSACTIONAL}
                onSelect={setSelected}
              />
            </QueryState>
          </Section>
        ))}

      {tab === 'history' && (
        <Section title={t('upload.history')}>
          <QueryState
            isLoading={historyQuery.isLoading}
            error={historyQuery.error}
            onRetry={() => void historyQuery.refetch()}
            skeleton={<CardSkeleton rows={6} />}
          >
            <DataTable
              tableId="upload.history"
              rows={(historyQuery.data?.batches ?? []) as unknown as Record<string, any>[]}
              columns={[
                { key: 'upload_id', header: t('upload.batchId'), width: '5rem' },
                {
                  key: 'started_at',
                  header: t('upload.date'),
                  render: (row) => formatDateTime(row.started_at),
                },
                { key: 'username', header: t('upload.user') },
                { key: 'upload_type', header: t('upload.type') },
                { key: 'file_name', header: t('upload.file') },
                {
                  key: 'rows',
                  header: t('upload.rows'),
                  align: 'right',
                  render: (row) => formatCount(row.totals?.total_rows ?? 0),
                },
                {
                  key: 'success',
                  header: t('upload.success'),
                  align: 'right',
                  render: (row) => formatCount(row.totals?.valid_rows ?? 0),
                },
                {
                  key: 'failed',
                  header: t('upload.failed'),
                  align: 'right',
                  render: (row) => formatCount(row.totals?.invalid_rows ?? 0),
                },
                {
                  key: 'status',
                  header: t('common.status'),
                  render: (row) => <StatusBadge status={row.status} />,
                },
              ]}
              pageSize={20}
              onRowClick={(row) => setOpenBatch(Number(row.upload_id))}
              rowKey={(row) => String(row.upload_id)}
            />
          </QueryState>

          {openBatch !== null && (
            <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-700">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {t('upload.batch')} #{openBatch}
                </h3>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => void dataUploadService.downloadErrorReport(openBatch)}
                  >
                    <Download size={14} />
                    {t('upload.downloadErrors')}
                  </button>
                  {batchQuery.data?.upload.can_rollback &&
                    user?.role === 'SUPER_ADMIN' && (
                      <button
                        type="button"
                        className="btn-secondary text-red-600"
                        disabled={rollback.isPending}
                        onClick={() => {
                          if (window.confirm(t('upload.rollbackConfirm'))) {
                            rollback.mutate(openBatch);
                          }
                        }}
                      >
                        <RotateCcw size={14} />
                        {t('upload.rollback')}
                      </button>
                    )}
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => setOpenBatch(null)}
                  >
                    {t('common.close')}
                  </button>
                </div>
              </div>
              <QueryState
                isLoading={batchQuery.isLoading}
                error={batchQuery.error}
                onRetry={() => void batchQuery.refetch()}
                isEmpty={batchQuery.data?.errors.length === 0}
                emptyMessage={t('upload.noErrors')}
              >
                <DataTable
                  tableId="upload.batch-errors"
                  rows={(batchQuery.data?.errors ?? []) as unknown as Record<string, any>[]}
                  columns={[
                    { key: 'row', header: t('upload.row'), width: '5rem' },
                    { key: 'column', header: t('upload.column') },
                    { key: 'value', header: t('upload.value') },
                    { key: 'error', header: t('upload.error') },
                    { key: 'suggested_fix', header: t('upload.suggestedFix') },
                  ]}
                  pageSize={15}
                  dense
                  rowKey={(row, index) => `${row.row}-${index}`}
                />
              </QueryState>
            </div>
          )}
        </Section>
      )}

      {tab === 'failed' && (
        <Section title={t('upload.failedRecords')}>
          <QueryState
            isLoading={failedQuery.isLoading}
            error={failedQuery.error}
            onRetry={() => void failedQuery.refetch()}
            isEmpty={failedQuery.data?.records.length === 0}
            emptyMessage={t('upload.noFailedRecords')}
            skeleton={<CardSkeleton rows={6} />}
          >
            <DataTable
              tableId="upload.failed"
              rows={(failedQuery.data?.records ?? []) as unknown as Record<string, any>[]}
              columns={[
                { key: 'upload_id', header: t('upload.batchId'), width: '5rem' },
                { key: 'upload_type', header: t('upload.type') },
                { key: 'file_name', header: t('upload.file') },
                { key: 'row', header: t('upload.row'), width: '5rem' },
                { key: 'column', header: t('upload.column') },
                { key: 'value', header: t('upload.value') },
                { key: 'error', header: t('upload.error') },
                { key: 'suggested_fix', header: t('upload.suggestedFix') },
              ]}
              pageSize={20}
              dense
              rowKey={(row, index) => `${row.upload_id}-${row.row}-${index}`}
            />
          </QueryState>
        </Section>
      )}

      {tab === 'quality' && (
        <Section title={t('upload.dataQuality')}>
          <QueryState
            isLoading={qualityQuery.isLoading}
            error={qualityQuery.error}
            onRetry={() => void qualityQuery.refetch()}
            skeleton={<CardSkeleton rows={4} />}
          >
            <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
              {t('upload.qualityNote')}
            </p>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {Object.entries(qualityQuery.data?.totals ?? {}).map(([key, value]) => (
                <Stat key={key} label={key.replace(/_/g, ' ')} value={Number(value)} />
              ))}
            </div>
            {Object.keys(qualityQuery.data?.by_category ?? {}).length > 0 && (
              <>
                <h3 className="mb-2 mt-4 text-sm font-semibold">
                  {t('upload.rejectionsByCategory')}
                </h3>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  {Object.entries(qualityQuery.data?.by_category ?? {}).map(
                    ([key, value]) => (
                      <Stat
                        key={key}
                        label={key.replace(/_/g, ' ')}
                        value={Number(value)}
                        tone="text-red-600"
                      />
                    ),
                  )}
                </div>
              </>
            )}
          </QueryState>
        </Section>
      )}
    </>
  );
}

export type { UploadBatch };
