/**
 * Country Target: the one figure in this module a person types.
 *
 * Target Volume is the primary input and is styled as such — every other number
 * on the row is derived from it and from the Material Master, and is drawn as
 * read-only supporting detail so nobody mistakes a calculated cell for one they
 * can correct here.
 *
 * **A derived cell with a missing input reads `n/a`, never a number.** The
 * backend returns `null` for a quantity whose material states no Conversion
 * Factor, and `missing` names which input was absent — so the cell can say
 * *why* rather than just showing a dash, and point at the column of the Material
 * Master upload that would fix it.
 *
 * The country total goes further: it is suppressed entirely unless every line
 * derived. A total over a mixture of derivable and non-derivable lines is short
 * by an unknown amount, and a plausible wrong figure is worse than an honest
 * `n/a`. The backend decides that and explains it in `notes`; this component
 * shows what it was told rather than re-deriving the rule.
 *
 * Edits are held locally until Save, so a grid of several hundred materials is
 * one request and one transaction — either every line is written or none is.
 */

import { Plus, Save, Undo2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatAmount, formatQuantity } from '../../utils/format';
import type {
  TargetAvailableMaterial,
  TargetCountryLine,
  TargetCountryTotals,
} from '../../types/api';

/**
 * A derived figure that could not be computed.
 *
 * `n/a` rather than the `—` the shared formatters emit for an absent value: the
 * project already distinguishes the two, and this is the second kind — a ratio
 * whose divisor is missing, not a blank. The tooltip names the input to load.
 */
function NotAvailable({ missing }: { missing: TargetCountryLine['missing'] }) {
  const t = useT();
  const reason = missing.includes('conversion_factor')
    ? t('targetMgmt.noConversionFactor')
    : t('targetMgmt.noTransferPrice');
  return (
    <span
      className="cursor-help text-slate-400 dark:text-slate-500"
      title={reason}
    >
      n/a
    </span>
  );
}

export interface CountryTargetGridProps {
  lines: TargetCountryLine[];
  totals: TargetCountryTotals;
  notes: string[];
  editable: boolean;
  canEdit: boolean;
  available: TargetAvailableMaterial[];
  saving: boolean;
  onSave: (lines: { material_code: string; target_volume: string }[]) => void;
}

