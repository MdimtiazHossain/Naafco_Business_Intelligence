/**
 * What the trend chart is asked to draw.
 *
 * Recharts is stubbed rather than rendered: what matters here is the props the
 * chart hands each line, and an SVG assertion would test the library. Two of
 * them are load-bearing and both fail *invisibly* — a joined null draws a
 * straight line through months nobody measured, and a target sharing the year
 * palette reads as a fourth year.
 */

import { render } from '@testing-library/react';
import { ThemeProvider } from '../contexts/ThemeContext';
import { describe, expect, it, vi } from 'vitest';

/** Captures the props of every `Line` the chart renders. */
const lines: Record<string, any>[] = [];
/** And of every axis, for the two-axis form. */
const axes: Record<string, any>[] = [];

vi.mock('recharts', () => {
  const passthrough = (name: string) => {
    const Component = (props: any) => {
        if (name === 'Line' || name === 'Area') lines.push(props);
      if (name === 'YAxis') axes.push(props);
      return <div data-testid={name}>{props.children}</div>;
    };
    Component.displayName = name;
    return Component;
  };
  return {
    ResponsiveContainer: passthrough('ResponsiveContainer'),
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

const { TrendChart, CHART_COLORS } = await import('../charts/Charts');

const ROWS = [
  { label: 'Jul 2026', net_sales: 100, net_sales_minus_1: 90, target_amount: 120,
    achievement_percent: 83.3, growth_percent: 11.1 },
  // August has no figure for last year — the gap the chart must keep — so it
  // has no growth either, which is the same gap one axis further right.
  { label: 'Aug 2026', net_sales: 130, net_sales_minus_1: null, target_amount: 125,
    achievement_percent: 104, growth_percent: null },
  { label: 'Sep 2026', net_sales: 150, net_sales_minus_1: 110, target_amount: 130,
    achievement_percent: 115.4, growth_percent: 36.4 },
];

function draw(series: Parameters<typeof TrendChart>[0]['series'],
              extra: Record<string, unknown> = {}) {
  lines.length = 0;
  axes.length = 0;
  render(
    <ThemeProvider>
      <TrendChart data={ROWS} xKey="label" series={series} {...extra} />
    </ThemeProvider>,
  );
  return lines;
}

describe('a multi-series trend', () => {
  it('draws one line per series, keyed by the column it names', () => {
    const drawn = draw([
      { key: 'net_sales', label: 'This year' },
      { key: 'net_sales_minus_1', label: 'Last year' },
    ]);
    expect(drawn.map((line) => line.dataKey)).toEqual([
      'net_sales',
      'net_sales_minus_1',
    ]);
    expect(drawn.map((line) => line.name)).toEqual(['This year', 'Last year']);
  });

  it('never joins across a null', () => {
    // The whole reason the backend sends null rather than 0. Joining draws a
    // straight line through a month nobody measured and reads as a real trend.
    const drawn = draw([
      { key: 'net_sales', label: 'This year' },
      { key: 'net_sales_minus_1', label: 'Last year' },
    ]);
    expect(drawn.every((line) => line.connectNulls === false)).toBe(true);
  });

  it('gives the target a dash and the neutral colour, not a fourth year colour', () => {
    // Hue says which year; the dash says plan against measurement. A target in
    // the next palette colour would read as one more year.
    const drawn = draw([
      { key: 'net_sales', label: 'This year' },
      { key: 'net_sales_minus_1', label: 'Last year' },
      { key: 'target_amount', label: 'Target', dashed: true, color: CHART_COLORS[7] },
    ]);
    const target = drawn[2];
    expect(target.strokeDasharray).toBeTruthy();
    expect(target.stroke).toBe(CHART_COLORS[7]);
    // And the years are solid, so the distinction is visible.
    expect(drawn[0].strokeDasharray).toBeUndefined();
    expect(drawn[1].strokeDasharray).toBeUndefined();
  });

  it('keeps a series to its own colour so a year does not change hue', () => {
    const drawn = draw([
      { key: 'net_sales', label: 'This year' },
      { key: 'net_sales_minus_1', label: 'Last year' },
    ]);
    expect(drawn[0].stroke).toBe(CHART_COLORS[0]);
    expect(drawn[1].stroke).toBe(CHART_COLORS[1]);
  });
});

describe('a trend with ratios beside it', () => {
  const MONEY = [
    { key: 'net_sales', label: 'This year' },
    { key: 'target_amount', label: 'Target', dashed: true },
  ];
  const RATIOS = [
    { key: 'achievement_percent', label: 'Ach.%' },
    { key: 'growth_percent', label: 'Grw.%' },
  ];

  it('puts money on the left axis and ratios on the right', () => {
    const drawn = draw(MONEY, { percentSeries: RATIOS });
    expect(drawn.map((line) => [line.dataKey, line.yAxisId])).toEqual([
      ['net_sales', 'value'],
      ['target_amount', 'value'],
      ['achievement_percent', 'percent'],
      ['growth_percent', 'percent'],
    ]);
    expect(axes.map((a) => [a.yAxisId, a.orientation]))
      .toEqual([['value', undefined], ['percent', 'right']]);
  });

  it('draws no second axis where the rows carry no reading', () => {
    // A period charted by day has no monthly target and no aligned prior year,
    // so neither key is on those rows at all. An empty scale beside the plot
    // reads as a measure sitting at zero rather than one this period cannot
    // answer.
    const drawn = draw(MONEY, {
      percentSeries: [{ key: 'not_on_these_rows', label: 'Ach.%' }],
    });
    expect(drawn.map((line) => line.dataKey)).toEqual(['net_sales', 'target_amount']);
    expect(axes).toHaveLength(1);
    // And with no second axis, nothing names one — which is what Recharts
    // requires of a single-axis chart.
    expect(drawn.every((line) => line.yAxisId === undefined)).toBe(true);
  });

  it('leaves a caller that passes none exactly as it was', () => {
    // Sales, Target and Materials draw this chart with one axis.
    const drawn = draw(MONEY);
    expect(axes).toHaveLength(1);
    expect(axes[0].yAxisId).toBeUndefined();
    expect(drawn.every((line) => line.yAxisId === undefined)).toBe(true);
  });

  it('never joins a ratio across a null either', () => {
    const drawn = draw(MONEY, { percentSeries: RATIOS });
    expect(drawn.every((line) => line.connectNulls === false)).toBe(true);
  });
});
