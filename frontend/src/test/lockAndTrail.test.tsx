/**
 * The lock control and the audit trail.
 *
 * **What the lock will do is stated before it is pressed.** It is the one act
 * in this module that reaches outside it, so the panel names its conditions and
 * — afterwards — the batch it wrote under, rather than reporting a bare success.
 *
 * **Blockers are listed one per line.** A missing transfer price, an unbalanced
 * allocation and a branch stopping above territory are fixed in three different
 * places; one merged sentence would send a reader to the wrong one.
 *
 * **The button is absent, not disabled, when the reader cannot lock.** A control
 * whose only outcome is a refusal teaches people to ignore controls.
 *
 * **The trail draws old and new side by side.** They are what a reader
 * compares, and "changed to X" would lose the half saying what was there before.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AuditTrail } from '../components/targetmgmt/AuditTrail';
import { LockPanel } from '../components/targetmgmt/LockPanel';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetAuditEntry,
  TargetAuditResponse,
  TargetLockState,
} from '../types/api';

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider>
      <ThemeProvider>{node}</ThemeProvider>
    </I18nProvider>,
  );
}

function lockState(overrides: Partial<TargetLockState> = {}): TargetLockState {
  return {
    lockable: true,
    blockers: [],
    locked_batch_id: null,
    locked_at: null,
    locks_role: 'MANAGEMENT',
    ...overrides,
  };
}

function showLock(
  state = lockState(),
  canLock = true,
  onLock = vi.fn(),
) {
  wrap(
    <LockPanel
      state={state}
      canLock={canLock}
      onLock={onLock}
      busy={false}
      error={null}
    />,
  );
  return onLock;
}

describe('LockPanel', () => {
  it('offers the lock when nothing is blocking it', () => {
    showLock();
    expect(screen.getByText('Lock target')).toBeTruthy();
  });

  it('lists every blocker separately', () => {
    showLock(
      lockState({
        lockable: false,
        blockers: [
          'No Transfer Price for SKU001 — a locked target states an amount.',
          'Reconciliation does not balance at 2 nodes.',
        ],
      }),
    );
    expect(screen.getByText(/No Transfer Price for SKU001/)).toBeTruthy();
    expect(
      screen.getByText('Reconciliation does not balance at 2 nodes.'),
    ).toBeTruthy();
    expect(screen.queryByText('Lock target')).toBeNull();
  });

  it('omits the button entirely for a reader who cannot lock, and says who can', () => {
    showLock(lockState(), false);
    expect(screen.queryByText('Lock target')).toBeNull();
    expect(screen.getByText(/held by Management/)).toBeTruthy();
  });

  it('passes the note through when locking', () => {
    const onLock = showLock();
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Agreed at the September planning meeting.' },
    });
    fireEvent.click(screen.getByText('Lock target'));
    expect(onLock).toHaveBeenCalledWith(
      'Agreed at the September planning meeting.',
    );
  });

  it('reports the batch once locked, and offers no second lock', () => {
    showLock(
      lockState({
        lockable: false,
        locked_at: '2026-09-01T10:00:00Z',
        locked_batch_id: '26a0afd0-0c11-4f2e-9a55-2b1d4e6f7c88',
      }),
    );
    expect(screen.getByText(/26a0afd0/)).toBeTruthy();
    expect(screen.getByText(/never edited/)).toBeTruthy();
    expect(screen.queryByText('Lock target')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AuditTrail
// ---------------------------------------------------------------------------

function entry(overrides: Partial<TargetAuditEntry> = {}): TargetAuditEntry {
  return {
    audit_id: 1,
    plan_id: 1,
    version_id: 9,
    action: 'TARGET_LOCKED',
    actor: 'ceo',
    actor_role: 'MANAGEMENT',
    node_label: 'TP-2026-001 · V1',
    old_value: 'APPROVED',
    new_value: 'LOCKED · batch 26a0afd0',
    reason: 'Agreed at the September planning meeting.',
    occurred_at: '2026-09-01T10:00:00Z',
    ...overrides,
  };
}

function trail(overrides: Partial<TargetAuditResponse> = {}): TargetAuditResponse {
  return {
    rows: [
      entry(),
      entry({
        audit_id: 2, action: 'REVISION_APPROVED', actor: 'ceo',
        node_label: 'Customer CUST-001', old_value: '13,333',
        new_value: '20,000', reason: 'Two new outlets opened.',
      }),
      entry({
        audit_id: 3, action: 'PLAN_CREATED', actor: 'ceo',
        old_value: null, new_value: 'TP-2026-001', reason: null,
      }),
    ],
    total: 11,
    actions: ['PLAN_CREATED', 'TARGET_LOCKED', 'REVISION_APPROVED'],
    ...overrides,
  };
}

function showTrail(
  data = trail(),
  onActionChange = vi.fn(),
  onPageChange = vi.fn(),
  page = 0,
) {
  wrap(
    <AuditTrail
      data={data}
      action={null}
      onActionChange={onActionChange}
      page={page}
      onPageChange={onPageChange}
      pageSize={50}
    />,
  );
  return { onActionChange, onPageChange };
}

describe('AuditTrail', () => {
  it('shows old and new side by side', () => {
    showTrail();
    const table = screen.getByRole('table');
    const row = within(table).getByText('Revision Approved').closest('tr')!;
    expect(within(row).getByText('13,333')).toBeTruthy();
    expect(within(row).getByText('20,000')).toBeTruthy();
  });

  it('draws a dash only where there genuinely was no prior value', () => {
    showTrail();
    const table = screen.getByRole('table');
    const created = within(table).getByText('Plan Created').closest('tr')!;
    expect(within(created).getAllByText('—').length).toBeGreaterThan(0);
    expect(within(created).getByText('TP-2026-001')).toBeTruthy();
  });

  it('reports the matching total, not the page size', () => {
    showTrail();
    expect(screen.getByText('Showing 1–3 of 11')).toBeTruthy();
  });

  it('asks the server for a filtered trail rather than hiding rows', () => {
    const { onActionChange } = showTrail();
    fireEvent.change(screen.getByLabelText('Action'), {
      target: { value: 'TARGET_LOCKED' },
    });
    expect(onActionChange).toHaveBeenCalledWith('TARGET_LOCKED');
  });

  it('pages forward and cannot page back from the first page', () => {
    const { onPageChange } = showTrail(trail({ total: 120 }));
    expect((screen.getByText('Previous') as HTMLButtonElement).disabled)
      .toBe(true);
    fireEvent.click(screen.getByText('Next'));
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it('explains an empty trail rather than showing a bare table', () => {
    showTrail(trail({ rows: [], total: 0 }));
    expect(screen.getByText(/No actions have been recorded/)).toBeTruthy();
  });
});
