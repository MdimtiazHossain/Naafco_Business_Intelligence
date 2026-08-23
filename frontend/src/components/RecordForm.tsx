/**
 * The create / edit form.
 *
 * Built from the entity's own field list, so a column added to a dimension
 * appears here with its label, its type and its required flag already correct —
 * there is no per-entity form to keep in step.
 *
 * Two rules shape it:
 *
 * **Only what changed is sent.** The API applies a partial update, so posting
 * every field would turn "I edited the phone number" into an edit of forty
 * fields in the history, and would overwrite anything a colleague changed in
 * between with the values this form loaded.
 *
 * **Server errors land on their fields.** The backend returns row/column/reason
 * per problem; matching them back to inputs by label is what makes a rejected
 * save actionable instead of a red banner at the top of a long form.
 */

import { useQuery } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { useMemo, useState, type FormEvent } from 'react';
import { useT } from '../contexts/I18nContext';
import { ApiError, masterDataService } from '../services';
import { Modal } from './Modal';
import type { FieldIssue, ManagedEntity, ManagedField, ManagedRow } from '../types/api';

/**
 * The filter level that lists a referenced table's codes.
 *
 * `/api/master-data/options/{level}` already serves permission-scoped,
 * searchable code lists for every hierarchy level — the cascading filter bar
 * runs on it. Reusing it means the dropdown a form offers can never contain a
 * code the user is not allowed to use.
 */
const OPTION_LEVEL_BY_TABLE: Record<string, string> = {
  dim_company: 'company_code',
  dim_business_unit: 'bu_code',
  dim_sales_line: 'sales_line_code',
  dim_zone: 'zone_code',
  dim_region: 'region_code',
  dim_area: 'area_code',
  dim_unit: 'unit_code',
  dim_territory: 'territory_code',
  dim_sub_territory: 'sub_territory_code',
};

export interface RecordFormProps {
  open: boolean;
  entity: ManagedEntity;
  /** Absent when creating. */
  record?: ManagedRow | null;
  busy?: boolean;
  error?: unknown;
  onSubmit: (values: Record<string, unknown>, reason: string) => void;
  onCancel: () => void;
}

function initialValue(field: ManagedField, record?: ManagedRow | null): string {
  const raw = record?.[field.name];
  if (raw === null || raw === undefined) return '';
  return String(raw);
}

/**
 * A code field backed by its master.
 *
 * A free-text input with a `datalist` rather than a `<select>`: the referenced
 * master can hold thousands of codes, which a select renders as an unusable
 * wall, and the browser's own datalist filters as the user types without a
 * custom autocomplete to build and get wrong. Typing a code the list does not
 * contain is still allowed here — the backend refuses it with a message naming
 * the code, which is a better place to be strict than a control that silently
 * refuses to accept a keystroke.
 */
function LookupInput({
  id,
  field,
  value,
  invalid,
  onChange,
}: {
  id: string;
  field: ManagedField;
  value: string;
  invalid: boolean;
  onChange: (value: string) => void;
}) {
  const t = useT();
  const level = field.references
    ? OPTION_LEVEL_BY_TABLE[field.references]
    : undefined;

  const options = useQuery({
    queryKey: ['lookup-options', level],
    queryFn: () => masterDataService.options(level as string),
    enabled: Boolean(level),
    staleTime: 5 * 60 * 1000,
  });

  return (
    <>
      <input
        id={id}
        className="input"
        type="text"
        list={`${id}-options`}
        value={value}
        autoComplete="off"
        aria-invalid={invalid || undefined}
        aria-describedby={invalid ? `${id}-error` : undefined}
        placeholder={t('record.lookupPlaceholder')}
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id={`${id}-options`}>
        {(options.data?.options ?? []).map((option) => (
          <option key={option.code} value={option.code}>
            {option.label}
          </option>
        ))}
      </datalist>
    </>
  );
}

/** Pull the per-field problems out of a 422, whatever else it carries. */
export function fieldIssues(error: unknown): FieldIssue[] {
  if (!(error instanceof ApiError)) return [];
  const detail = (error.detail ?? {}) as { errors?: FieldIssue[] };
  return Array.isArray(detail.errors) ? detail.errors : [];
}

