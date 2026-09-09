/**
 * What the region combo card is asked to draw.
 *
 * Recharts is stubbed rather than rendered, for the reason `trendChart.test.tsx`
 * gives: what matters is the props each series is handed, and an SVG assertion
 * would test the library. Three of them fail invisibly on a chart that still
 * looks right — money and percentages plotted on one axis, a joined null, and a
 * target bar wearing a colour a real measurement also wears.
 */

import { render } from '@testing-library/react';
import { ThemeProvider } from '../contexts/ThemeContext';
import { describe, expect, it, vi } from 'vitest';

/** Every rendered Recharts element, by component name. */
const drawn: Record<string, Record<string, any>[]> = {};

vi.mock('recharts', () => {
  const passthrough = (name: string) => {
    const Component = (props: any) => {
      (drawn[name] ??= []).push(props);
      return <div data-testid={name}>{props.children}</div>;
    };
    Component.displayName = name;
    return Component;
  };
  return {
    ResponsiveContainer: passthrough('ResponsiveContainer'),
    ComposedChart: passthrough('ComposedChart'),
    LineChart: passthrough('LineChart'),
    AreaChart: passthrough('AreaChart'),
    BarChart: passthrough('BarChart'),
    PieChart: passthrough('PieChart'),
    Line: passthrough('Line'),
    Area: passthrough('Area'),
    Bar: passthrough('Bar'),
    Pie: passthrough('Pie'),
    Cell: passthrough('Cell'),
    XAxis: passthrough('XAxis'),
    YAxis: passthrough('YAxis'),
    CartesianGrid: passthrough('CartesianGrid'),
    Tooltip: passthrough('Tooltip'),
    Legend: passthrough('Legend'),
    Label: passthrough('Label'),
    LabelList: passthrough('LabelList'),
    ReferenceLine: passthrough('ReferenceLine'),
  };
});

const { ComboBarLineChart, barSeriesFrom, trendSeriesFrom, seriesHues,
        CHART_COLORS } = await import('../charts/Charts');

const ROWS = [
  { label: 'Dhaka', target_amount: 2_000_000, actual_sales: 1_500_000,
    previous_sales: 2_000_000, achievement_percent: 75, growth_percent: -25 },
  // Khulna opened this year: no last-period figure, so no growth. The backend
  // sends null for both rather than 0 and -100, and the chart has to keep it.
  { label: 'Khulna', target_amount: 1_000_000, actual_sales: 300_000,
    previous_sales: null, achievement_percent: 30, growth_percent: null },
];

const BARS = [
  { key: 'target_amount', label: 'Target', color: CHART_COLORS[7] },
  { key: 'actual_sales', label: 'Actual' },
  { key: 'previous_sales', label: 'Last-period Actual' },
];
const LINES = [
  { key: 'achievement_percent', label: 'Achievement %' },
  { key: 'growth_percent', label: 'Growth %' },
];

function draw() {
  for (const key of Object.keys(drawn)) delete drawn[key];
  render(
    <ThemeProvider>
      <ComboBarLineChart data={ROWS} xKey="label" bars={BARS} lines={LINES} />
    </ThemeProvider>,
  );
  return drawn;
}

describe('the region combo card', () => {
  it('puts money on the left axis and percentages on the right', () => {
    // The load-bearing one. An achievement of 75 shares an axis with 2,000,000
    // only by becoming invisible, and a percentage that rounds to nothing at the
    // foot of a money axis reads as a region with no achievement at all.
    const chart = draw();
    expect(chart.Bar.map((bar) => bar.yAxisId)).toEqual(['value', 'value', 'value']);
    expect(chart.Line.map((line) => line.yAxisId)).toEqual(['percent', 'percent']);

    const axes = chart.YAxis.map((axis) => [axis.yAxisId, axis.orientation]);
    expect(axes).toEqual([['value', undefined], ['percent', 'right']]);
  });

  it('draws each series from the column it names', () => {
    const chart = draw();
    expect(chart.Bar.map((bar) => bar.dataKey)).toEqual([
      'target_amount', 'actual_sales', 'previous_sales',
    ]);
    expect(chart.Line.map((line) => line.dataKey)).toEqual([
      'achievement_percent', 'growth_percent',
    ]);
    expect(chart.Bar.map((bar) => bar.name)).toEqual([
      'Target', 'Actual', 'Last-period Actual',
    ]);
  });

  it('never joins a line across a null', () => {
    // Khulna has no growth. Joining would draw a segment straight through it,
    // which reads as a measured value rather than as the gap it is.
    expect(draw().Line.every((line) => line.connectNulls === false)).toBe(true);
  });

  it('gives the target the neutral colour and no measurement shares it', () => {
    // The target is the plan; the other two bars and the two lines are
    // measurements. A target in the next palette colour reads as a third
    // measured series.
    const chart = draw();
    expect(chart.Bar[0].fill).toBe(CHART_COLORS[7]);

    const colours = [
      ...chart.Bar.slice(1).map((bar) => bar.fill),
      ...chart.Line.map((line) => line.stroke),
    ];
    expect(colours).not.toContain(CHART_COLORS[7]);
    // And no two series share one, or the legend would name two things once.
    expect(new Set(colours).size).toBe(colours.length);
  });

  it('reports a bar as money and a line as a ratio', () => {
    // One formatter for both would misreport whichever it was not written for:
    // an achievement of 75 rendered as ৳75, or 1.5 crore rendered as 1500000.0%.
    const { formatter } = draw().Tooltip[0];
    expect(formatter(1_500_000, 'Actual', { dataKey: 'actual_sales' }))
      .toBe('৳15.00 L');
    expect(formatter(75, 'Achievement %', { dataKey: 'achievement_percent' }))
      .toBe('75%');
  });
});