export function CountryTargetGrid({
  lines,
  totals,
  notes,
  editable,
  canEdit,
  available,
  saving,
  onSave,
}: CountryTargetGridProps) {
  const t = useT();
  /**
   * Volumes being edited, keyed by material code, as **text**.
   *
   * Text rather than numbers all the way to the backend: `Number('12,5OO')` is
   * `NaN` and `parseFloat` reads it as `12`, so parsing here would mean the
   * browser deciding what an unreadable cell means. It refuses it by name
   * instead, exactly as a bulk upload of the same value would be refused.
   */
  const [draft, setDraft] = useState<Record<string, string>>({});
  /** Materials added in this session but not yet saved. */
  const [added, setAdded] = useState<TargetAvailableMaterial[]>([]);
  const [picking, setPicking] = useState('');

  // A save (or a version change) replaces the server's rows, so the local edits
  // that produced them are spent. Keyed on the rows themselves rather than a
  // save counter: a refetch for any reason is a new starting point.
  useEffect(() => {
    setDraft({});
    setAdded([]);
  }, [lines]);

  const rows: TargetCountryLine[] = [
    ...lines,
    ...added.map((material) => ({
      material_code: material.material_code,
      material_description: material.material_description,
      material_brand: material.material_brand,
      material_group_name: material.material_group_name,
      company_code: null,
      conversion_factor: material.conversion_factor,
      transfer_price: material.transfer_price,
      target_volume: 0,
      quantity: null,
      value: null,
      missing: [
        ...(material.conversion_factor === null
          ? (['conversion_factor'] as const)
          : []),
        ...(material.transfer_price === null ? (['transfer_price'] as const) : []),
      ],
    })),
  ];

  const dirty = Object.keys(draft).length > 0 || added.length > 0;
  const writable = editable && canEdit;

  /**
   * What a row's volume currently reads.
   *
   * The draft wins where one exists — including an empty string, which is a
   * reader midway through typing and must not snap back to the stored value
   * under their cursor.
   */
  const volumeOf = (row: TargetCountryLine) =>
    draft[row.material_code] ?? String(row.target_volume);

  const columns = [
    { key: 'material_brand', header: t('targetMgmt.col.brand') },
    { key: 'material_code', header: t('targetMgmt.col.material') },
    { key: 'material_description', header: t('targetMgmt.col.description') },
    {
      key: 'target_volume',
      header: t('targetMgmt.col.targetVolume'),
      align: 'right' as const,
      render: (row: TargetCountryLine) =>
        writable ? (
          <input
            className="w-28 rounded-md border border-brand-300 bg-white px-2 py-1 text-right text-sm font-semibold tabular-nums text-brand-700 dark:border-brand-800 dark:bg-slate-900 dark:text-brand-300"
            value={volumeOf(row)}
            aria-label={`${t('targetMgmt.col.targetVolume')} ${row.material_code}`}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                [row.material_code]: event.target.value,
              }))
            }
          />
        ) : (
          <span className="font-semibold tabular-nums">
            {formatQuantity(row.target_volume)}
          </span>
        ),
    },
    {
      key: 'conversion_factor',
      header: t('targetMgmt.col.conversionFactor'),
      align: 'right' as const,
      render: (row: TargetCountryLine) =>
        row.conversion_factor === null ? (
          <NotAvailable missing={['conversion_factor']} />
        ) : (
          <span className="tabular-nums text-slate-500">{row.conversion_factor}</span>
        ),
    },
    {
      key: 'transfer_price',
      header: t('targetMgmt.col.transferPrice'),
      align: 'right' as const,
      render: (row: TargetCountryLine) =>
        row.transfer_price === null ? (
          <NotAvailable missing={['transfer_price']} />
        ) : (
          <span className="tabular-nums text-slate-500">{row.transfer_price}</span>
        ),
    },
    {
      key: 'quantity',
      header: t('targetMgmt.col.calculatedQuantity'),
      align: 'right' as const,
      render: (row: TargetCountryLine) =>
        row.quantity === null ? (
          <NotAvailable missing={row.missing} />
        ) : (
          <span className="tabular-nums text-slate-500">
            {formatQuantity(row.quantity)}
          </span>
        ),
    },
    {
      key: 'value',
      header: t('targetMgmt.col.calculatedValue'),
      align: 'right' as const,
      render: (row: TargetCountryLine) =>
        row.value === null ? (
          <NotAvailable missing={row.missing} />
        ) : (
          <span className="tabular-nums text-slate-500">{formatAmount(row.value)}</span>
        ),
    },
    {
      key: 'material_group_name',
      header: t('targetMgmt.col.materialGroup'),
      hidden: true,
    },
  ];

  const save = () => {
    // Only what changed, plus anything added. Re-sending an untouched grid
    // would be several hundred no-op writes, and the backend audits per changed
    // line — a save should not read as a revision of every number on the page.
    const payload = [
      ...Object.entries(draft).map(([material_code, target_volume]) => ({
        material_code,
        target_volume,
      })),
      ...added
        .filter((material) => !(material.material_code in draft))
        .map((material) => ({
          material_code: material.material_code,
          target_volume: '0',
        })),
    ];
    if (payload.length) onSave(payload);
  };

  return (
    <>
      <Section
        title={t('targetMgmt.countryTitle')}
        className="mt-4"
        actions={
          <span className="text-xs text-slate-400 dark:text-slate-500">
            {t('targetMgmt.countryRowCount', { count: String(rows.length) })}
          </span>
        }
      >
        <div className="mb-3 rounded-lg border border-brand-200 bg-brand-50/50 px-3 py-2 text-xs text-brand-800 dark:border-brand-900 dark:bg-brand-950/30 dark:text-brand-200">
          {t('targetMgmt.countryHelp')}
        </div>

        {!writable && (
          <div className="mb-3 rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {editable ? t('targetMgmt.noEditAction') : t('targetMgmt.versionFrozen')}
          </div>
        )}

        {rows.length === 0 ? (
          <EmptyState message={t('targetMgmt.noCountryLines')} />
        ) : (
          <DataTable
            tableId="target-management.country"
            rows={rows}
            columns={columns}
            rowKey={(row) => (row as TargetCountryLine).material_code}
            searchable
          />
        )}

        <CountryTotals totals={totals} notes={notes} />

        {writable && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-4 dark:border-slate-800">
            <div className="flex items-center gap-2">
              <select
                className="input w-72"
                value={picking}
                aria-label={t('targetMgmt.addMaterial')}
                onChange={(event) => setPicking(event.target.value)}
              >
                <option value="">{t('targetMgmt.addMaterial')}</option>
                {available
                  .filter(
                    (material) =>
                      !added.some(
                        (row) => row.material_code === material.material_code,
                      ),
                  )
                  .map((material) => (
                    <option
                      key={material.material_code}
                      value={material.material_code}
                    >
                      {material.material_code} · {material.material_description}
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="btn-secondary"
                disabled={!picking}
                onClick={() => {
                  const material = available.find(
                    (row) => row.material_code === picking,
                  );
                  if (material) setAdded((current) => [...current, material]);
                  setPicking('');
                }}
              >
                <Plus size={14} />
                {t('common.add')}
              </button>
            </div>
            <div className="flex items-center gap-2">
              {dirty && (
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    setDraft({});
                    setAdded([]);
                  }}
                >
                  <Undo2 size={14} />
                  {t('common.cancel')}
                </button>
              )}
              <button
                type="button"
                className="btn-primary"
                disabled={!dirty || saving}
                onClick={save}
              >
                <Save size={14} />
                {t('targetMgmt.saveCountry')}
              </button>
            </div>
          </div>
        )}
      </Section>
    </>
  );
}

