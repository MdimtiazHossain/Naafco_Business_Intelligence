/**
 * Chart wrappers over Recharts.
 *
 * Charts render values the backend already computed; they never aggregate.
 * All of them are responsive and reuse one palette so a series keeps its colour
 * across the app.
 */

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Label,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useEffect, useState } from 'react';
import { useTheme } from '../contexts/ThemeContext';
import {
  formatAmount,
  formatCell,
  formatPercent,
  formatQuantity,
  formatStock,
  humanizeColumn,
  isStockMeasureColumn,
  stockStatusOf,
  type StockStatus,
} from '../utils/format';
import { EmptyState } from '../components/States';

export const CHART_COLORS = [
  '#2563eb',
  '#0891b2',
  '#7c3aed',
  '#ea580c',
  '#059669',
  '#db2777',
  '#ca8a04',
  '#475569',
];

/**
 * The stock status colours, as a chart needs them.
 *
 * The same standard as the `.stock-status--*` classes in `index.css` — green
 * for the stock that can be sold, amber for what is running out of shelf life,
 * red for what has already lost it — expressed as fills, because an SVG bar
 * takes a colour and not a class. The two live apart for the same reason
 * `AGING_COLORS` sits here rather than in the stylesheet: this file is where a
 * chart's palette is decided. Keep them in step; these are the same weights the
 * classes apply — 700 for green and amber, 600 for red.
 */
export const STOCK_STATUS_COLORS: Record<StockStatus, { light: string; dark: string }> = {
  unrestricted: { light: '#047857', dark: '#34d399' },
  expiring: { light: '#b45309', dark: '#fbbf24' },
  expired: { light: '#dc2626', dark: '#f87171' },
};

/**
 * Aging buckets read best when the colour tracks severity: green for money
 * inside its terms, through yellow and orange, to deepening red for debt that
 * has been owed for a year.
 *
 * **Eight buckets, and the keys are the backend's own.** This table used to hold
 * six — `CURRENT`, `91-180`, `180+` — and had held them since revision 0020
 * removed the Outstanding module that was its only reader, so every key in it
 * named a bucket nothing produced. Credit Control returns receivables with a
 * finer split (a 120-day debt and a 179-day debt are chased by different
 * people), so the table is *replaced* rather than extended: not one of the old
 * keys survives in the new scheme, and leaving them would be six more names
 * outliving what they named.
 *
 * Keep these in step with `app.etl.credit.AGING_BUCKETS`. A bucket with no
 * colour here falls back to the palette's default, which is legible but breaks
 * the severity ramp — the one thing this table exists to provide.
 */
export const AGING_COLORS: Record<string, string> = {
  NOT_YET_DUE: '#059669',
  '1-30': '#65a30d',
  '31-60': '#ca8a04',
  '61-90': '#ea580c',
  '91-120': '#f97316',
  '121-180': '#dc2626',
  '181-365': '#b91c1c',
  '365+': '#7f1d1d',
};

/** How a chart's numbers should read. `stock` carries the MT unit with it. */
export type ChartValueKind = 'currency' | 'quantity' | 'stock';

interface BaseChartProps {
  data: Record<string, any>[];
  xKey: string;
  yKey: string;
  height?: number;
  /**
   * Quantities and counts must not be rendered as currency, and material stock
   * is a measured position rather than money — so a chart cannot show a stock
   * figure that looks like an amount in taka.
   */
  valueKind?: ChartValueKind;
  /**
   * What the plotted measure is called, for the tooltip.
   *
   * Without it Recharts falls back to the raw column name. It is how a stock
   * chart states its unit: the caller passes `Total Stock (KG/LTR)` and the
   * tooltip reads `Total Stock (KG/LTR): 12,500`, keeping the unit on the name
   * and off the number exactly as the cards and tables do.
   */
  valueLabel?: string;
  emptyMessage?: string;
}

function useChartTheme() {
  const { resolved } = useTheme();
  const dark = resolved === 'dark';
  return {
    grid: dark ? '#1e293b' : '#e2e8f0',
    axis: dark ? '#94a3b8' : '#64748b',
    tooltipBg: dark ? '#0f172a' : '#ffffff',
    tooltipBorder: dark ? '#334155' : '#e2e8f0',
    tooltipText: dark ? '#e2e8f0' : '#0f172a',
  };
}

