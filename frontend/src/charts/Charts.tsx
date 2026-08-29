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

/** Aging buckets read best when the colour tracks severity. */
export const AGING_COLORS: Record<string, string> = {
  CURRENT: '#059669',
  '1-30': '#65a30d',
  '31-60': '#ca8a04',
  '61-90': '#ea580c',
  '91-180': '#dc2626',
  '180+': '#991b1b',
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

export function TrendChart({
  data,
  xKey,
  yKey,
  height = 260,
  valueKind = 'currency',
  emptyMessage,
  area = false,
}: BaseChartProps & { area?: boolean }) {
  const theme = useChartTheme();
  const format = useValueFormatter(valueKind);
  const narrow = useNarrowViewport();
  if (!data?.length) return <EmptyState message={emptyMessage} />;

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
        {area ? (
          <Area
            type="monotone"
            dataKey={yKey}
            stroke={CHART_COLORS[0]}
            fill={CHART_COLORS[0]}
            fillOpacity={0.15}
            strokeWidth={2}
          />
        ) : (
          <Line
            type="monotone"
            dataKey={yKey}
            stroke={CHART_COLORS[0]}
            strokeWidth={2}
            dot={false}
          />
        )}
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
  spec: { type: string; x_axis: string; y_axis: string; data: Record<string, any>[] };
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
  switch (spec.type) {
    case 'line':
      return <TrendChart {...props} />;
    case 'area':
      return <TrendChart {...props} area />;
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
