/** Component tests: table behaviour, states, KPI cards and the login form. */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AiResponse, AnswerText } from '../components/AiResponse';
import { KpiCard } from '../components/KpiCard';
import { EmptyState, ErrorState, QueryState } from '../components/States';
import { AuthProvider } from '../contexts/AuthContext';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { LoginPage } from '../pages/LoginPage';
import { ApiError } from '../services/apiClient';
import { DataTable } from '../tables/DataTable';

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const ROWS = [
  { code: 'REG001', label: 'Dhaka', net_sales: 15_000_000, achievement_percent: 31.4 },
  { code: 'REG002', label: 'Chattogram', net_sales: 7_200_000, achievement_percent: 28.1 },
  { code: 'REG003', label: 'Rajshahi', net_sales: 2_700_000, achievement_percent: 22.0 },
];

const COLUMNS = [
  { key: 'label', header: 'Name' },
  { key: 'net_sales', header: 'Net Sales' },
  { key: 'achievement_percent', header: 'Achievement' },
];

describe('DataTable', () => {
  it('formats currency and percentages by column name', () => {
    wrap(<DataTable rows={ROWS} columns={COLUMNS} />);
    expect(screen.getByText('৳1.50 Cr')).toBeInTheDocument();
    expect(screen.getByText('31.4%')).toBeInTheDocument();
  });

  // A detail table repeats `code` freely — one invoice has many lines — so the
  // fallback row key must not be `code` alone. It was, and React silently
  // deduplicated the rows behind an unkeyed fragment.
  it('renders every row when two rows share a code', () => {
    const lines = [
      { code: 'INV-1', label: 'Line one', net_sales: 100 },
      { code: 'INV-1', label: 'Line two', net_sales: 200 },
      { code: 'INV-1', label: 'Line three', net_sales: 300 },
    ];
    wrap(<DataTable rows={lines} columns={[{ key: 'label', header: 'Name' }]} />);
    expect(screen.getByText('Line one')).toBeInTheDocument();
    expect(screen.getByText('Line two')).toBeInTheDocument();
    expect(screen.getByText('Line three')).toBeInTheDocument();
  });

  it('filters rows as you search', () => {
    wrap(<DataTable rows={ROWS} columns={COLUMNS} />);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'Rajshahi' } });
    expect(screen.getByText('Rajshahi')).toBeInTheDocument();
    expect(screen.queryByText('Dhaka')).not.toBeInTheDocument();
  });

  it('sorts when a header is clicked', () => {
    wrap(<DataTable rows={ROWS} columns={COLUMNS} />);
    fireEvent.click(screen.getByRole('button', { name: /Net Sales/ }));
    const cells = screen.getAllByRole('cell').map((cell) => cell.textContent);
    expect(cells[0]).toBe('Dhaka');    // descending by default
  });

  it('paginates on the client', () => {
    wrap(<DataTable rows={ROWS} columns={COLUMNS} pageSize={2} />);
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
    expect(screen.queryByText('Rajshahi')).not.toBeInTheDocument();
  });

  it('delegates paging to the server in server mode', () => {
    const onPageChange = vi.fn();
    wrap(
      <DataTable
        rows={ROWS}
        columns={COLUMNS}
        serverMode
        page={2}
        totalPages={5}
        total={250}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByText(/Page 2 of 5/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Next'));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it('hides a column from the picker', () => {
    wrap(<DataTable rows={ROWS} columns={COLUMNS} />);
    fireEvent.click(screen.getByRole('button', { name: /Columns/ }));
    fireEvent.click(screen.getByLabelText('Achievement'));
    expect(screen.queryByText('31.4%')).not.toBeInTheDocument();
  });

  it('shows an empty message rather than a blank table', () => {
    wrap(<DataTable rows={[]} columns={COLUMNS} />);
    expect(screen.getByText(/No data found/)).toBeInTheDocument();
  });

  it('calls back when a row is clicked, which is how drill-down works', () => {
    const onRowClick = vi.fn();
    wrap(<DataTable rows={ROWS} columns={COLUMNS} onRowClick={onRowClick} />);
    fireEvent.click(screen.getByText('Dhaka'));
    expect(onRowClick).toHaveBeenCalledWith(expect.objectContaining({ code: 'REG001' }));
  });
});