/**
 * The monthly card's bars, named from the backend's own series list.
 *
 * The shape `get_sales_trend` sends for a monthly window with two comparison
 * years and a target — current year first, then the earlier windows, then the
 * target, which is the order the *tool* declares and not the order the card
 * draws.
 */
const TREND_CHART = {
  type: 'line',
  x_axis: 'label',
  y_axis: 'net_sales',
  series: [
    { key: 'net_sales', label: 'Jul 2026 - Sep 2026' },
    { key: 'net_sales_minus_1', label: 'Jul 2025 - Sep 2025' },
    { key: 'net_sales_minus_2', label: 'Jul 2024 - Sep 2024' },
    { key: 'target_amount', label: 'Jul 2026 - Sep 2026' },
  ],
};

describe('the monthly card reads the backend series list', () => {
  it('orders the bars history, then plan, then outcome', () => {
    // Left to right: the oldest year first and this period's actual last, so a
    // group of bars reads as what happened, what was planned, what happened.
    expect(barSeriesFrom(TREND_CHART).map((bar) => bar.key)).toEqual([
      'net_sales_minus_2',
      'net_sales_minus_1',
      'target_amount',
      'net_sales',
    ]);
  });

  it('keeps every series the hue it has on the line chart above', () => {
    // Two cards on one page drawing the same year in two colours would be
    // worse than either alone — the hue is what says which year a bar is.
    const lines = new Map(trendSeriesFrom(TREND_CHART).map((s) => [s.key, s.color]));
    for (const bar of barSeriesFrom(TREND_CHART)) {
      expect(bar.color).toBe(lines.get(bar.key));
    }
    expect(lines.get('target_amount')).toBe(CHART_COLORS[7]);
  });

  it('drops the dash, which a bar cannot carry', () => {
    // On a line the dash says plan-against-measurement; on this chart the
    // target's neutral colour says it instead.
    expect(barSeriesFrom(TREND_CHART).every((bar) => bar.dashed === undefined))
      .toBe(true);
    expect(trendSeriesFrom(TREND_CHART).find((s) => s.key === 'target_amount')?.dashed)
      .toBe(true);
  });

  it('falls back to one series when the tool sent no list', () => {
    // A window too short for months: one actual series, no target, no
    // comparison — and the card is still drawn rather than vanishing.
    const single = barSeriesFrom({ y_axis: 'net_sales' }, 'Net Sales');
    expect(single).toEqual([{ key: 'net_sales', label: 'Net Sales' }]);
    // No colour of its own, so both charts fall through to the same palette
    // default and the one bar matches the one line beside it.
    expect(trendSeriesFrom({ y_axis: 'net_sales' }, 'Net Sales'))
      .toEqual([{ key: 'net_sales', label: 'Net Sales' }]);
  });

  it('names the target rather than repeating the window', () => {
    // The tool labels every series with the window it covers, so the target and
    // this year's actual arrive carrying the same string. Left alone the legend
    // reads "FY 2026-27" twice, with nothing saying which one is the plan.
    const raw = TREND_CHART.series.filter((s) => s.key.startsWith('net_sales_minus') === false);
    expect(raw.map((s) => s.label)).toEqual(['Jul 2026 - Sep 2026', 'Jul 2026 - Sep 2026']);

    for (const named of [trendSeriesFrom(TREND_CHART, 'Net Sales', 'Target'),
                         barSeriesFrom(TREND_CHART, 'Net Sales', 'Target')]) {
      const labels = named.map((s) => s.label);
      expect(new Set(labels).size).toBe(labels.length);
      expect(named.find((s) => s.key === 'target_amount')?.label).toBe('Target');
      // Only the target is renamed — a year keeps the period name it was given.
      expect(named.find((s) => s.key === 'net_sales')?.label)
        .toBe('Jul 2026 - Sep 2026');
    }
  });

  it('leaves a caller that names no target exactly as it was', () => {
    // Every other page calls it with two arguments, so the third must change
    // nothing when it is absent.
    expect(trendSeriesFrom(TREND_CHART, 'Net Sales').map((s) => s.label))
      .toEqual(TREND_CHART.series.map((s) => s.label));
  });

  it('leaves the Sales Trend card drawing exactly what it always drew', () => {
    // The percentages travel on the rows, never in `chart.series`. This is the
    // card that was NOT being changed, and the one a stray key would break.
    const lines = trendSeriesFrom(TREND_CHART);
    expect(lines.map((line) => line.key)).toEqual([
      'net_sales', 'net_sales_minus_1', 'net_sales_minus_2', 'target_amount',
    ]);
    expect(lines.some((line) => line.key.endsWith('_percent'))).toBe(false);
  });
});

