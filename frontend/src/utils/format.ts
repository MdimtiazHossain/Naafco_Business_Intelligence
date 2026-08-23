/**
 * Display formatting.
 *
 * These are *presentation* helpers only — they never derive a business figure.
 * Every value they format was computed by the backend; growth, margin and
 * achievement all arrive already calculated.
 *
 * Currency follows Bangladeshi convention: ৳ with lakh/crore units and Indian
 * digit grouping for raw values.
 */

export const TAKA = '৳';
const CRORE = 10_000_000;
const LAKH = 100_000;
const THOUSAND = 1_000;

/** `18700000` -> `1,87,00,000` (Indian grouping). */
export function groupIndian(value: number): string {
  const negative = value < 0;
  const digits = Math.abs(Math.round(value)).toString();
  let grouped: string;
  if (digits.length <= 3) {
    grouped = digits;
  } else {
    const tail = digits.slice(-3);
    let head = digits.slice(0, -3);
    const parts: string[] = [];
    while (head.length > 2) {
      parts.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) parts.unshift(head);
    grouped = `${parts.join(',')},${tail}`;
  }
  return negative ? `-${grouped}` : grouped;
}

/** Money for humans: `৳1.87 Cr`. A missing value is an em dash, never `0`. */
export function formatAmount(value: number | null | undefined, compact = true): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (!compact) return `${TAKA}${groupIndian(value)}`;

  const magnitude = Math.abs(value);
  if (magnitude >= CRORE) return `${TAKA}${(value / CRORE).toFixed(2)} Cr`;
  if (magnitude >= LAKH) return `${TAKA}${(value / LAKH).toFixed(2)} L`;
  if (magnitude >= THOUSAND) return `${TAKA}${(value / THOUSAND).toFixed(1)} K`;
  return `${TAKA}${value.toFixed(0)}`;
}

/** A ratio the backend could not compute shows as `n/a`, never as `0%`. */
export function formatPercent(
  value: number | null | undefined,
  options: { signed?: boolean; decimals?: number } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  const { signed = false, decimals = 1 } = options;
  const rendered = value.toFixed(decimals);
  return signed && value > 0 ? `+${rendered}%` : `${rendered}%`;
}