describe('states', () => {
  it('renders an empty message', () => {
    wrap(<EmptyState message="Nothing here" />);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
  });

  it('explains a permission refusal without offering a pointless retry', () => {
    const onRetry = vi.fn();
    wrap(
      <ErrorState
        error={new ApiError(403, "You don't have permission to access this information.")}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText(/don't have permission/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Try again/ })).not.toBeInTheDocument();
  });

  it('offers a retry for a real failure', () => {
    const onRetry = vi.fn();
    wrap(<ErrorState error={new ApiError(500, 'Something went wrong.')} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: /Try again/ }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('shows a skeleton rather than content while loading', () => {
    wrap(
      <QueryState isLoading error={null}>
        <p>content</p>
      </QueryState>,
    );
    expect(screen.queryByText('content')).not.toBeInTheDocument();
  });

  it('shows the empty message rather than content when there is no data', () => {
    wrap(
      <QueryState isLoading={false} error={null} isEmpty>
        <p>content</p>
      </QueryState>,
    );
    expect(screen.getByText(/No data found/)).toBeInTheDocument();
    expect(screen.queryByText('content')).not.toBeInTheDocument();
  });

  it('shows content once data has arrived', () => {
    wrap(
      <QueryState isLoading={false} error={null}>
        <p>content</p>
      </QueryState>,
    );
    expect(screen.getByText('content')).toBeInTheDocument();
  });
});

describe('KpiCard', () => {
  it('shows the value, growth and comparison', () => {
    wrap(
      <KpiCard
        kpi={{
          key: 'total_sales',
          label: 'Total Sales',
          value: 18_700_000,
          previous_value: 17_250_000,
          growth_percent: 8.4,
          format: 'currency',
        }}
      />,
    );
    expect(screen.getByText('৳1.87 Cr')).toBeInTheDocument();
    expect(screen.getByText('+8.4%')).toBeInTheDocument();
    expect(screen.getByText('(৳1.73 Cr)')).toBeInTheDocument();
  });

  it('renders a missing ratio as n/a, never 0%', () => {
    wrap(
      <KpiCard
        kpi={{
          key: 'achievement',
          label: 'Achievement',
          value: null,
          previous_value: null,
          growth_percent: null,
          format: 'percent',
        }}
      />,
    );
    expect(screen.getByText('n/a')).toBeInTheDocument();
  });
});

/**
 * The stock status colour standard: one colour for the *complete* metric.
 *
 * These assert the class rather than a computed colour because the class is the
 * token — `index.css` owns what green and amber are, and a component that
 * spelled its own shade would pass a colour assertion while breaking the
 * standard. Both halves are checked on every case: colouring the figure and
 * leaving the label grey is the specific mistake this replaces.
 */
describe('stock status colours', () => {
  const cases = [
    ['unrestricted_stock', 'Unrestricted Stock (KG/LTR)', 'stock-status--unrestricted'],
    ['expiring_soon_stock', 'Expiring Soon (KG/LTR)', 'stock-status--expiring'],
    ['expired_stock', 'Expired Stock (KG/LTR)', 'stock-status--expired'],
  ] as const;

  it.each(cases)('colours the %s label and value alike', (key, label, className) => {
    wrap(
      <KpiCard
        kpi={{
          key,
          label,
          value: 125_500,
          previous_value: null,
          growth_percent: null,
          format: 'stock',
        }}
      />,
    );
    expect(screen.getByText(label)).toHaveClass(className);
    // The value is the plain grouped figure: the unit is in the label above it
    // and is never repeated beside the number.
    expect(screen.getByText('1,25,500')).toHaveClass(className);
  });

  it('leaves an unrelated KPI in the neutral colour', () => {
    wrap(
      <KpiCard
        kpi={{
          key: 'total_sales',
          label: 'Total Sales',
          value: 18_700_000,
          previous_value: null,
          growth_percent: null,
          format: 'currency',
        }}
      />,
    );
    expect(screen.getByText('Total Sales').className).not.toContain('stock-status');
    expect(screen.getByText('৳1.87 Cr').className).not.toContain('stock-status');
  });

  it('colours a stock status column in a table, heading and cells', () => {
    wrap(
      <DataTable
        rows={[{ code: 'P1', label: 'Plant 1', unrestricted_stock: 125_500, total_stock: 130_000 }]}
        columns={[
          { key: 'label', header: 'Plant' },
          { key: 'unrestricted_stock' },
          { key: 'total_stock' },
        ]}
        searchable={false}
      />,
    );
    // `humanizeColumn` supplies the heading, unit included.
    const heading = screen.getByRole('columnheader', { name: /Unrestricted Stock \(KG\/LTR\)/ });
    expect(heading).toHaveClass('stock-status--unrestricted');
    expect(screen.getByText('1,25,500')).toHaveClass('stock-status--unrestricted');
    // Total stock is not a status and keeps the neutral colour.
    expect(screen.getByText('1,30,000').className).not.toContain('stock-status');
  });
});

describe('AiResponse', () => {
  function answer(text: string) {
    return {
      conversation_id: 'c1',
      message_id: 1,
      intent: 'TARGET_ACHIEVEMENT',
      answer: text,
      data: {},
      filters: {},
      date_range: {},
      sources: [],
      assumptions: [],
      chart: null,
      needs_clarification: false,
      error_code: null,
      language: 'en',
      tools_used: [],
      elapsed_ms: 12,
    };
  }

  it('bolds a label that starts the line', () => {
    wrap(<AiResponse response={answer('**Target:** ৳1.11 Cr')} />);
    expect(screen.getByText('Target:').tagName).toBe('STRONG');
  });

  it('bolds a label that sits mid-line, rather than showing the asterisks', () => {
    // The backend prefixes this line with an emoji, which used to defeat the
    // start-of-line-only match and leave `**Period:**` visible on the page.
    wrap(<AiResponse response={answer('📅 **Period:** This month')} />);
    expect(screen.getByText('Period:').tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it('bolds every label on a line that carries two of them', () => {
    wrap(<AiResponse response={answer('**Gross:** ৳40 L   **Discount:** ৳1 L')} />);
    expect(screen.getByText('Gross:').tagName).toBe('STRONG');
    expect(screen.getByText('Discount:').tagName).toBe('STRONG');
  });

  it('bolds inside a bullet too', () => {
    wrap(<AiResponse response={answer('- **Dhaka:** ৳15 Cr')} />);
    expect(screen.getByText('Dhaka:').tagName).toBe('STRONG');
  });

  it('leaves the markdown table to the data table on a live answer', () => {
    wrap(<AiResponse response={answer('**Target:** ৳1 Cr\n| Name | Actual |\n|---|---:|\n| Dhaka | ৳21 L |')} />);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows an assumption once, not twice', () => {
    // The backend writes every assumption into the answer text, because that
    // text is the whole answer on WhatsApp, on the CLI and in a conversation
    // reloaded from history. Repeating the same sentence underneath printed it
    // twice — once at body size and again in 11px grey — and the quiet copy is
    // what taught people to stop reading the line that says a period or a
    // filter was assumed.
    const sentence = 'Keeping your earlier filter: Adamdighi.';
    const response = {
      ...answer('**Net Sales:** ৳19.60 L\nℹ️ ' + sentence),
      assumptions: [sentence],
    };
    wrap(<AiResponse response={response} />);
    expect(screen.getAllByText(sentence)).toHaveLength(1);
  });

  it('still shows the sources, which the answer text does not carry', () => {
    const response = { ...answer('**Net Sales:** ৳19.60 L'), sources: ['vw_sales_detail'] };
    wrap(<AiResponse response={response} />);
    expect(screen.getByText(/vw_sales_detail/)).toBeInTheDocument();
  });
});

describe('AnswerText replaying history', () => {
  // A conversation reloaded from the backend has only prose — no `data.rows` —
  // so the table has to be read back out of the text.
  const HISTORY = [
    '🎯 Target Achievement',
    '',
    '**Target:** ৳1.11 Cr',
    '',
    '| Name | Actual | Achievement |',
    '|---|---:|---:|',
    '| Dhaka | ৳21.42 L | 45.8% |',
    '| Chattogram | ৳3.10 L | 4.8% |',
    '',
    '📅 **Period:** This month (01 Aug 2026 – 11 Aug 2026)',
  ].join('\n');

  it('renders the markdown table rather than dropping its numbers', () => {
    wrap(<AnswerText text={HISTORY} withTables />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('৳21.42 L')).toBeInTheDocument();
    expect(screen.getByText('Chattogram')).toBeInTheDocument();
    // The `|---|---:|` alignment row is not data.
    expect(screen.queryByText('---')).not.toBeInTheDocument();
  });

  it('still bolds the labels around the table', () => {
    wrap(<AnswerText text={HISTORY} withTables />);
    expect(screen.getByText('Target:').tagName).toBe('STRONG');
    expect(screen.getByText('Period:').tagName).toBe('STRONG');
  });

  it('drops table lines when the caller renders rows itself', () => {
    wrap(<AnswerText text={HISTORY} />);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('Chattogram')).not.toBeInTheDocument();
  });
});

describe('LoginPage', () => {
  function renderLogin() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <I18nProvider>
          <ThemeProvider>
            <MemoryRouter>
              <AuthProvider>
                <LoginPage />
              </AuthProvider>
            </MemoryRouter>
          </ThemeProvider>
        </I18nProvider>
      </QueryClientProvider>,
    );
  }

  it('requires both fields before calling the API', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    renderLogin();

    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enter your username and password.',
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('toggles password visibility', () => {
    vi.stubGlobal('fetch', vi.fn());
    renderLogin();
    const field = screen.getByLabelText('Password') as HTMLInputElement;
    expect(field.type).toBe('password');
    fireEvent.click(screen.getByLabelText('Show password'));
    expect(field.type).toBe('text');
  });

  it('shows the server error without revealing whether the user exists', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        headers: { get: () => null },
        json: async () => ({ detail: 'Incorrect username or password.' }),
      } as unknown as Response),
    );
    renderLogin();

    fireEvent.change(screen.getByLabelText('Username or email'), {
      target: { value: 'ceo' },
    });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Incorrect username or password.'),
    );
  });
});
