/**
 * Renders an AI answer from **structured** backend data.
 *
 * The agent returns `{answer, data, chart, sources, assumptions}`; the answer
 * text is plain markdown-ish prose the backend composed, and the numbers live
 * in `data`. This component renders those parts as text, KPIs, a table and a
 * chart — it never interprets HTML from the model, so a model that emitted
 * markup could not inject anything into the page.
 */

import { Database, Info, MessageSquareWarning } from 'lucide-react';
import type { ReactNode } from 'react';
import { AutoChart } from '../charts/Charts';
import { useT } from '../contexts/I18nContext';
import { DataTable, columnsFromRows } from '../tables/DataTable';
import { formatAmount, formatPercent, humanizeColumn } from '../utils/format';
import type { ChatMessageResponse } from '../types/api';

/**
 * Bold every `**…**` span in one line, wherever it sits.
 *
 * The backend puts bold labels mid-line as well as at the start — `📅
 * **Period:** …` and `**Gross:** x   **Discount:** y` — so matching only a
 * leading span left the asterisks visible in the page. Text is emitted as React
 * strings, never HTML, so this cannot inject markup.
 */
function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\*\*(.+?)\*\*/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(
      <strong key={match.index} className="font-semibold">
        {match[1]}
      </strong>,
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length > 0 ? parts : [text];
}

