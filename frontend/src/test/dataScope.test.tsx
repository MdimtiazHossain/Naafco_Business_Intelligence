/**
 * The data-scope form: two chains, and neither one losing the other.
 *
 * A scope used to be one level and its codes. There are two chains now — the
 * sales hierarchy, and `company -> plant -> storage location`, which is the
 * only one a stock position states — so the form has to express either, both,
 * or neither, and has to keep working against an API that has not been told
 * about the second one yet.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import DataScopeFields, {
  EMPTY_SCOPE,
  chosenLevel,
  dataScopeFrom,
  scopeDimensions,
  scopeStateFrom,
  type ScopeState,
} from '../components/admin/DataScopeFields';
import { I18nProvider } from '../contexts/I18nContext';

const ORG = {
  key: 'org',
  label: 'the sales hierarchy',
  levels: [
    { code_field: 'company_code', label: 'company' },
    { code_field: 'region_code', label: 'region' },
    { code_field: 'area_code', label: 'area' },
  ],
};

const PLANT = {
  key: 'plant',
  label: 'the plant hierarchy',
  levels: [{ code_field: 'plant_code', label: 'plant' }],
};

const DIMENSIONS = [ORG, PLANT];

function Harness({ initial = EMPTY_SCOPE }: { initial?: ScopeState }) {
  const [scope, setScope] = useState<ScopeState>(initial);
  return (
    <I18nProvider>
      <form>
        <DataScopeFields
          dimensions={DIMENSIONS}
          idPrefix="t"
          value={scope}
          onChange={setScope}
        />
        <output data-testid="body">{JSON.stringify(dataScopeFrom(DIMENSIONS, scope))}</output>
      </form>
    </I18nProvider>
  );
}

describe('the scope chains', () => {
  it('falls back to one unnamed chain when the API sends no dimensions', () => {
    // A bundle newer than the API it is talking to keeps the control it had,
    // rather than losing data scope entirely.
    const fallback = scopeDimensions({ scope_levels: ['region_code', 'area_code'] });
    expect(fallback).toHaveLength(1);
    expect(fallback[0].levels.map((l) => l.code_field)).toEqual([
      'region_code',
      'area_code',
    ]);
  });

  it('prefers the dimensions the API did send', () => {
    expect(scopeDimensions({ scope_levels: ['region_code'], scope_dimensions: DIMENSIONS }))
      .toEqual(DIMENSIONS);
  });

  it('reads the selected level off an existing scope without being told', () => {
    // The chain list arrives asynchronously, so the state cannot be grouped by
    // chain when it is built. The codes say which level is in use.
    const state = scopeStateFrom({ region_code: ['REG001', 'REG002'] });
    expect(state.codes.region_code).toBe('REG001, REG002');
    expect(chosenLevel(ORG, state)).toBe('region_code');
    expect(chosenLevel(PLANT, state)).toBe('');
  });

  it('an explicit choice of none beats the codes still sitting in state', () => {
    const state: ScopeState = {
      level: { org: '' },
      codes: { region_code: 'REG001' },
    };
    expect(chosenLevel(ORG, state)).toBe('');
    expect(dataScopeFrom(DIMENSIONS, state)).toEqual({});
  });
});

describe('the body it builds', () => {
  it('sends both chains when both are filled in', () => {
    const state: ScopeState = {
      level: {},
      codes: { region_code: 'REG001', plant_code: '1110, 1210' },
    };
    expect(dataScopeFrom(DIMENSIONS, state)).toEqual({
      region_code: ['REG001'],
      plant_code: ['1110', '1210'],
    });
  });

  it('sends only the chosen level of a chain, not every level typed into', () => {
    // Switching the picker keeps what was typed at the old level, so it can be
    // switched back without retyping — but only the chosen one is granted.
    const state: ScopeState = {
      level: { org: 'area_code' },
      codes: { region_code: 'REG001', area_code: 'AR001' },
    };
    expect(dataScopeFrom(DIMENSIONS, state)).toEqual({ area_code: ['AR001'] });
  });

  it('drops a level whose codes are blank rather than granting an empty scope', () => {
    const state: ScopeState = { level: { org: 'region_code' }, codes: { region_code: '  ,  ' } };
    expect(dataScopeFrom(DIMENSIONS, state)).toEqual({});
  });
});

describe('the form', () => {
  it('draws a picker for the sales chain and a plain field for the plant', () => {
    render(<Harness />);

    // Three levels to choose between, so a picker.
    expect(screen.getByLabelText('Sales hierarchy')).toBeInstanceOf(HTMLSelectElement);
    // One grantable level, so a picker would be a control with no decision in
    // it — the chain's name goes straight onto the codes field.
    expect(screen.getByLabelText('Plant (stock)')).toBeInstanceOf(HTMLInputElement);
  });

  it('grants a plant scope without touching the sales chain', () => {
    render(<Harness />);

    fireEvent.change(screen.getByLabelText('Plant (stock)'), { target: { value: '1110' } });
    expect(JSON.parse(screen.getByTestId('body').textContent ?? '{}')).toEqual({
      plant_code: ['1110'],
    });
  });

  it('grants both chains at once', () => {
    render(<Harness />);

    fireEvent.change(screen.getByLabelText('Sales hierarchy'), {
      target: { value: 'region_code' },
    });
    fireEvent.change(screen.getByLabelText('Scope codes'), { target: { value: 'REG001' } });
    fireEvent.change(screen.getByLabelText('Plant (stock)'), { target: { value: '1110' } });

    expect(JSON.parse(screen.getByTestId('body').textContent ?? '{}')).toEqual({
      region_code: ['REG001'],
      plant_code: ['1110'],
    });
  });

  it('shows an existing scope on the chain it belongs to', () => {
    render(<Harness initial={scopeStateFrom({ plant_code: ['1110', '1210'] })} />);

    expect(screen.getByLabelText('Plant (stock)')).toHaveValue('1110, 1210');
    expect(screen.getByLabelText('Sales hierarchy')).toHaveValue('');
  });

  it('will not take codes for the sales chain until a level is chosen', () => {
    // Codes with no level name nothing, and a field that silently discards what
    // is typed into it is worse than one that says it is not ready.
    render(<Harness />);
    expect(screen.getByLabelText('Scope codes')).toBeDisabled();
  });
});
