/**
 * Export buttons.
 *
 * The rows sent here came from the backend and were already validated there;
 * the frontend never recomputes a figure for an export. The report name, the
 * period and the active filters travel with the request so the exported file
 * documents what it measured.
 */

import { Download, FileSpreadsheet, FileText, Table2 } from 'lucide-react';
import { useState } from 'react';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { exportService } from '../services';
import type { DateRange } from '../types/api';

export interface ExportButtonsProps {
  reportName: string;
  rows: Record<string, unknown>[];
  period?: DateRange | null;
  /** Already-formatted KPI pairs shown at the top of the PDF. */
  kpis?: [string, string][];
  disabled?: boolean;
  compact?: boolean;
}

export function ExportButtons({
  reportName,
  rows,
  period,
  kpis,
  disabled,
  compact = false,
}: ExportButtonsProps) {
  const t = useT();
  const { filters } = useFilters();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(format: 'xlsx' | 'csv' | 'pdf') {
    setBusy(format);
    setError(null);
    try {
      await exportService.download({
        format,
        title: reportName,
        report_name: reportName,
        date_range: period ? `${period.date_from} – ${period.date_to}` : undefined,
        filters: filters as Record<string, unknown>,
        rows,
        kpis: kpis?.map(([label, value]) => [label, value]),
      });
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const isDisabled = disabled || rows.length === 0;

  return (
    <div className="flex items-center gap-1.5">
      <button
        type="button"
        className="btn-secondary"
        onClick={() => run('xlsx')}
        disabled={isDisabled || busy !== null}
        title={t('common.exportExcel')}
      >
        {busy === 'xlsx' ? <Download size={14} className="animate-pulse" /> : <FileSpreadsheet size={14} />}
        {!compact && <span className="hidden sm:inline">Excel</span>}
      </button>
      <button
        type="button"
        className="btn-secondary"
        onClick={() => run('csv')}
        disabled={isDisabled || busy !== null}
        title={t('common.exportCsv')}
      >
        {busy === 'csv' ? <Download size={14} className="animate-pulse" /> : <Table2 size={14} />}
        {!compact && <span className="hidden sm:inline">CSV</span>}
      </button>
      <button
        type="button"
        className="btn-secondary"
        onClick={() => run('pdf')}
        disabled={isDisabled || busy !== null}
        title={t('common.exportPdf')}
      >
        {busy === 'pdf' ? <Download size={14} className="animate-pulse" /> : <FileText size={14} />}
        {!compact && <span className="hidden sm:inline">PDF</span>}
      </button>
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </div>
  );
}
