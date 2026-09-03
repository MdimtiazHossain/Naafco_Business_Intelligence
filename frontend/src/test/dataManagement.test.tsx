/**
 * The Data Management pages.
 *
 * What is worth testing here is the part the backend cannot enforce for the
 * user's benefit: that a control is drawn only when the API would honour it,
 * that a destructive action always passes through a confirmation which names
 * the record, and that a refusal from the server is shown rather than
 * swallowed. The authorisation itself is proved in the backend suite, against
 * the endpoints.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import * as services from '../services';
import type { ManagedEntity, RecordListResponse } from '../types/api';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 1, username: 'root', role: 'SUPER_ADMIN', is_admin: true },
    isAdmin: true,
    hasSection: () => true,
    can: () => true,
  }),
}));

vi.mock('../filters/GlobalFilterBar', () => ({
  GlobalFilterBar: () => <div data-testid="filter-bar" />,
}));

vi.mock('../contexts/FilterContext', () => ({
  useFilters: () => ({ query: { period: 'THIS_MONTH' } }),
  FilterProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const CUSTOMER: ManagedEntity = {
  key: 'dim_customer',
  label: 'Customer',
  category: 'MASTER',
  description: 'Customer master.',
  section: 'master_data',
  key_fields: ['customer_code'],
  label_field: 'customer_name',
  fields: [
    {
      name: 'customer_code', label: 'Customer Code', kind: 'code', required: true,
      editable: false, description: 'Official code.', is_key: true,
      references: null, references_column: null, default_visible: true, choices: [],
    },
    {
      name: 'customer_name', label: 'Customer Name', kind: 'text', required: true,
      editable: true, description: '', is_key: false, references: null, references_column: null,
      default_visible: true, choices: [],
    },
    {
      name: 'customer_type', label: 'Customer Type', kind: 'text', required: false,
      editable: true, description: '', is_key: false, references: null, references_column: null,
      default_visible: true, choices: [],
    },
    {
      name: 'address', label: 'Address', kind: 'text', required: false,
      editable: true, description: '', is_key: false, references: null, references_column: null,
      default_visible: false, choices: [],
    },
    {
      name: 'status', label: 'Status', kind: 'text', required: false,
      editable: true, description: '', is_key: false, references: null, references_column: null,
      default_visible: true, choices: ['Active', 'Inactive'],
    },
  ],
  default_columns: ['customer_code', 'customer_name', 'customer_type', 'status'],
  status_field: 'status',
  status_values: ['Active', 'Inactive'],
  soft_delete: true,
  voidable: false,
  scope_level: null,
  parent_table: null,
  parent_column: null,
  filter_fields: [],
  search_fields: ['customer_code', 'customer_name'],
  data_type: null,
  table: 'dim_customer',
};

const SALES: ManagedEntity = {
  ...CUSTOMER,
  key: 'sales',
  label: 'Sales',
  category: 'TRANSACTION',
  description: 'Sales transactions.',
  section: 'transaction_data',
  key_fields: ['sales_id'],
  label_field: 'invoice_no',
  fields: [
    {
      name: 'invoice_no', label: 'Invoice No', kind: 'text', required: false,
      editable: false, description: 'Read-only.', is_key: false, references: null, references_column: null,
      default_visible: true, choices: [],
    },
    {
      name: 'net_sales', label: 'Net Sales', kind: 'numeric', required: false,
      editable: true, description: 'Correctable measure.', is_key: false,
      references: null, references_column: null, default_visible: true, choices: [],
    },
  ],
  default_columns: ['invoice_no', 'net_sales'],
  status_field: null,
  status_values: [],
  soft_delete: false,
  voidable: true,
  data_type: 'sales',
  table: null,
};

function listResponse(overrides: Partial<RecordListResponse> = {}): RecordListResponse {
  return {
    entity: CUSTOMER,
    permissions: { VIEW: true, CREATE: true, EDIT: true, DELETE: true, EXPORT: true },
    rows: [
      {
        _key: 'C001', customer_code: 'C001', customer_name: 'ABC Traders',
        customer_type: 'Dealer', address: '12 Market Road', status: 'Active',
        is_deleted: false,
      },
      {
        _key: 'C002', customer_code: 'C002', customer_name: 'XYZ Traders',
        customer_type: 'Dealer', address: null, status: 'Active',
        is_deleted: false,
      },
    ],
    columns: ['customer_code', 'customer_name', 'customer_type', 'address', 'status'],
    page: 1,
    page_size: 25,
    total: 2,
    total_pages: 1,
    range_from: 1,
    range_to: 2,
    scope_description: 'all regions',
    ...overrides,
  };
}

function wrap(ui: React.ReactNode, route = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/**
 * Open a row's action menu and return a scope over it.
 *
 * A row with more actions than fit inline collapses into `⋮`, which is also the
 * only form on a narrow screen — so driving the menu is the path that works for
 * every row rather than only for short ones.
 */