describe('what the card was asked to show beyond the series', () => {
  function drawWith(extra: Record<string, unknown>) {
    for (const key of Object.keys(drawn)) delete drawn[key];
    render(
      <ThemeProvider>
        <ComboBarLineChart data={ROWS} xKey="label" bars={BARS} lines={LINES}
                           {...extra} />
      </ThemeProvider>,
    );
    return drawn;
  }

  it('titles both axes when asked, and neither when not', () => {
    // A reader who takes the right-hand scale for taka reads every percentage
    // as a rounding error.
    const titled = drawWith({ valueAxisLabel: 'Net Sales / Target (BDT)',
                              percentAxisLabel: 'Achievement / Growth (%)' });
    expect((titled.Label ?? []).map((l) => l.value)).toEqual([
      'Net Sales / Target (BDT)', 'Achievement / Growth (%)',
    ]);
    // Rotated to sit along each axis, and each on its own side.
    expect(titled.Label.map((l) => l.angle)).toEqual([-90, 90]);

    expect(drawWith({}).Label).toBeUndefined();
  });

  it('lets the caller shorten a category before the phone rule runs', () => {
    // The window is in the page header, so a month need not repeat its year.
    const chart = drawWith({ xTickFormatter: (v: string) => v.replace(/\s+\d{4}$/, '') });
    expect(chart.XAxis[0].tickFormatter('Jul 2026')).toBe('Jul');
    // Untouched without one.
    expect(drawWith({}).XAxis[0].tickFormatter('Jul 2026')).toBe('Jul 2026');
  });

  it('prints each line reading, and prints nothing where the line breaks', () => {
    const chart = drawWith({ showLineValues: true });
    const labels = chart.LabelList ?? [];
    expect(labels.map((l) => l.dataKey))
      .toEqual(['achievement_percent', 'growth_percent']);
    expect(labels[0].formatter(75)).toBe('75%');
    // Khulna has no growth. A label over a gap would put a reading where the
    // chart is deliberately silent.
    expect(labels[1].formatter(null)).toBe('');
    expect(labels[1].formatter(undefined)).toBe('');

    expect(drawWith({}).LabelList).toBeUndefined();
  });

  it('lists the legend as bars first, then lines', () => {
    // Recharts orders it by the order each series registers itself, which put
    // the two lines in among the bars.
    const legend = drawWith({}).Legend[0];
    const { container } = render(<ThemeProvider>{legend.content()}</ThemeProvider>);
    expect([...container.querySelectorAll('li')].map((li) => li.textContent))
      .toEqual([
        'Target', 'Actual', 'Last-period Actual', 'Achievement %', 'Growth %',
      ]);
  });
});

describe('the hue rule both dashboard charts read', () => {
  it('runs the palette oldest year first, whatever order it is given', () => {
    // The bar card draws oldest-first and the line chart draws current-first.
    // Colouring by position would give one year two colours on one page, which
    // is the whole reason the rank comes from the key.
    const lines = seriesHues(trendSeriesFrom(TREND_CHART));
    const bars = seriesHues(barSeriesFrom(TREND_CHART));

    const hue = (list: typeof lines, key: string) =>
      list.find((one) => one.key === key)?.color;
    for (const key of ['net_sales', 'net_sales_minus_1', 'net_sales_minus_2',
                       'target_amount']) {
      expect(hue(lines, key)).toBe(hue(bars, key));
    }
    expect(hue(lines, 'net_sales_minus_2')).toBe(CHART_COLORS[0]);
    expect(hue(lines, 'net_sales_minus_1')).toBe(CHART_COLORS[1]);
  });

  it('gives this period its own green and the target the neutral', () => {
    const hues = seriesHues(trendSeriesFrom(TREND_CHART));
    expect(hues.find((one) => one.key === 'net_sales')?.color)
      .toBe(CHART_COLORS[4]);
    expect(hues.find((one) => one.key === 'target_amount')?.color)
      .toBe(CHART_COLORS[7]);
  });

  it('leaves a caller that does not read it alone', () => {
    // Sales, Target and Materials draw one trend on its own and keep the
    // palette order they have; only the dashboard opts in.
    expect(trendSeriesFrom(TREND_CHART).find((one) => one.key === 'net_sales')?.color)
      .toBe(CHART_COLORS[0]);
  });
});
