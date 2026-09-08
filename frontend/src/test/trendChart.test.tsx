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

vi.mock('recharts', () => {
  const passthrough = (name: string) => {
    const Component = (props: any) => {
      if (name === 'Line' || name === 'Area') lines.push(props);
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
    LabelList: passthrough('LabelList'),
    ReferenceLine: passthrough('ReferenceLine'),
  };
});

const { TrendChart, CHART_COLORS } = await import('../charts/Charts');

const ROWS = [
  { label: 'Jul 2026', net_sales: 100, net_sales_minus_1: 90, target_amount: 120 },
  // August has no figure for last year — the gap the chart must keep.
  { label: 'Aug 2026', net_sales: 130, net_sales_minus_1: null, target_amount: 125 },
  { label: 'Sep 2026', net_sales: 150, net_sales_minus_1: 110, target_amount: 130 },
];

function draw(series: Parameters<typeof TrendChart>[0]['series']) {
  lines.length = 0;
  render(
    <ThemeProvider>
      <TrendChart data={ROWS} xKey="label" series={series} />
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
