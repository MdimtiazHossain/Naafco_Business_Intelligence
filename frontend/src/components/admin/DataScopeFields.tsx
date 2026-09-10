/**
 * The data-scope controls, one row per scope chain.
 *
 * A data scope used to be one level and its codes, because there was one chain
 * to name a level in. There are two now — the sales hierarchy, and
 * `company -> plant -> storage location`, which is the only chain a stock
 * position states — so an account can be scoped in either or in both, and a
 * single row could express only one of them.
 *
 * **The chains come from the server** (`/api/admin/roles` -> `scope_dimensions`),
 * so this file carries no list of which level is a plant and a third chain
 * would draw itself. When an older API answers without them, the levels it does
 * send are drawn as one unnamed chain, which is exactly the form this screen
 * had before — a bundle newer than the API it is talking to keeps working
 * rather than losing the control entirely.
 *
 * Codes are held **per level** rather than per chain, so switching the level
 * picker away and back does not lose what was typed. Only the chosen level of
 * each chain is submitted.
 */
import { FILTER_LABELS } from '../../contexts/FilterContext';
import { useT } from '../../contexts/I18nContext';
import type { FilterLevel, ScopeDimension } from '../../types/api';

export interface ScopeState {
  /** Chain key -> the level chosen in it. Absent means "work it out from the codes". */
  level: Record<string, string>;
  /** Level -> its comma-separated codes. Kept across level changes on purpose. */
  codes: Record<string, string>;
}

export const EMPTY_SCOPE: ScopeState = { level: {}, codes: {} };

/** The scope of a user being edited, as this component's state. */
export function scopeStateFrom(scope: Record<string, string[]> | undefined): ScopeState {
  return {
    level: {},
    codes: Object.fromEntries(
      Object.entries(scope ?? {}).map(([level, codes]) => [level, codes.join(', ')]),
    ),
  };
}

/**
 * The chains to draw. Falls back to one unnamed chain over whatever flat list
 * of levels the API sent, which is what an older server answers with.
 */
export function scopeDimensions(
  data: { scope_dimensions?: ScopeDimension[]; scope_levels?: string[] } | undefined,
): ScopeDimension[] {
  if (data?.scope_dimensions?.length) return data.scope_dimensions;
  return [
    {
      key: 'scope',
      label: '',
      levels: (data?.scope_levels ?? []).map((code_field) => ({ code_field, label: code_field })),
    },
  ];
}

/**
 * Which level of this chain is selected.
 *
 * Falls back to the first level of the chain the user already has codes for, so
 * editing an existing account shows their scope without this component having
 * to wait for the chain list before it can set up its state.
 */
export function chosenLevel(dimension: ScopeDimension, state: ScopeState): string {
  const explicit = state.level[dimension.key];
  if (explicit !== undefined) return explicit;
  return dimension.levels.find((level) => state.codes[level.code_field])?.code_field ?? '';
}

/** The `data_scope` body to send. Only the chosen level of each chain. */
export function dataScopeFrom(
  dimensions: ScopeDimension[],
  state: ScopeState,
): Record<string, string[]> {
  const scope: Record<string, string[]> = {};
  for (const dimension of dimensions) {
    const level = chosenLevel(dimension, state);
    if (!level) continue;
    const codes = (state.codes[level] ?? '')
      .split(',')
      .map((code) => code.trim())
      .filter(Boolean);
    if (codes.length) scope[level] = codes;
  }
  return scope;
}

function levelLabel(t: (key: string) => string, code_field: string, fallback: string): string {
  const key = FILTER_LABELS[code_field as FilterLevel];
  return key ? t(key) : fallback;
}

export default function DataScopeFields({
  dimensions,
  idPrefix,
  value,
  onChange,
}: {
  dimensions: ScopeDimension[];
  idPrefix: string;
  value: ScopeState;
  onChange: (next: ScopeState) => void;
}) {
  const t = useT();

  return (
    <>
      {dimensions.map((dimension) => {
        const level = chosenLevel(dimension, value);
        const single = dimension.levels.length === 1;
        const levelId = `${idPrefix}-scope-level-${dimension.key}`;
        const codesId = `${idPrefix}-scope-codes-${dimension.key}`;
        const chainLabel = t(`admin.scopeChain.${dimension.key}`);
        const heading =
          chainLabel === `admin.scopeChain.${dimension.key}`
            ? dimension.label || t('admin.dataScope')
            : chainLabel;

        // A chain with one grantable level gets one field, not two. A picker
        // offering a single option is a control with no decision in it, and a
        // heading reading "Plant" over a line reading "Plant" says it twice.
        const codes = (
          <div>
            <label className="label" htmlFor={codesId}>
              {single ? heading : t('admin.scopeCodes')}
            </label>
            <input
              id={codesId}
              className="input"
              placeholder={single ? '1110, 1210' : 'REG001, REG002'}
              value={(level && value.codes[level]) || ''}
              disabled={!single && !level}
              onChange={(event) => {
                const target = level || (single ? dimension.levels[0].code_field : '');
                if (!target) return;
                onChange({
                  ...value,
                  codes: { ...value.codes, [target]: event.target.value },
                });
              }}
            />
          </div>
        );

        if (single) return <div key={dimension.key}>{codes}</div>;

        return (
          <div key={dimension.key} className="contents">
            <div>
              <label className="label" htmlFor={levelId}>
                {heading}
              </label>
              <select
                id={levelId}
                className="input"
                value={level}
                onChange={(event) =>
                  onChange({
                    ...value,
                    level: { ...value.level, [dimension.key]: event.target.value },
                  })
                }
              >
                <option value="">{t('common.none')}</option>
                {dimension.levels.map((entry) => (
                  <option key={entry.code_field} value={entry.code_field}>
                    {levelLabel(t, entry.code_field, entry.label)}
                  </option>
                ))}
              </select>
            </div>
            {codes}
          </div>
        );
      })}
    </>
  );
}
