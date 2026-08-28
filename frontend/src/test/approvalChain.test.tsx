/**
 * The approval screens: the chain, the matrix, the queue and the revision form.
 *
 * Four promises are pinned here, and each is a place where a shortcut would
 * quietly mislead somebody.
 *
 * **The chain is drawn bottom-up and shows the steps that do not sign.** The
 * Sales Officer sits in it without approving at it, and hiding them would make
 * the step numbers look like they skipped.
 *
 * **Unlimited and 0% never render the same.** A blank adjustment limit means
 * the role may move any figure; zero means it may move none. As an empty cell
 * they look alike and mean opposite things.
 *
 * **The revision form sends the raw string that was typed.** Parsing it in the
 * browser would make this the place where a target quietly becomes a different
 * number.
 *
 * **Blockers are listed one per line.** They fail for different reasons and are
 * fixed in different places.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApprovalMatrixEditor } from '../components/targetmgmt/ApprovalMatrixEditor';
import { ApprovalPanel } from '../components/targetmgmt/ApprovalPanel';
import { MyApprovals } from '../components/targetmgmt/MyApprovals';
import { RevisionDialog } from '../components/targetmgmt/RevisionDialog';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetApprovalQueue,
  TargetApprovalState,
  TargetChainStep,
  TargetMatrixResponse,
  TargetMatrixRow,
  TargetReviewRow,
} from '../types/api';

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider>
      <ThemeProvider>{node}</ThemeProvider>
    </I18nProvider>,
  );
}

function step(overrides: Partial<TargetChainStep> = {}): TargetChainStep {
  return {
    role: 'UNIT_MANAGER',
    hierarchy_level: 'unit',
    approval_sequence: 3,
    can_approve: true,
    approved: false,
    actor: null,
    acted_at: null,
    is_current: false,
    ...overrides,
  };
}

const CHAIN: TargetChainStep[] = [
  step({
    role: 'SALES_OFFICER', hierarchy_level: 'sub_territory',
    approval_sequence: 1, can_approve: false,
  }),
  step({ approved: true, actor: 'karim' }),
  step({
    role: 'AREA_MANAGER', hierarchy_level: 'area', approval_sequence: 4,
    is_current: true,
  }),
  step({
    role: 'MANAGEMENT', hierarchy_level: 'company', approval_sequence: 8,
  }),
];

function approvalState(
  overrides: Partial<TargetApprovalState> = {},
): TargetApprovalState {
  return {
    steps: CHAIN,
    history: [],
    blockers: [],
    my_role: 'AREA_MANAGER',
    my_matrix: null,
    my_turn: true,
    already_acted: null,
    current_role: 'AREA_MANAGER',
    version: { status: 'UNDER_REVIEW' } as never,
    ...overrides,
  };
}

function matrixRow(overrides: Partial<TargetMatrixRow> = {}): TargetMatrixRow {
  return {
    role: 'REGIONAL_MANAGER',
    hierarchy_level: 'region',
    approval_sequence: 5,
    can_edit: true,
    can_approve: true,
    can_reject: true,
    can_revise: true,
    adjustment_limit_percent: 10,
    is_active: true,
    limit_label: '±10%',
    ...overrides,
  };
}

function matrixData(
  overrides: Partial<TargetMatrixResponse> = {},
): TargetMatrixResponse {
  const rows = [
    matrixRow(),
    matrixRow({
      role: 'MANAGEMENT', hierarchy_level: 'company', approval_sequence: 8,
      adjustment_limit_percent: null, limit_label: 'Unlimited',
    }),
    matrixRow({
      role: 'SALES_OFFICER', hierarchy_level: 'sub_territory',
      approval_sequence: 1, can_approve: false, can_reject: false,
      adjustment_limit_percent: 0, limit_label: '±0%',
    }),
    matrixRow({
      role: 'ADMIN', hierarchy_level: 'company', approval_sequence: null,
      can_approve: false, can_reject: false, can_revise: false,
      adjustment_limit_percent: null, limit_label: 'Unlimited',
    }),
  ];
  return {
    rows,
    chain: rows.filter((row) => row.approval_sequence !== null),
    levels: ['company', 'zone', 'region', 'area', 'unit', 'territory',
             'sub_territory', 'customer'],
    defaults: rows,
    my_role: 'MANAGEMENT',
    ...overrides,
  };
}

function reviewRow(overrides: Partial<TargetReviewRow> = {}): TargetReviewRow {
  return {
    level: 'region',
    node_code: 'REG001',
    name: 'Dhaka',
    parent_level: 'company',
    parent_code: 'C001',
    depth: 1,
    has_children: true,
    target_volume: 80000,
    system_volume: 80000,
    adjusted: false,
    target_quantity: 160000,
    target_value: 38400000,
    missing_derivation: [],
    previous_year_volume: 54000,
    growth_percent: 48.1,
    actual_volume: null,
    achievement_percent: null,
    recon_variance: 0,
    pending_revisions: 0,
    status: 'UNDER_REVIEW',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// ApprovalPanel
// ---------------------------------------------------------------------------

describe('ApprovalPanel', () => {
  const noop = vi.fn();
  const show = (state = approvalState(), canApprove = true) =>
    wrap(
      <ApprovalPanel
        state={state}
        canApprove={canApprove}
        submittable={false}
        busy={false}
        error={null}
        onApprove={noop}
        onReject={noop}
        onSendBack={noop}
        onSubmit={noop}
      />,
    );

  it('draws the chain bottom-up, in the order it runs', () => {
    show();
    const items = screen.getAllByRole('listitem');
    expect(items[0].textContent).toContain('Sales Officer');
    expect(items[items.length - 1].textContent).toContain('Management');
  });

  it('shows a step that sits in the chain without signing at it', () => {
    show();
    const officer = screen.getByText('Sales Officer').closest('li')!;
    expect(within(officer).getByText('questions only')).toBeTruthy();
  });

  it('names who approved a completed step', () => {
    show();
    const unit = screen.getByText('Unit Manager').closest('li')!;
    expect(within(unit).getByText(/karim/)).toBeTruthy();
  });

  it('lists each blocker separately', () => {
    show(
      approvalState({
        blockers: [
          '2 revision requests are still open.',
          'Reconciliation does not balance at 3 nodes.',
        ],
      }),
    );
    expect(screen.getByText('2 revision requests are still open.')).toBeTruthy();
    expect(
      screen.getByText('Reconciliation does not balance at 3 nodes.'),
    ).toBeTruthy();
  });

  it('offers approve and reject only when it is this role’s turn', () => {
    show(approvalState({ my_turn: false, current_role: 'ZONE_MANAGER' }));
    expect(screen.queryByText('Approve')).toBeNull();
    expect(screen.getByText(/still with Zone Manager|with Zone Manager/i)).toBeTruthy();
  });

  it('will not reject without a reason', () => {
    const onReject = vi.fn();
    wrap(
      <ApprovalPanel
        state={approvalState()}
        canApprove
        submittable={false}
        busy={false}
        error={null}
        onApprove={noop}
        onReject={onReject}
        onSendBack={noop}
        onSubmit={noop}
      />,
    );
    const button = screen.getByText('Reject') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Growth assumption is too high.' },
    });
    expect((screen.getByText('Reject') as HTMLButtonElement).disabled).toBe(false);
  });

  it('approves without requiring a comment', () => {
    const onApprove = vi.fn();
    wrap(
      <ApprovalPanel
        state={approvalState()}
        canApprove
        submittable={false}
        busy={false}
        error={null}
        onApprove={onApprove}
        onReject={noop}
        onSendBack={noop}
        onSubmit={noop}
      />,
    );
    fireEvent.click(screen.getByText('Approve'));
    expect(onApprove).toHaveBeenCalledWith('');
  });

  it('offers send-back on an approved version, because locking is the point of no return', () => {
    show(
      approvalState({
        my_turn: false,
        current_role: null,
        version: { status: 'APPROVED' } as never,
      }),
    );
    expect(screen.getByText('Send back for review')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// ApprovalMatrixEditor
// ---------------------------------------------------------------------------

describe('ApprovalMatrixEditor', () => {
  it('renders unlimited and zero differently', () => {
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable={false}
        busy={false}
        error={null}
        onSave={vi.fn()}
      />,
    );
    // Two roles hold an unlimited limit and one holds zero — the point is that
    // the two render as different text, not as the same empty cell.
    expect(screen.getAllByText('Unlimited').length).toBe(2);
    expect(screen.getByText('±0%')).toBeTruthy();
  });

  it('shows a role outside the chain with a dash and an explanation', () => {
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable={false}
        busy={false}
        error={null}
        onSave={vi.fn()}
      />,
    );
    const admin = screen.getByText('Admin').closest('tr')!;
    const dash = within(admin).getByText('—');
    expect(dash.getAttribute('title')).toContain('signs nothing');
  });

  it('sends only the fields that were touched', () => {
    const onSave = vi.fn();
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable
        busy={false}
        error={null}
        onSave={onSave}
      />,
    );
    fireEvent.click(screen.getByLabelText('REGIONAL_MANAGER can_reject'));
    fireEvent.click(screen.getByText('Save'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const [entries] = onSave.mock.calls[0];
    expect(entries).toHaveLength(1);
    expect(entries[0].role).toBe('REGIONAL_MANAGER');
    expect(entries[0].fields_present).toEqual(['can_reject']);
  });

  it('saves nothing until something changes', () => {
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable
        busy={false}
        error={null}
        onSave={vi.fn()}
      />,
    );
    expect((screen.getByText('Save').closest('button') as HTMLButtonElement).disabled)
      .toBe(true);
  });

  it('the unlimited toggle sets null rather than blanking the number', () => {
    const onSave = vi.fn();
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable
        busy={false}
        error={null}
        onSave={onSave}
      />,
    );
    fireEvent.click(screen.getByLabelText('REGIONAL_MANAGER unlimited'));
    fireEvent.click(screen.getByText('Save'));
    const [entries] = onSave.mock.calls[0];
    expect(entries[0].adjustment_limit_percent).toBeNull();
  });

  it('hides the controls entirely when the reader cannot edit', () => {
    wrap(
      <ApprovalMatrixEditor
        data={matrixData()}
        editable={false}
        busy={false}
        error={null}
        onSave={vi.fn()}
      />,
    );
    expect(screen.queryByText('Save')).toBeNull();
    expect(screen.queryByText('Restore defaults')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// RevisionDialog
// ---------------------------------------------------------------------------

describe('RevisionDialog', () => {
  const open = (props: Partial<Parameters<typeof RevisionDialog>[0]> = {}) => {
    const onSubmit = vi.fn();
    wrap(
      <RevisionDialog
        row={reviewRow()}
        materialCode={null}
        limitPercent={10}
        busy={false}
        error={null}
        onClose={vi.fn()}
        onSubmit={onSubmit}
        {...props}
      />,
    );
    return onSubmit;
  };

  it('sends the raw string that was typed, not a parsed number', () => {
    const onSubmit = open();
    const [volumeField, reasonField] = screen.getAllByRole('textbox');
    fireEvent.change(volumeField, { target: { value: '90,000' } });
    fireEvent.change(reasonField, { target: { value: 'Two new distributors.' } });
    fireEvent.click(screen.getByText('Send request'));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ requested_volume: '90,000' }),
    );
  });

  it('will not send without a reason', () => {
    open();
    const [volumeField] = screen.getAllByRole('textbox');
    fireEvent.change(volumeField, { target: { value: '90000' } });
    expect(
      (screen.getByText('Send request').closest('button') as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it('warns before submitting that a large change will escalate', () => {
    open();
    const [volumeField] = screen.getAllByRole('textbox');
    fireEvent.change(volumeField, { target: { value: '120000' } });
    expect(screen.getByText(/escalated/)).toBeTruthy();
  });

  it('says nothing about escalation inside the limit', () => {
    open();
    const [volumeField] = screen.getAllByRole('textbox');
    fireEvent.change(volumeField, { target: { value: '84000' } });
    expect(screen.queryByText(/escalated/)).toBeNull();
  });

  it('never shows 0% for an entry it cannot read', () => {
    open();
    const [volumeField] = screen.getAllByRole('textbox');
    fireEvent.change(volumeField, { target: { value: '12,5OO' } });
    expect(screen.queryByText(/0.0%/)).toBeNull();
    expect(screen.getByText(/Thousands separators/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// MyApprovals
// ---------------------------------------------------------------------------

describe('MyApprovals', () => {
  function queue(overrides: Partial<TargetApprovalQueue> = {}): TargetApprovalQueue {
    return {
      versions: [
        {
          plan: {
            plan_id: 1, plan_code: 'TP-2026-001', financial_year: 'FY 2026-27',
            target_period: 'FY',
          } as never,
          version: { version_id: 9, version_no: 2 } as never,
          is_my_turn: false,
          waiting_on: ['UNIT_MANAGER', 'AREA_MANAGER'],
          blockers: ['1 revision request is still open.'],
        },
      ],
      revisions: [
        {
          revision_id: 3, version_id: 9, level: 'customer',
          node_code: 'CUST-001', node_name: 'Rahim Traders',
          material_code: null, system_volume: 13333, requested_volume: 20000,
          approved_volume: null, change_percent: 50, status: 'ESCALATED',
          escalated_to_role: 'MANAGEMENT', reason: 'Two new outlets.',
          requested_by: 'karim', decided_by: null, decided_at: null,
          requested_at: null, plan_code: 'TP-2026-001', version_no: 2,
          version_status: 'UNDER_REVIEW',
        },
      ],
      notes: [],
      role: 'MANAGEMENT',
      matrix: matrixRow({ role: 'MANAGEMENT', limit_label: 'Unlimited' }),
      ...overrides,
    };
  }

  it('shows a version that is not yet this reader’s turn, and says who has it', () => {
    wrap(
      <MyApprovals
        data={queue()}
        busy={false}
        onOpenVersion={vi.fn()}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByText(/Still with Unit Manager, Area Manager/)).toBeTruthy();
  });

  it('marks an escalated request as escalated', () => {
    wrap(
      <MyApprovals
        data={queue()}
        busy={false}
        onOpenVersion={vi.fn()}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByText('Escalated')).toBeTruthy();
    expect(screen.getByText(/\(\+50\.0%\)/)).toBeTruthy();
  });

  it('grants a request through the version it belongs to', () => {
    const onDecide = vi.fn();
    wrap(
      <MyApprovals
        data={queue()}
        busy={false}
        onOpenVersion={vi.fn()}
        onDecide={onDecide}
      />,
    );
    fireEvent.click(screen.getByText('Grant'));
    expect(onDecide).toHaveBeenCalledWith(9, 3, true);
  });

  it('explains an empty queue rather than showing a blank screen', () => {
    wrap(
      <MyApprovals
        data={queue({
          versions: [], revisions: [], matrix: null,
          notes: ['Admin is not part of the approval chain, so no target is waiting on it.'],
        })}
        busy={false}
        onOpenVersion={vi.fn()}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByText(/not part of the approval chain/)).toBeTruthy();
  });
});