function useValueFormatter(kind: ChartValueKind = 'currency') {
  return (value: number) => {
    if (kind === 'stock') return formatStock(value);
    if (kind === 'quantity') return formatQuantity(value);
    return formatAmount(value);
  };
}

/**
 * Whether the viewport is phone-width.
 *
 * A chart's axis configuration is JavaScript — Recharts measures and lays out
 * its ticks itself, and no media query can reach inside it — so this is the one
 * place the app asks the viewport a question rather than expressing the answer
 * in CSS. It is deliberately the cheapest form of asking: `matchMedia` fires
 * only when the breakpoint is actually crossed, so there is no resize listener,
 * no polling and no re-render while a reader is merely scrolling.
 *
 * The query is `max-width: 639px`, one pixel below Tailwind's `sm`, so tablets
 * and desktops take the exact configuration they take today and only a phone
 * gets the thinned-out one.
 */
const NARROW_QUERY = '(max-width: 639px)';

function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(NARROW_QUERY).matches,
  );

  useEffect(() => {
    const media = window.matchMedia(NARROW_QUERY);
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    setNarrow(media.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  return narrow;
}

/**
 * The x-axis settings for a categorical chart, at this width.
 *
 * `interval={0}` prints every category name. On a desktop that is what a
 * reader wants; on a 320px phone thirteen region names in the same space
 * collide into an unreadable smear, which is worse than showing fewer. So on a
 * phone Recharts is allowed to drop the labels that will not fit — the bars,
 * the tooltip and the underlying data are all untouched, and §11's instruction
 * is exactly this: reduce the visual density rather than remove the chart.
 *
 * The steeper angle and the shorter reserved height go with it: at -45° a name
 * needs less horizontal room per character, and 60px of axis out of a 260px
 * chart is a quarter of the plot given to labels that are no longer all drawn.
 */
function categoryAxis(count: number, narrow: boolean) {
  const crowded = count > 6;
  if (narrow) {
    return {
      interval: 'preserveStartEnd' as const,
      angle: crowded ? -45 : 0,
      textAnchor: crowded ? ('end' as const) : ('middle' as const),
      height: crowded ? 52 : 26,
    };
  }
  return {
    interval: 0 as const,
    angle: crowded ? -25 : 0,
    textAnchor: crowded ? ('end' as const) : ('middle' as const),
    height: crowded ? 60 : 30,
  };
}

/**
 * How much of the plot the value axis may reserve for its own labels.
 *
 * 70px is a fifth of a desktop chart and nearly a quarter of a phone one, where
 * the formatted amounts are the same length but the plot is a third the width.
 */
function valueAxisWidth(narrow: boolean) {
  // 56 rather than something tighter: a formatted amount is as long on a phone
  // as on a desktop, and below this "BDT 36.00 L" wraps onto two lines and the
  // axis stops reading as a column of numbers.
  return narrow ? 56 : 70;
}

/**
 * Shorten a category name so a rotated tick stays inside the plot.
 *
 * A material description — "Naafco Granular 40kg (1's)" — is thirty characters
 * of label under a bar 20px wide, and at -45 degrees it runs off the left edge
 * of the chart and is clipped mid-word, which reads as a rendering fault rather
 * than as a name too long to show. Truncating says the same thing deliberately,
 * and nothing is lost: the tooltip carries the full name, and so does the table
 * beneath every one of these charts.
 *
 * Desktop is untouched — it gets the name exactly as the backend sent it.
 */
function shortTick(value: unknown, narrow: boolean): string {
  const text = String(value ?? '');
  if (!narrow || text.length <= 14) return text;
  return `${text.slice(0, 13)}…`;
}

function tooltipStyle(theme: ReturnType<typeof useChartTheme>) {
  return {
    contentStyle: {
      backgroundColor: theme.tooltipBg,
      border: `1px solid ${theme.tooltipBorder}`,
      borderRadius: 8,
      fontSize: 12,
      color: theme.tooltipText,
    },
    labelStyle: { color: theme.tooltipText },
  };
}

/**
 * The lines a tool's chart spec asks for, coloured.
 *
 * One place, because four pages draw this chart and the target rule has to be
 * the same on all of them: **the target is dashed and takes the palette's
 * neutral**, while each year takes the next hue. Hue answers "which year", the
 * dash answers "plan or measurement" — a target in the next palette colour
 * would read as a fourth year, which is the one misreading this chart has to
 * avoid.
 *
 * A spec with no `series` is a single line over `y_axis`, which is every other
 * chart in the platform and is what a page gets before the backend starts
 * sending comparisons.
 */
export function trendSeriesFrom(
  chart: { y_axis: string; series?: { key: string; label: string }[] } | null | undefined,
  fallbackLabel?: string,
  targetLabel?: string,
): TrendSeries[] {
  if (!chart) return [];
  if (!chart.series?.length) {
    return [{ key: chart.y_axis, label: fallbackLabel ?? humanizeColumn(chart.y_axis) }];
  }
  return chart.series.map((line, index) => {
    const isTarget = line.key.startsWith('target');
    return {
      key: line.key,
      // `targetLabel` renames the target series, and a caller drawing one
      // should pass it. The tool labels every series with the *window* it
      // covers, so the target and this year's actual arrive carrying the same
      // string — a legend with two entries reading "FY 2026-27" and no way to
      // tell which is the plan. The colour and the dash already separate them;
      // this is what lets the name do so too, in the reader's own language.
      label: isTarget && targetLabel ? targetLabel : line.label,
      dashed: isTarget,
      color: isTarget ? CHART_COLORS[7] : CHART_COLORS[index % CHART_COLORS.length],
    };
  });
}

/**
 * The same series, ordered and coloured for the combo card's *bars*.
 *
 * Built on `trendSeriesFrom` rather than beside it, so the colour rule is
 * written once: a year keeps the hue it has on the Sales Trend line chart
 * directly above, and the target keeps the neutral. Two cards on one page
 * drawing the same series in different colours would be worse than either
 * alone — the hue is what tells a reader which year a bar is.
 *
 * The **order** is the card's own, and it is not the backend's: prior years
 * oldest-first, then Target, then this period's Actual, so a group of bars
 * reads left to right as history → plan → outcome. The rank comes from the
 * `_minus_N` suffix the tool puts on each earlier window, never from a
 * hard-coded `net_sales_minus_1`, so a third comparison year needs no change
 * here.
 *
 * `dashed` is dropped: it distinguishes a plan from a measurement on a *line*,
 * and a bar cannot carry it. On this chart the target's neutral colour is what
 * says the same thing.
 */
export function barSeriesFrom(
  chart: { y_axis: string; series?: { key: string; label: string }[] } | null | undefined,
  fallbackLabel?: string,
  targetLabel?: string,
): TrendSeries[] {
  /** How many whole years back this series is, or `null` for the current one. */
  const yearsBack = (key: string): number | null => {
    const match = /_minus_(\d+)$/.exec(key);
    return match ? Number(match[1]) : null;
  };
  const rank = (series: TrendSeries): number => {
    const back = yearsBack(series.key);
    if (back !== null) return -back;          // oldest first
    if (series.key.startsWith('target')) return 1;
    return 2;                                  // this period's actual, last
  };
  return trendSeriesFrom(chart, fallbackLabel, targetLabel)
    .map(({ dashed: _dashed, ...bar }) => bar)
    .sort((a, b) => rank(a) - rank(b));
}

/** One line of a trend. The same shape `ComparisonBarChart` takes for its bars. */
export interface TrendSeries {
  key: string;
  label: string;
  color?: string;
  /**
   * Draw this line dashed.
   *
   * For a series that is a different *kind* of thing rather than another of the
   * same thing — a target beside three years of actuals. Hue carries which
   * year; the dash carries plan-against-measurement. Giving the target a fourth
   * colour would read as a fourth year, which is the one misreading this chart
   * has to avoid.
   */
  dashed?: boolean;
}

/**
 * A line (or area) chart over one or more series.
 *
 * `series` is the only way in, deliberately: a component with two ways to say
 * the same thing grows a second behaviour on one of them, and the single-series
 * form was already the reason no chart here could draw a year against a year.
 *
 * **`connectNulls` is false and must stay false.** A month with no figures is a
 * gap — joining across it draws a straight line through months nobody measured
 * and reads as a real trend, which is the same absent-is-not-zero rule the
 * backend keeps by sending `null` rather than `0`.
 */
export function TrendChart({
  data,
  xKey,
  series,
  height = 260,
  valueKind = 'currency',
  emptyMessage,
  area = false,
}: Omit<BaseChartProps, 'yKey'> & { series: TrendSeries[]; area?: boolean }) {
  const theme = useChartTheme();
  const format = useValueFormatter(valueKind);
  const narrow = useNarrowViewport();
  if (!data?.length || !series.length) return <EmptyState message={emptyMessage} />;

  const Chart = area ? AreaChart : LineChart;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <Chart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} vertical={false} />
        <XAxis
          dataKey={xKey}
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={(value) => formatCell(xKey, value)}
          // A wider gap on a phone: dates are the same length whatever the
          // plot is, so the same 16px lets them touch on a narrow screen.
          minTickGap={narrow ? 28 : 16}
        />
        <YAxis
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={format}
          width={valueAxisWidth(narrow)}
        />
        <Tooltip formatter={(value) => format(Number(value))} {...tooltipStyle(theme)} />
        {/* A legend only where there is more than one line to tell apart. */}
        {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((line, index) => {
          const color = line.color ?? CHART_COLORS[index % CHART_COLORS.length];
          return area ? (
            <Area
              key={line.key}
              type="monotone"
              dataKey={line.key}
              name={line.label}
              stroke={color}
              fill={color}
              fillOpacity={0.15}
              strokeWidth={2}
              strokeDasharray={line.dashed ? '6 4' : undefined}
              connectNulls={false}
            />
          ) : (
            <Line
              key={line.key}
              type="monotone"
              dataKey={line.key}
              name={line.label}
              stroke={color}
              strokeWidth={2}
              strokeDasharray={line.dashed ? '6 4' : undefined}
              dot={false}
              connectNulls={false}
            />
          );
        })}
      </Chart>
    </ResponsiveContainer>
  );
}

