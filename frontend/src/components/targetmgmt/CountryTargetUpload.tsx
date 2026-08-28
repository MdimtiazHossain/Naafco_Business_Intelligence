/**
 * Loading a country target from a file instead of typing it.
 *
 * **Nothing is applied by choosing a file.** The upload is two steps — preview,
 * then apply — and the preview names every row it would change before anybody
 * commits to it. This is the figure a whole sales force is measured on; seeing
 * it first is the point.
 *
 * **A file with any bad row applies nothing, and every problem is listed.** Not
 * the first one: a file fixed one error at a time takes as many uploads as it
 * has mistakes. The Apply button is *absent* while anything is rejected, rather
 * than present and refusing.
 *
 * **The screen says what the upload will not touch.** Materials already on the
 * version and absent from the file keep their figures — this upload updates
 * what it names and removes nothing — and the count is shown so nobody has to
 * take that on trust.
 */

import { CheckCircle2, Download, Upload, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { Section } from '../PageHeader';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatQuantity } from '../../utils/format';
import type {
  TargetUploadPreview,
  TargetUploadResult,
  TargetUploadRow,
} from '../../types/api';

const STATUS_TONE: Record<string, string> = {
  NEW: 'text-blue-700 dark:text-blue-400',
  CHANGED: 'text-amber-700 dark:text-amber-400',
  UNCHANGED: 'text-slate-500 dark:text-slate-400',
  SKIPPED: 'text-slate-400 dark:text-slate-500',
  REJECTED: 'text-red-600 dark:text-red-400',
};

export function CountryTargetUpload({
  preview,
  result,
  editable,
  canUpload,
  busy,
  error,
  onTemplate,
  onFile,
  onApply,
  onDismiss,
}: {
  preview: TargetUploadPreview | null;
  result: TargetUploadResult | null;
  editable: boolean;
  canUpload: boolean;
  busy: boolean;
  error: string | null;
  onTemplate: () => void;
  onFile: (file: File) => void;
  onApply: () => void;
  onDismiss: () => void;
}) {
  const t = useT();
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);

  if (!canUpload) return null;

  const columns = [
    { key: 'row_number', header: t('targetMgmt.upload.row'), align: 'right' as const },
    { key: 'material_code', header: t('targetMgmt.col.material') },
    {
      key: 'current_volume',
      header: t('targetMgmt.upload.current'),
      align: 'right' as const,
      render: (row: TargetUploadRow) =>
        row.current_volume === null ? (
          <span
            className="cursor-help text-slate-400"
            title={t('targetMgmt.upload.noCurrent')}
          >
            —
          </span>
        ) : (
          formatQuantity(row.current_volume)
        ),
    },
    {
      key: 'volume',
      header: t('targetMgmt.upload.fromFile'),
      align: 'right' as const,
      render: (row: TargetUploadRow) => {
        if (row.volume !== null) return formatQuantity(row.volume);
        // A skipped row stated nothing; a rejected one stated something the
        // reader refused. Showing the refused text back is the useful half —
        // telling somebody `12,5OO` was rejected beats telling them "row 4 was".
        if (row.status === 'SKIPPED') {
          return (
            <span
              className="cursor-help text-slate-400"
              title={t('targetMgmt.upload.blankMeaning')}
            >
              —
            </span>
          );
        }
        return (
          <span className="font-mono text-xs text-red-600 dark:text-red-400">
            {row.raw_volume ?? '—'}
          </span>
        );
      },
    },
    {
      key: 'status',
      header: t('targetMgmt.col.status'),
      render: (row: TargetUploadRow) => (
        <span className={`text-xs ${STATUS_TONE[row.status] ?? ''}`}>
          {row.status.toLowerCase()}
        </span>
      ),
    },
    {
      key: 'error',
      header: t('targetMgmt.upload.problem'),
      render: (row: TargetUploadRow) =>
        row.error ? (
          <span className="text-red-600 dark:text-red-400">{row.error}</span>
        ) : (
          ''
        ),
    },
  ];

  return (
    <Section
      title={t('targetMgmt.upload.title')}
      className="mt-4"
      actions={
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onTemplate}
            className="flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            <Download className="h-3.5 w-3.5" />
            {t('targetMgmt.upload.template')}
          </button>
          {editable && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setOpen(true);
                input.current?.click();
              }}
              className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              <Upload className="h-3.5 w-3.5" />
              {t('targetMgmt.upload.choose')}
            </button>
          )}
        </div>
      }
    >
      <input
        ref={input}
        type="file"
        accept=".csv,.xlsx"
        aria-label={t('targetMgmt.upload.choose')}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onFile(file);
          // Cleared so choosing the same file twice fires again — a planner who
          // fixes the spreadsheet and re-picks it expects it to be read.
          event.target.value = '';
        }}
      />

      <p className="text-sm text-slate-500 dark:text-slate-400">
        {t('targetMgmt.upload.description')}
      </p>

      {error && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      {result && (
        <p className="mt-3 flex items-start gap-2 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {t('targetMgmt.upload.applied', {
              file: result.file_name,
              created: String(result.created),
              updated: String(result.updated),
              untouched: String(result.untouched),
            })}
          </span>
        </p>
      )}

      {preview && (
        <div className="mt-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-slate-700 dark:text-slate-300">
              {t('targetMgmt.upload.readCounts', {
                file: preview.file_name,
                read: String(preview.counts.read),
                created: String(preview.counts.new),
                changed: String(preview.counts.changed),
                rejected: String(preview.counts.rejected),
              })}
            </p>
            <button
              type="button"
              onClick={onDismiss}
              className="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
            >
              <X className="h-3.5 w-3.5" />
              {t('common.cancel')}
            </button>
          </div>

          <ul className="mb-3 space-y-1 text-sm text-slate-600 dark:text-slate-400">
            {preview.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>

          <DataTable
            tableId="target-management.upload-preview"
            columns={columns}
            rows={preview.rows}
            rowKey={(row) => String((row as TargetUploadRow).row_number)}
          />

          {preview.applicable && editable && (
            <button
              type="button"
              disabled={busy}
              onClick={onApply}
              className="mt-3 rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              {t('targetMgmt.upload.apply', {
                count: String(preview.counts.read - preview.counts.rejected),
              })}
            </button>
          )}
        </div>
      )}

      {open && !preview && !busy && !error && (
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
          {t('targetMgmt.upload.waiting')}
        </p>
      )}
    </Section>
  );
}