export function RecordForm({
  open,
  entity,
  record,
  busy = false,
  error,
  onSubmit,
  onCancel,
}: RecordFormProps) {
  const t = useT();
  const creating = !record;

  // Creating offers the key fields; editing does not, because the business code
  // is the record's identity on every fact that references it.
  const fields = useMemo(
    () => entity.fields.filter((field) => (creating ? true : field.editable)),
    [entity.fields, creating],
  );

  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, initialValue(f, record)])),
  );
  const [reason, setReason] = useState('');

  const issues = fieldIssues(error);
  const issueByLabel = new Map(issues.map((issue) => [issue.column, issue]));
  const generalError =
    error && issues.length === 0
      ? (error as ApiError).message ?? t('common.error')
      : null;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const changed: Record<string, unknown> = {};
    for (const field of fields) {
      const next = values[field.name] ?? '';
      const before = initialValue(field, record);
      if (creating ? next !== '' : next !== before) {
        changed[field.name] = next === '' ? null : next;
      }
    }
    onSubmit(changed, reason.trim());
  }

  return (
    <Modal
      open={open}
      size="lg"
      title={
        creating
          ? t('record.createTitle', { entity: entity.label })
          : t('record.editTitle', { entity: entity.label })
      }
      description={entity.description}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            form="record-form"
            className="btn-primary"
            disabled={busy}
          >
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t('common.save')}
          </button>
        </>
      }
    >
      <form id="record-form" onSubmit={handleSubmit} className="space-y-3" noValidate>
        {generalError && (
          <p role="alert" className="rounded border-l-4 border-l-red-500 bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">
            {generalError}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          {fields.map((field) => {
            const issue = issueByLabel.get(field.label);
            const inputId = `field-${field.name}`;
            return (
              <div key={field.name} className={field.kind === 'text' && field.name.includes('address') ? 'sm:col-span-2' : ''}>
                <label htmlFor={inputId} className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
                  {field.label}
                  {field.required && <span className="ml-0.5 text-red-500">*</span>}
                  {creating && field.is_key && (
                    <span className="ml-1 text-[10px] font-normal text-slate-400">
                      {t('record.keyHint')}
                    </span>
                  )}
                </label>

                {field.choices.length > 0 ? (
                  <select
                    id={inputId}
                    className="input"
                    value={values[field.name] ?? ''}
                    onChange={(event) =>
                      setValues((previous) => ({
                        ...previous,
                        [field.name]: event.target.value,
                      }))
                    }
                  >
                    <option value="">—</option>
                    {field.choices.map((choice) => (
                      <option key={choice} value={choice}>
                        {choice}
                      </option>
                    ))}
                  </select>
                ) : field.references ? (
                  <LookupInput
                    id={inputId}
                    field={field}
                    value={values[field.name] ?? ''}
                    invalid={Boolean(issue)}
                    onChange={(next) =>
                      setValues((previous) => ({ ...previous, [field.name]: next }))
                    }
                  />
                ) : (
                  <input
                    id={inputId}
                    className="input"
                    // `text` even for numbers: the backend does the coercion and
                    // reports a precise reason, while a browser number input
                    // silently discards what it cannot parse.
                    type="text"
                    inputMode={
                      field.kind === 'decimal' || field.kind === 'integer' ||
                      field.kind === 'numeric'
                        ? 'decimal'
                        : undefined
                    }
                    value={values[field.name] ?? ''}
                    aria-invalid={issue ? true : undefined}
                    aria-describedby={issue ? `${inputId}-error` : undefined}
                    onChange={(event) =>
                      setValues((previous) => ({
                        ...previous,
                        [field.name]: event.target.value,
                      }))
                    }
                  />
                )}

                {issue ? (
                  <p id={`${inputId}-error`} className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                    {issue.error}
                    {issue.suggested_fix ? ` ${issue.suggested_fix}` : ''}
                  </p>
                ) : (
                  field.description && (
                    <p className="mt-1 text-[11px] text-slate-400">{field.description}</p>
                  )
                )}
              </div>
            );
          })}
        </div>

        {!creating && (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('record.reason')}
            </span>
            <input
              className="input"
              value={reason}
              maxLength={500}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t('record.reasonPlaceholder')}
            />
          </label>
        )}
      </form>
    </Modal>
  );
}