function rowMenu(index = 0) {
  const row = screen.getAllByTestId('row-actions')[index];
  fireEvent.click(within(row).getByRole('button', { name: 'Actions' }));
  return within(within(row).getByRole('menu'));
}

async function renderMasterTable(response = listResponse()) {
  vi.spyOn(services.masterRecordService, 'list').mockResolvedValue(response as never);
  const { default: MasterDataPage } = await import('../pages/MasterDataPage');
  wrap(
    <Routes>
      <Route path="/data-management/master/:entity" element={<MasterDataPage />} />
    </Routes>,
    '/data-management/master/dim_customer',
  );
  // Wait for whichever row the caller supplied, not a fixed one — a test that
  // renders only a retired record has no "ABC Traders" to wait for.
  const first = String(response.rows[0].customer_name);
  await waitFor(() => expect(screen.getByText(first)).toBeInTheDocument());
}

describe('MasterDataPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the table the entity describes', async () => {
    await renderMasterTable();

    expect(screen.getByRole('columnheader', { name: /Customer Code/ })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: /Customer Name/ })).toBeInTheDocument();
    // A field the entity marks as not visible by default stays out of the way.
    expect(screen.queryByRole('columnheader', { name: /Address/ })).not.toBeInTheDocument();
  });

  it('reports the server-side range rather than counting the page', async () => {
    await renderMasterTable(listResponse({ total: 12450, range_from: 1, range_to: 25 }));
    expect(screen.getByText(/1–25 of 12,450/)).toBeInTheDocument();
  });

  it('sends the search to the server', async () => {
    const list = vi
      .spyOn(services.masterRecordService, 'list')
      .mockResolvedValue(listResponse() as never);
    const { default: MasterDataPage } = await import('../pages/MasterDataPage');
    wrap(
      <Routes>
        <Route path="/data-management/master/:entity" element={<MasterDataPage />} />
      </Routes>,
      '/data-management/master/dim_customer',
    );
    await waitFor(() => expect(screen.getByText('ABC Traders')).toBeInTheDocument());

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'XYZ' } });

    await waitFor(() => {
      const last = list.mock.calls.at(-1)?.[1] as { search?: string };
      expect(last.search).toBe('XYZ');
    });
  });

  it('asks the server to sort rather than sorting the page it holds', async () => {
    const list = vi
      .spyOn(services.masterRecordService, 'list')
      .mockResolvedValue(listResponse() as never);
    const { default: MasterDataPage } = await import('../pages/MasterDataPage');
    wrap(
      <Routes>
        <Route path="/data-management/master/:entity" element={<MasterDataPage />} />
      </Routes>,
      '/data-management/master/dim_customer',
    );
    await waitFor(() => expect(screen.getByText('ABC Traders')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Customer Name/ }));

    await waitFor(() => {
      const last = list.mock.calls.at(-1)?.[1] as { sort_by?: string };
      expect(last.sort_by).toBe('customer_name');
    });
  });

  it('offers view, edit and retire on a row', async () => {
    await renderMasterTable();

    const actions = rowMenu();
    expect(actions.getByText('View')).toBeInTheDocument();
    expect(actions.getByText('Edit')).toBeInTheDocument();
    expect(actions.getByText('Retire')).toBeInTheDocument();
  });

  it('hides the actions the backend would refuse', async () => {
    await renderMasterTable(
      listResponse({ permissions: { VIEW: true, EDIT: false, DELETE: false } }),
    );

    const actions = rowMenu();
    expect(actions.getByText('View')).toBeInTheDocument();
    expect(actions.queryByText('Edit')).not.toBeInTheDocument();
    expect(actions.queryByText('Retire')).not.toBeInTheDocument();
    // …and the whole-table controls go with them.
    expect(screen.queryByRole('button', { name: /New record/ })).not.toBeInTheDocument();
  });

  it('does not delete on the first click', async () => {
    const remove = vi.spyOn(services.masterRecordService, 'remove');
    vi.spyOn(services.masterRecordService, 'dependants').mockResolvedValue({
      counts: { 'sales transactions': 12 }, total: 12, blocking: false,
      description: '12 sales transactions',
    } as never);
    await renderMasterTable();

    fireEvent.click(rowMenu().getByText('Retire'));

    const dialog = await screen.findByRole('dialog');
    expect(remove).not.toHaveBeenCalled();
    // The dialog names what is about to go, and what it carries.
    expect(within(dialog).getByText('ABC Traders')).toBeInTheDocument();
    expect(within(dialog).getByText('C001')).toBeInTheDocument();
    await waitFor(() =>
      expect(within(dialog).getByText(/12 sales transactions/)).toBeInTheDocument(),
    );
  });

  it('retires only after the confirmation is accepted', async () => {
    const remove = vi
      .spyOn(services.masterRecordService, 'remove')
      .mockResolvedValue({ record: {}, action: 'DELETED', changed_fields: [], message: '' } as never);
    vi.spyOn(services.masterRecordService, 'dependants').mockRejectedValue(new Error('x'));
    await renderMasterTable();

    fireEvent.click(rowMenu().getByText('Retire'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retire' }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith('dim_customer', 'C001', undefined));
  });

  it('says the record is retired rather than hiding what happened', async () => {
    await renderMasterTable(
      listResponse({
        rows: [
          {
            _key: 'C002', customer_code: 'C002', customer_name: 'XYZ Traders',
            customer_type: 'Dealer', address: null, status: 'Active',
            is_deleted: true, deleted_by: 'root',
          },
        ],
      }),
    );
    expect(screen.getByText('Retired')).toBeInTheDocument();
    // A retired record offers Restore in place of Retire.
    const actions = rowMenu();
    expect(actions.getByText('Restore')).toBeInTheDocument();
    expect(actions.queryByText('Retire')).not.toBeInTheDocument();
  });

  it('edits status inline without opening the form', async () => {
    const setStatus = vi
      .spyOn(services.masterRecordService, 'setStatus')
      .mockResolvedValue({ record: {}, action: 'UPDATED', changed_fields: [], message: '' } as never);
    await renderMasterTable();

    fireEvent.change(screen.getAllByLabelText('status')[0], {
      target: { value: 'Inactive' },
    });

    await waitFor(() =>
      expect(setStatus).toHaveBeenCalledWith('dim_customer', 'C001', 'Inactive'),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('sends only the fields that changed', async () => {
    const update = vi
      .spyOn(services.masterRecordService, 'update')
      .mockResolvedValue({ record: {}, action: 'UPDATED', changed_fields: [], message: '' } as never);
    await renderMasterTable();

    fireEvent.click(rowMenu().getByText('Edit'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Customer Name/), {
      target: { value: 'ABC Traders Ltd' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    const [, , values] = update.mock.calls[0];
    expect(values).toEqual({ customer_name: 'ABC Traders Ltd' });
  });

  it('does not offer the business code for editing', async () => {
    await renderMasterTable();
    fireEvent.click(rowMenu().getByText('Edit'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText(/Customer Code/)).not.toBeInTheDocument();
  });

  it('shows a rejected field against the field that was rejected', async () => {
    vi.spyOn(services.masterRecordService, 'update').mockRejectedValue(
      new services.ApiError(422, 'invalid', {
        errors: [
          {
            row: null, column: 'Customer Name', value: '',
            error_code: 'MISSING_REQUIRED_FIELD',
            error: 'Customer Name is required and cannot be cleared.',
            suggested_fix: 'Enter a value.', severity: 'ERROR', category: 'VALIDATION',
          },
        ],
      }),
    );
    await renderMasterTable();

    fireEvent.click(rowMenu().getByText('Edit'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Customer Name/), {
      target: { value: '' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(screen.getByText(/is required and cannot be cleared/)).toBeInTheDocument(),
    );
    expect(within(dialog).getByLabelText(/Customer Name/)).toHaveAttribute(
      'aria-invalid', 'true',
    );
  });

  it('selects rows and offers the bulk actions', async () => {
    const bulk = vi
      .spyOn(services.masterRecordService, 'bulk')
      .mockResolvedValue({ succeeded: ['C001'], failed: [], success_count: 1, failure_count: 0 } as never);
    await renderMasterTable();

    fireEvent.click(screen.getAllByLabelText('Select row')[0]);
    expect(screen.getByText('1 selected')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
    await waitFor(() => expect(bulk).toHaveBeenCalled());
    expect(bulk.mock.calls[0].slice(0, 3)).toEqual([
      'dim_customer', 'DEACTIVATE', ['C001'],
    ]);
  });

  it('selecting the header checkbox selects the page, not the whole table', async () => {
    await renderMasterTable(listResponse({ total: 12450 }));

    fireEvent.click(screen.getByLabelText('Select all rows on this page'));
    expect(screen.getByText('2 selected')).toBeInTheDocument();
  });
});

describe('TransactionDataPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  const salesList = {
    entity: SALES,
    permissions: { VIEW: true, EDIT: true, DELETE: true, EXPORT: true },
    rows: [
      { _key: '1', sales_id: 1, invoice_no: 'INV-001', net_sales: 5000, is_void: false },
    ],
    columns: ['invoice_no', 'net_sales'],
    page: 1, page_size: 25, total: 1, total_pages: 1,
    range_from: 1, range_to: 1, scope_description: 'all regions',
  };

  async function renderTransactions(response: unknown = salesList) {
    vi.spyOn(services.transactionRecordService, 'list').mockResolvedValue(
      response as never,
    );
    const { default: TransactionDataPage } = await import('../pages/TransactionDataPage');
    wrap(
      <Routes>
        <Route
          path="/data-management/transactions/:type"
          element={<TransactionDataPage />}
        />
      </Routes>,
      '/data-management/transactions/sales',
    );
    await waitFor(() => expect(screen.getByText('INV-001')).toBeInTheDocument());
  }

  it('offers Void rather than Delete, and never Create', async () => {
    await renderTransactions();

    const actions = rowMenu();
    expect(actions.getByText('Void')).toBeInTheDocument();
    expect(actions.queryByText('Retire')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New record/ })).not.toBeInTheDocument();
  });

  it('will not void without a reason', async () => {
    vi.spyOn(services.transactionRecordService, 'get').mockResolvedValue({
      dependants: { counts: {}, total: 0, blocking: false, description: '' },
    } as never);
    const voidCall = vi.spyOn(services.transactionRecordService, 'void');
    await renderTransactions();

    fireEvent.click(rowMenu().getByText('Void'));
    const dialog = await screen.findByRole('dialog');

    // The API requires a reason, so the button stays disabled until there is one.
    const confirm = within(dialog).getByRole('button', { name: 'Void' });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByRole('textbox'), {
      target: { value: 'Duplicate invoice' },
    });
    expect(confirm).toBeEnabled();
    expect(voidCall).not.toHaveBeenCalled();
  });

  it('shows the dependency refusal instead of a bare error', async () => {
    vi.spyOn(services.transactionRecordService, 'get').mockResolvedValue({
      dependants: {
        counts: { collections: 1 }, total: 1, blocking: true,
        description: '1 collections',
      },
    } as never);
    await renderTransactions();

    fireEvent.click(rowMenu().getByText('Void'));
    const dialog = await screen.findByRole('dialog');

    await waitFor(() =>
      expect(within(dialog).getByText(/1 collections/)).toBeInTheDocument(),
    );
    // Blocked means blocked: confirming is not offered at all.
    expect(within(dialog).getByRole('button', { name: 'Void' })).toBeDisabled();
  });

  it('marks a voided row and offers to reinstate it', async () => {
    await renderTransactions({
      ...salesList,
      rows: [
        {
          _key: '1', sales_id: 1, invoice_no: 'INV-001', net_sales: 5000,
          is_void: true, void_reason: 'Duplicate', voided_by: 'root',
        },
      ],
    });

    const actions = rowMenu();
    expect(actions.getByText('Reinstate')).toBeInTheDocument();
    expect(actions.queryByText('Void')).not.toBeInTheDocument();
    expect(actions.queryByText('Correct')).not.toBeInTheDocument();
  });
});