/** Split a markdown table row into its cells. */
function cells(line: string): string[] {
  return line.replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

/** `|---|---:|` and friends — the alignment row, which carries no data. */
function isSeparatorRow(line: string): boolean {
  return cells(line).every((cell) => /^:?-{2,}:?$/.test(cell));
}

/**
 * A markdown table from the answer text.
 *
 * Live answers render their rows from `data.rows`, but a conversation reloaded
 * from history has only the prose the backend composed, so its table has to be
 * read back out of the text or the numbers would simply disappear.
 */
function TextTable({ rows }: { rows: string[] }) {
  const parsed = rows.filter((row) => !isSeparatorRow(row)).map(cells);
  if (parsed.length === 0) return null;
  const [header, ...body] = parsed;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-slate-200 text-left dark:border-slate-700">
            {header.map((cell, index) => (
              <th key={index} className="px-2 py-1 font-semibold text-slate-500">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-b border-slate-100 dark:border-slate-800">
              {row.map((cell, index) => (
                <td key={index} className="px-2 py-1 tabular-nums">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Render the agent's prose safely: no HTML, just paragraphs and simple lists.
 *
 * `withTables` is for replayed history, where the table is only in the text.
 * A live answer leaves it off so the same rows are not shown twice — once as
 * text and once as the sortable `DataTable` built from `data.rows`.
 */
export function AnswerText({ text, withTables = false }: { text: string; withTables?: boolean }) {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let table: string[] = [];

  function flushTable(key: string) {
    if (table.length === 0) return;
    if (withTables) blocks.push(<TextTable key={key} rows={table} />);
    table = [];
  }

  lines.forEach((line, index) => {
    const trimmed = line.trim();
    if (trimmed.startsWith('|')) {
      table.push(trimmed);
      return;
    }
    flushTable(`t-${index}`);
    blocks.push(renderLine(trimmed, index));
  });
  flushTable('t-end');

  return <div className="space-y-1.5 text-sm leading-relaxed">{blocks}</div>;
}

/** One line of prose: a blank spacer, a bullet, an aside, or a paragraph. */
function renderLine(trimmed: string, index: number): ReactNode {
  if (!trimmed) return <div key={index} className="h-1" />;
  if (trimmed.startsWith('- ')) {
    return (
      <p key={index} className="pl-4 text-slate-600 dark:text-slate-300">
        • {inline(trimmed.slice(2))}
      </p>
    );
  }
  if (trimmed.startsWith('_') && trimmed.endsWith('_')) {
    return (
      <p key={index} className="text-xs italic text-slate-500">
        {trimmed.slice(1, -1)}
      </p>
    );
  }
  // An assumption the backend had to make: a period it inherited, a filter it
  // kept or ended, a top-N still in force. These change how the figure above
  // should be read, so they stay at body size and gain an icon rather than
  // being shrunk into the footnote grey they used to be repeated in. Matched on
  // the information character alone, because the emoji arrives both with and
  // without its variation selector.
  if (trimmed.startsWith('ℹ')) {
    return (
      <p
        key={index}
        className="flex items-start gap-1.5 text-slate-600 dark:text-slate-300"
      >
        <Info size={14} className="mt-0.5 shrink-0" />
        <span>{inline(trimmed.replace(/^ℹ️?\s*/, ''))}</span>
      </p>
    );
  }
  return <p key={index}>{inline(trimmed)}</p>;
}

/** Headline figures pulled out of the tool result, when there are any. */
function KpiStrip({ values }: { values: Record<string, any> }) {
  const interesting = [
    ['net_sales', formatAmount],
    ['collection_amount', formatAmount],
    ['outstanding_amount', formatAmount],
    ['overdue_amount', formatAmount],
    ['target', formatAmount],
    ['actual', formatAmount],
    ['achievement_percent', formatPercent],
    ['growth_percent', (value: number) => formatPercent(value, { signed: true })],
  ] as const;

  const shown = interesting
    .filter(([key]) => values[key] !== undefined && values[key] !== null)
    .slice(0, 4);
  if (shown.length === 0) return null;

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {shown.map(([key, format]) => (
        <div
          key={key}
          className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700"
        >
          <p className="text-[11px] uppercase text-slate-500">{humanizeColumn(key)}</p>
          <p className="text-sm font-semibold tabular-nums">{format(values[key])}</p>
        </div>
      ))}
    </div>
  );
}

export function AiResponse({ response }: { response: ChatMessageResponse }) {
  const t = useT();
  const rows = (response.data?.rows ?? []) as Record<string, any>[];
  const values = (response.data?.values ?? {}) as Record<string, any>;
  const interpretations = (response.data?.interpretations ?? []) as string[];

  const isError = Boolean(response.error_code);

  return (
    <div className="space-y-3">
      {isError && (
        <div className="flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <MessageSquareWarning size={16} className="mt-0.5 shrink-0" />
          <span>{response.answer}</span>
        </div>
      )}

      {!isError && <AnswerText text={response.answer} />}

      {!isError && Object.keys(values).length > 0 && (
        <KpiStrip values={values} />
      )}

      {response.chart?.data?.length ? (
        <div className="rounded-lg border border-slate-200 p-2 dark:border-slate-700">
          <AutoChart spec={response.chart} height={220} />
        </div>
      ) : null}

      {rows.length > 0 && (
        <DataTable
          rows={rows}
          columns={columnsFromRows(rows)}
          searchable={false}
          pageSize={10}
          dense
          rowKey={(row, index) => String(row.code ?? row.material_code ?? index)}
        />
      )}

      {interpretations.length > 0 && (
        <div className="rounded-lg bg-slate-100 px-3 py-2 dark:bg-slate-800/60">
          <p className="mb-1 text-[11px] font-semibold uppercase text-slate-500">
            Interpretation
          </p>
          {interpretations.map((line) => (
            <p key={line} className="text-xs text-slate-600 dark:text-slate-300">
              {line}
            </p>
          ))}
        </div>
      )}

      {/*
        Assumptions are deliberately absent here. The backend already writes
        every one of them into the answer text as an `i` line, and that text is
        the whole answer on WhatsApp, on the CLI and in a conversation reloaded
        from history — so it has to carry them, and repeating them underneath
        printed each one twice: once at body size and once again in 11px grey.
        The duplicate was the quieter copy, which is what taught people to stop
        reading the line that says a filter or a period was assumed.

        Sources are not in that text, so they stay.
      */}
      {response.sources.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400">
          <span className="inline-flex items-center gap-1">
            <Database size={11} />
            {t('ai.sources')}: {response.sources.join(', ')}
          </span>
        </div>
      )}
    </div>
  );
}