export function CategoryBarChart({
  data,
  xKey,
  yKey,
  height = 260,
  valueKind = 'currency',
  valueLabel,
  emptyMessage,
  horizontal = false,
  colorByIndex = true,
  seriesColor,
}: BaseChartProps & {
  horizontal?: boolean;
  colorByIndex?: boolean;
  /**
   * One colour for every bar, overriding the per-category palette.
   *
   * For a series whose *measure* carries a meaning of its own — a stock status
   * — where a rainbow of categories would say the colour belongs to the plant
   * rather than to the status being plotted.
   */
  seriesColor?: string;
}) {
  const theme = useChartTheme();
  const format = useValueFormatter(valueKind);
  const narrow = useNarrowViewport();
  if (!data?.length) return <EmptyState message={emptyMessage} />;

  const xAxis = categoryAxis(data.length, narrow);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        layout={horizontal ? 'vertical' : 'horizontal'}
        // A tick rotated to -45 degrees extends down *and to the left* of the
        // tick it belongs to, and Recharts reserves height for it but not
        // width — so on a phone the first category's name was clipped by the
        // left edge of the SVG. The extra margin is that missing room.
        margin={{
          top: 8,
          right: 12,
          bottom: 4,
          left: horizontal ? 16 : narrow ? 14 : 4,
        }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
        {horizontal ? (
          <>
            <XAxis
              type="number"
              tick={{ fontSize: 11, fill: theme.axis }}
              tickFormatter={format}
            />
            <YAxis
              type="category"
              dataKey={xKey}
              tick={{ fontSize: 11, fill: theme.axis }}
              // 110px of a 294px phone plot is over a third of it given to
              // names; 88 keeps a territory name readable and gives the bars
              // back the room that makes the comparison legible at all.
              width={narrow ? 88 : 110}
              tickFormatter={(value) => shortTick(value, narrow)}
            />
          </>
        ) : (
          <>
            <XAxis
              dataKey={xKey}
              tick={{ fontSize: 11, fill: theme.axis }}
              tickFormatter={(value) => shortTick(value, narrow)}
              {...xAxis}
            />
            <YAxis
              tick={{ fontSize: 11, fill: theme.axis }}
              tickFormatter={format}
              width={valueAxisWidth(narrow)}
            />
          </>
        )}
        <Tooltip formatter={(value) => format(Number(value))} {...tooltipStyle(theme)} />
        <Bar dataKey={yKey} name={valueLabel} radius={[4, 4, 0, 0]}>
          {data.map((row, index) => (
            <Cell
              key={index}
              fill={
                seriesColor ??
                (colorByIndex
                  ? CHART_COLORS[index % CHART_COLORS.length]
                  : (AGING_COLORS[String(row[xKey])] ?? CHART_COLORS[0]))
              }
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Two series side by side — used for target vs actual. */
export function ComparisonBarChart({
  data,
  xKey,
  series,
  height = 280,
  emptyMessage,
}: {
  data: Record<string, any>[];
  xKey: string;
  series: { key: string; label: string; color?: string }[];
  height?: number;
  emptyMessage?: string;
}) {
  const theme = useChartTheme();
  const narrow = useNarrowViewport();
  if (!data?.length) return <EmptyState message={emptyMessage} />;

  const xAxis = categoryAxis(data.length, narrow);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={data}
        margin={{ top: 8, right: 12, bottom: 4, left: narrow ? 14 : 4 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} vertical={false} />
        <XAxis
          dataKey={xKey}
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={(value) => shortTick(value, narrow)}
          {...xAxis}
        />
        <YAxis
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={(value) => formatAmount(value)}
          width={valueAxisWidth(narrow)}
        />
        <Tooltip
          formatter={(value) => formatAmount(Number(value))}
          {...tooltipStyle(theme)}
        />
        {/* On a phone the rotated category names occupy the bottom of the
            chart, and a legend placed there landed on top of them. Above the
            plot it collides with nothing and is read before the bars rather
            than after, which is if anything the better order. */}
        <Legend
          wrapperStyle={{ fontSize: 12 }}
          verticalAlign={narrow ? 'top' : 'bottom'}
        />
        {series.map((entry, index) => (
          <Bar
            key={entry.key}
            dataKey={entry.key}
            name={entry.label}
            fill={entry.color ?? CHART_COLORS[index % CHART_COLORS.length]}
            radius={[4, 4, 0, 0]}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Money as grouped bars, ratios as lines on a second axis.
 *
 * The one chart in this platform that plots two *kinds* of quantity at once, so
 * the axes carry the whole burden of not being misread: taka on the left,
 * percent on the right, both labelled, and **neither ever hidden** — a 90%
 * achievement line read against a scale that tops out at nine crore is a
 * catastrophic misreading and the reader would have no way to notice it.
 *
 * The formatter is chosen per *series* rather than per chart, because the
 * tooltip is where the two kinds meet: one entry reads `BDT 3.86 Cr` and the
 * one under it reads `48.7%`, and a single chart-wide formatter would have to
 * be wrong about one of them.
 *
 * `connectNulls={false}` on the lines, for the reason it is false everywhere
 * else here: a region absent from the comparison window has no growth, and
 * joining across it draws a slope nobody measured.
 */
export function ComboBarLineChart({
  data,
  xKey,
  bars,
  lines,
  height = 340,
  emptyMessage,
  valueAxisLabel,
  percentAxisLabel,
  xTickFormatter,
  showLineValues = false,
}: {
  data: Record<string, any>[];
  xKey: string;
  bars: TrendSeries[];
  lines: TrendSeries[];
  height?: number;
  emptyMessage?: string;
  /** Rotated title on the money axis, e.g. "Net Sales / Target (BDT)". */
  valueAxisLabel?: string;
  /** Rotated title on the percentage axis, e.g. "Achievement / Growth (%)". */
  percentAxisLabel?: string;
  /** Shorten a category name — a month label that need not repeat its year. */
  xTickFormatter?: (value: string) => string;
  /** Print each line's own reading above its point. */
  showLineValues?: boolean;
}) {
  const theme = useChartTheme();
  // The same money formatter every other chart here uses, so a figure reads
  // identically on the axis, in the tooltip and on the card above.
  const format = useValueFormatter();
  const narrow = useNarrowViewport();
  if (!data?.length) return <EmptyState message={emptyMessage} />;

  const shortenX = (value: unknown) =>
    xTickFormatter ? xTickFormatter(String(value ?? '')) : String(value ?? '');
  // `categoryAxis` rotates anything past six categories, which is right for a
  // region name and wrong for "Jul". The decision is really about how wide the
  // labels are, so where the caller has shortened them to a few characters the
  // rotation is dropped: twelve months sit level across the axis and read as a
  // year rather than as a fan of diagonals.
  const shortLabels = data.every((row) => shortenX(row[xKey]).length <= 4);
  const xAxis = shortLabels
    ? { ...categoryAxis(data.length, narrow), angle: 0,
        textAnchor: 'middle' as const, height: narrow ? 26 : 30 }
    : categoryAxis(data.length, narrow);
  const percentKeys = new Set(lines.map((line) => line.key));
  const barColor = (bar: TrendSeries, index: number) =>
    bar.color ?? CHART_COLORS[index % CHART_COLORS.length];
  const lineColor = (line: TrendSeries, index: number) =>
    line.color ?? CHART_COLORS[(bars.length + index) % CHART_COLORS.length];
  // An axis title costs room the plot would otherwise have, so it is only
  // asked for on a card whose two axes measure different kinds of thing.
  const axisTitleRoom = (title: string | undefined) => (title ? 18 : 0);

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart
        data={data}
        margin={{ top: showLineValues ? 20 : 8, right: 12, bottom: 4,
                  left: narrow ? 14 : 4 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} vertical={false} />
        <XAxis
          dataKey={xKey}
          tick={{ fontSize: 11, fill: theme.axis }}
          // The caller shortens first — a month need not repeat the year the
          // page header already states — and the phone rule truncates after.
          tickFormatter={(value) => shortTick(shortenX(value), narrow)}
          {...xAxis}
        />
        <YAxis
          yAxisId="value"
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={(value) => format(Number(value))}
          width={valueAxisWidth(narrow) + axisTitleRoom(valueAxisLabel)}
        >
          {valueAxisLabel ? (
            <Label
              value={valueAxisLabel}
              angle={-90}
              position="insideLeft"
              style={{ fontSize: 11, fill: theme.axis, textAnchor: 'middle' }}
            />
          ) : null}
        </YAxis>
        {/* Kept at full width on a phone too. It is narrower content than the
            money axis, and dropping it to save room would leave the lines
            plotted against nothing a reader could check them against. */}
        <YAxis
          yAxisId="percent"
          orientation="right"
          tick={{ fontSize: 11, fill: theme.axis }}
          tickFormatter={(value) => formatPercent(Number(value))}
          width={(narrow ? 44 : 52) + axisTitleRoom(percentAxisLabel)}
        >
          {percentAxisLabel ? (
            <Label
              value={percentAxisLabel}
              angle={90}
              position="insideRight"
              style={{ fontSize: 11, fill: theme.axis, textAnchor: 'middle' }}
            />
          ) : null}
        </YAxis>
        <Tooltip
          // Per series: the bars are money and the lines are ratios, and one
          // formatter for both would misreport whichever it was not written for.
          formatter={(value, _name, entry) =>
            percentKeys.has(String((entry as { dataKey?: string })?.dataKey))
              ? formatPercent(Number(value))
              : format(Number(value))
          }
          {...tooltipStyle(theme)}
        />
        {/* Same placement rule as ComparisonBarChart: on a phone the rotated
            category names own the bottom of the plot.

            The legend is rendered here rather than left to Recharts, which
            orders it by the order each series registers itself and put the two
            lines in among the bars — an order that is neither the drawing order
            nor any other a reader could name. Bars first, in the order they are
            drawn, then the lines; a square for a bar and a line-with-dot for a
            line, so the mark says which kind of series it names. (Recharts'
            own `payload` prop is not on the public Legend type in this
            version, so `content` is the supported way to say this.) */}
        <Legend
          wrapperStyle={{ fontSize: 12 }}
          verticalAlign={narrow ? 'top' : 'bottom'}
          content={() => (
            <ul style={{
              display: 'flex', flexWrap: 'wrap', justifyContent: 'center',
              gap: '4px 14px', listStyle: 'none', margin: 0, padding: 0,
              color: theme.tooltipText, fontSize: 12,
            }}>
              {[
                ...bars.map((bar, index) => ({
                  key: bar.key, label: bar.label, colour: barColor(bar, index),
                  line: false,
                })),
                ...lines.map((line, index) => ({
                  key: line.key, label: line.label, colour: lineColor(line, index),
                  line: true,
                })),
              ].map((item) => (
                <li key={item.key}
                    style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  {item.line ? (
                    <svg width="18" height="10" aria-hidden="true">
                      <line x1="0" y1="5" x2="18" y2="5"
                            stroke={item.colour} strokeWidth="2" />
                      <circle cx="9" cy="5" r="3.5" fill={theme.tooltipBg}
                              stroke={item.colour} strokeWidth="2" />
                    </svg>
                  ) : (
                    <span style={{
                      width: 10, height: 10, borderRadius: 2,
                      backgroundColor: item.colour, display: 'inline-block',
                    }} />
                  )}
                  {item.label}
                </li>
              ))}
            </ul>
          )}
        />
        {bars.map((bar, index) => (
          <Bar
            key={bar.key}
            yAxisId="value"
            dataKey={bar.key}
            name={bar.label}
            fill={barColor(bar, index)}
            radius={[4, 4, 0, 0]}
          />
        ))}
        {lines.map((line, index) => (
          <Line
            key={line.key}
            yAxisId="percent"
            type="monotone"
            dataKey={line.key}
            name={line.label}
            stroke={lineColor(line, index)}
            strokeWidth={2}
            strokeDasharray={line.dashed ? '6 4' : undefined}
            // A dot per category, unlike the trend: these are discrete readings
            // rather than a continuum, so the point *is* the reading. Hollow,
            // so a printed value above it stays legible against the marker.
            dot={{ r: 4, strokeWidth: 2, fill: theme.tooltipBg }}
            connectNulls={false}
          >
            {showLineValues ? (
              <LabelList
                dataKey={line.key}
                position="top"
                offset={8}
                style={{ fontSize: 10, fill: lineColor(line, index),
                         fontWeight: 600 }}
                // A null is not labelled at all. Recharts hands the formatter
                // every point including the ones with no reading, and "n/a"
                // printed over a gap would put a label where the line
                // deliberately breaks.
                formatter={(value: unknown) =>
                  value === null || value === undefined
                    ? '' : formatPercent(Number(value), { decimals: 0 })}
              />
            ) : null}
          </Line>
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function DonutChart({
  data,
  xKey,
  yKey,
  height = 260,
  emptyMessage,
  useAgingColors = false,
}: BaseChartProps & { useAgingColors?: boolean }) {
  const theme = useChartTheme();
  if (!data?.length) return <EmptyState message={emptyMessage} />;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <PieChart>
        <Pie
          data={data}
          dataKey={yKey}
          nameKey={xKey}
          innerRadius="55%"
          outerRadius="80%"
          paddingAngle={2}
        >
          {data.map((row, index) => (
            <Cell
              key={index}
              fill={
                useAgingColors
                  ? (AGING_COLORS[String(row[xKey])] ?? CHART_COLORS[index % CHART_COLORS.length])
                  : CHART_COLORS[index % CHART_COLORS.length]
              }
            />
          ))}
        </Pie>
        <Tooltip
          formatter={(value) => formatAmount(Number(value))}
          {...tooltipStyle(theme)}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
      </PieChart>
    </ResponsiveContainer>
  );
}

// No `VolumeByUnitChart`. It drew one chart per unit of measure, because a
// kilogram bar and a litre bar share no axis. A transaction line now states one
// Total Volume and no unit, so volume is plotted with `CategoryBarChart` like
// any other measure.

/** Render whatever chart the backend asked for, safely. */
export function AutoChart({
  spec,
  height = 260,
}: {
  spec: {
    type: string;
    x_axis: string;
    y_axis: string;
    data: Record<string, any>[];
    series?: { key: string; label: string }[];
  };
  height?: number;
}) {
  const { resolved } = useTheme();
  // The agent's charts follow the same two stock rules as every other surface:
  // a stock measure is never rendered as taka, and it takes its unit in the
  // series name (which is what the tooltip reads) rather than beside the
  // figure. `humanizeColumn` supplies that name, unit included.
  const isStock = isStockMeasureColumn(spec.y_axis);
  const status = stockStatusOf(spec.y_axis);
  const props = {
    data: spec.data,
    xKey: spec.x_axis === 'date' ? 'date' : spec.x_axis,
    yKey: spec.y_axis,
    height,
    ...(isStock
      ? { valueKind: 'stock' as ChartValueKind, valueLabel: humanizeColumn(spec.y_axis) }
      : {}),
  };
  const lines = trendSeriesFrom(spec);

  switch (spec.type) {
    case 'line':
      return <TrendChart {...props} series={lines} />;
    case 'area':
      return <TrendChart {...props} series={lines} area />;
    case 'pie':
      return <DonutChart {...props} useAgingColors />;
    default:
      return (
        <CategoryBarChart
          {...props}
          // A chart *of* a stock status wears that status's colour rather than
          // the per-category palette — the same green or amber the cards and
          // tables use for the measure being plotted.
          seriesColor={
            status
              ? STOCK_STATUS_COLORS[status][resolved === 'dark' ? 'dark' : 'light']
              : undefined
          }
        />
      );
  }
}