/**
 * The country total, and why part of it may be missing.
 *
 * Beside the table rather than inside it: `DataTable` draws every report table
 * in this app and has no footer row, and forking a second table implementation
 * to gain one would cost every page the column arrangement it gives for free.
 */
function CountryTotals({
  totals,
  notes,
}: {
  totals: TargetCountryTotals;
  notes: string[];
}) {
  const t = useT();
  return (
    <div className="mt-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <TotalCard
          label={t('targetMgmt.totalVolume')}
          value={formatQuantity(totals.target_volume)}
          hint={t('targetMgmt.totalVolumeHint')}
          emphasis
        />
        <TotalCard
          label={t('targetMgmt.totalQuantity')}
          value={totals.quantity === null ? 'n/a' : formatQuantity(totals.quantity)}
          hint={
            totals.quantity === null
              ? t('targetMgmt.totalSuppressed', {
                  derivable: String(totals.derivable_count),
                  count: String(totals.line_count),
                })
              : t('targetMgmt.totalDerived')
          }
          muted={totals.quantity === null}
        />
        <TotalCard
          label={t('targetMgmt.totalValue')}
          value={totals.value === null ? 'n/a' : formatAmount(totals.value)}
          hint={
            totals.value === null
              ? t('targetMgmt.totalSuppressed', {
                  derivable: String(totals.derivable_count),
                  count: String(totals.line_count),
                })
              : t('targetMgmt.totalDerived')
          }
          muted={totals.value === null}
        />
      </div>
      {notes.length > 0 && (
        <ul className="mt-3 space-y-1">
          {notes.map((note) => (
            <li
              key={note}
              className="text-xs italic text-slate-500 dark:text-slate-400"
            >
              {note}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TotalCard({
  label,
  value,
  hint,
  emphasis = false,
  muted = false,
}: {
  label: string;
  value: string;
  hint: string;
  emphasis?: boolean;
  muted?: boolean;
}) {
  return (
    <div
      className={`card p-4 ${
        emphasis ? 'border-brand-200 dark:border-brand-900' : ''
      }`}
    >
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p
        className={`mt-2 text-2xl font-semibold tabular-nums ${
          muted
            ? 'text-slate-400 dark:text-slate-500'
            : emphasis
              ? 'text-brand-700 dark:text-brand-300'
              : 'text-slate-900 dark:text-slate-50'
        }`}
      >
        {value}
      </p>
      <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">{hint}</p>
    </div>
  );
}