export function formatQuantity(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number.isInteger(value)
    ? value.toLocaleString('en-IN')
    : value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

/**
 * The unit every Material Stock figure is reported in.
 *
 * `KG/LTR`, on the business's stated convention: the Material Transaction Data
 * carries four quantities and no unit of measure, and a plant holds stock in
 * kilograms or in litres depending on the material — the source never says
 * which, so the label names both rather than claiming one. Declared once here
 * and read by every stock surface — cards, charts, tables, tooltips, exports —
 * so no screen can label the same number differently.
 *
 * **The unit belongs to the label, not to the number.** A stock figure reads
 * `Unrestricted Stock (KG/LTR)` above `125,500`, never `125,500 KG/LTR`: the
 * name says what is being measured and in what, and the cell underneath is
 * then a plain figure that lines up with every other figure in its column.
 * `stockLabel` builds the name and `formatStock` renders the number, and the
 * two are always used together.
 *
 * **Nothing is converted.** There is no factor in the source to convert *with*,
 * so the figure shown is the figure uploaded; this constant names it rather
 * than transforming it. It is also why no stock table carries a UOM column: a
 * per-row unit would have to be invented, and naming both units in the heading
 * is the honest answer instead.
 */
export const STOCK_UNIT = 'KG/LTR';

/** `Unrestricted Stock` -> `Unrestricted Stock (KG/LTR)`. The unit lives here. */
export function stockLabel(name: string): string {
  return `${name} (${STOCK_UNIT})`;
}

/**
 * The stock columns that are a stock figure, and so take the unit in their
 * heading. `stock_source` and `stock_coverage_days` are not among them — one is
 * a provenance string and the other is a duration, and neither is in KG/LTR.
 */
const STOCK_MEASURE_COLUMNS = new Set([
  'unrestricted_stock',
  'quality_inspection_stock',
  'blocked_stock',
  'stock_in_transit',
  'total_stock',
  'expired_stock',
  'expiring_soon_stock',
]);

/**
 * A material stock figure: the uploaded value, formatted and nothing else.
 *
 * No unit is appended — it is on the label; see `STOCK_UNIT`. This stays a
 * function of its own rather than folding into `formatQuantity` because the two
 * answer different questions: a sales quantity is a case count, and this is a
 * plant's position in KG/LTR that must never be converted or re-based.
 */
export function formatStock(value: number | null | undefined): string {
  return formatQuantity(value);
}

/**
 * A volume, with its unit attached.
 *
 * The unit is never dropped and never converted. 500 KG and 250 LTR are two
 * different measurements, and a number without its unit is what makes them
 * look addable.
 */
export function formatVolume(
  value: number | null | undefined,
  unit?: string | null,
): string {
  const amount = formatQuantity(value);
  if (amount === '—' || !unit) return amount;
  return `${amount} ${unit}`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Math.round(value).toLocaleString('en-IN');
}

export function formatDays(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  return `${value.toFixed(1)} d`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${formatDate(value)} ${date.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
  })}`;
}

/** Format by the semantic type the backend declared for a KPI. */
export function formatByKind(
  value: number | null | undefined,
  kind: 'currency' | 'percent' | 'count' | 'quantity' | 'volume' | 'stock',
  unit?: string | null,
): string {
  switch (kind) {
    case 'currency':
      return formatAmount(value);
    case 'percent':
      return formatPercent(value);
    case 'quantity':
      return formatQuantity(value);
    case 'stock':
      return formatStock(value);
    case 'volume':
      return formatVolume(value, unit);
    default:
      return formatCount(value);
  }
}

/**
 * Format a managed record's field by its declared storage kind.
 *
 * Distinct from `formatCell`, which guesses from the column name: here the
 * entity has already said what the field *is*, and a declared type beats a
 * heuristic. It matters most for codes — `formatCell` would see a numeric-
 * looking customer code and render it as a number, losing leading zeros.
 */
export function formatFieldValue(kind: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  switch (kind) {
    case 'code':
    case 'phone':
      return String(value);
    case 'date':
    case 'timestamp':
      return formatDate(String(value));
    case 'decimal':
    case 'numeric':
      return formatQuantity(Number(value));
    case 'integer':
      return formatCount(Number(value));
    default:
      return String(value);
  }
}

/** Column name -> a sensible formatter, used by tables and charts. */
export function formatCell(column: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value !== 'number') {
    const text = String(value);
    return /^\d{4}-\d{2}-\d{2}/.test(text) ? formatDate(text) : text;
  }

  const name = column.toLowerCase();
  // A ranking position is a plain ordinal — not money, not a measure.
  if (name === 'rank') return String(Math.round(value));
  if (/(percent|_pct|margin|achievement|growth|share)/.test(name)) {
    return formatPercent(value, { signed: /growth|change/.test(name) });
  }
  if (/(coverage_days|days_overdue|max_days)/.test(name)) {
    return name.includes('coverage') ? formatDays(value) : `${Math.round(value)} d`;
  }
  if (/(count|invoices|materials)/.test(name)) return formatCount(value);
  // Material stock is a measured position, never money — so it is tested before
  // the currency fallback below, and the *target* columns are excluded because
  // they are a planned figure against a material rather than a position in a
  // storage location. The unit is not added here: `humanizeColumn` puts it in
  // the heading, so a column of stock cells is a clean column of numbers.
  if (/stock/.test(name) && !/^target_/.test(name)) return formatStock(value);
  // Volume is a measured amount, never currency, and it carries no unit — a
  // sales line and a target both state a plain number and the system has no
  // unit of measure to attach to either.
  if (/(quantity|qty|volume|opening|closing|transfer|purchase)/.test(name)) {
    return formatQuantity(value);
  }
  return formatAmount(value);
}

/** Right-align anything numeric so columns of figures line up. */
export function isNumericColumn(column: string, sample: unknown): boolean {
  if (typeof sample === 'number') return true;
  return /(amount|sales|profit|cost|discount|target|gap|percent|qty|quantity|volume|stock|count|days|rank)/.test(
    column.toLowerCase(),
  );
}

/** `net_sales` -> `Net Sales`, for columns the backend did not label. */
export function humanizeColumn(column: string): string {
  const name = column
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
    .replace(/\bBu\b/g, 'BU')
    .replace(/\bAsp\b/g, 'ASP')
    // "Stock In Transit" is the one phrase this capitalisation gets wrong: it is
    // a preposition, not a word of the name.
    .replace(/\bIn Transit\b/g, 'in Transit');
  // A stock figure takes its unit in the heading — the counterpart of
  // `formatCell` rendering the cell as a bare number. Every table that derives
  // its headers rather than naming them lands here, so the pairing holds
  // wherever a stock column appears without being asked for by name.
  return STOCK_MEASURE_COLUMNS.has(column) ? stockLabel(name) : name;
}

/** The three material stock measures the application colours by status. */
export type StockStatus = 'unrestricted' | 'expiring' | 'expired';

/**
 * Metric key or shelf-life bucket -> its stock status.
 *
 * The three the business colours are unrestricted stock (green, the only stock
 * that can be sold), expiring-soon stock (amber, a warning with time left to
 * act on it) and expired stock (red, money already lost). Both spellings of
 * each are listed: a KPI or a table column names it `expiring_soon_stock`,
 * while a row of the expiry breakdown names it by its bucket code
 * `EXPIRING_SOON`, and the same colour has to answer to both.
 *
 * Anything not listed here has no status — a total, a quality-inspection figure
 * or any sales measure keeps the neutral slate the rest of the application
 * uses, and must not be coloured as if it were a status.
 */
const STOCK_STATUSES: Record<string, StockStatus> = {
  unrestricted_stock: 'unrestricted',
  expiring_soon_stock: 'expiring',
  expired_stock: 'expired',
  EXPIRING_SOON: 'expiring',
  EXPIRED: 'expired',
};

/** The stock status a metric key carries, or `undefined` if it carries none. */
export function stockStatusOf(key: string | null | undefined): StockStatus | undefined {
  return key ? STOCK_STATUSES[key] : undefined;
}

/**
 * The colour class for a stock status metric, or `undefined` for anything else.
 *
 * The class is applied to the label *and* to the value — the classes themselves
 * are defined in `index.css`, which is what makes the colour one decision
 * rather than one per component. Callers pass the metric's key rather than its
 * translated name, so the colour holds in Bangla exactly as it does in English.
 */
export function stockStatusClass(key: string | null | undefined): string | undefined {
  const status = stockStatusOf(key);
  return status ? `stock-status--${status}` : undefined;
}

/** Is this column a material stock figure — the ones whose heading takes the unit? */
export function isStockMeasureColumn(column: string): boolean {
  return STOCK_MEASURE_COLUMNS.has(column);
}

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  HIGH: 'bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300',
  MEDIUM: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-300',
  LOW: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-950 dark:text-cyan-300',
};

export function severityClass(severity: string): string {
  return SEVERITY_STYLES[severity] ?? 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300';
}

const STATUS_STYLES: Record<string, string> = {
  OUT_OF_STOCK: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  CRITICAL: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  WARNING: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-300',
  NORMAL: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  COMPLETED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  COMPLETED_WITH_ERRORS:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-300',
  FAILED: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
};

export function statusClass(status: string): string {
  return STATUS_STYLES[status] ?? 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300';
}

/** Growth colour: green up, red down, neutral when unknown. */
export function growthClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return 'text-slate-500 dark:text-slate-400';
  }
  if (value > 0) return 'text-emerald-600 dark:text-emerald-400';
  if (value < 0) return 'text-red-600 dark:text-red-400';
  return 'text-slate-500 dark:text-slate-400';
}
