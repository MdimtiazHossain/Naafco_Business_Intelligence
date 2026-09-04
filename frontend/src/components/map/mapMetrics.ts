/**
 * Metric lookups and formatting for the map, from the catalogue the backend
 * publishes. Nothing here knows a metric by name: the catalogue says what each
 * one is called, which feature property carries it and how it is formatted.
 */

import type { MapMetricInfo } from '../../types/api';
import { formatAmount, formatByKind, formatPercent } from '../../utils/format';

export function metricByKey(metrics: MapMetricInfo[], key: string): MapMetricInfo | undefined {
  return metrics.find((metric) => metric.key === key);
}

/** A figure as the metric's kind says it should read; `n/a` when absent. */
export function formatMetric(metric: MapMetricInfo | undefined, value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  if (!metric) return String(value);
  if (metric.kind === 'percent') return formatPercent(value, { signed: metric.signed });
  if (metric.kind === 'currency' && metric.signed) {
    return value > 0 ? `+${formatAmount(value)}` : formatAmount(value);
  }
  return formatByKind(value, metric.kind);
}

/** The property a metric key reads on a feature or a ranking row. */
export function metricField(metrics: MapMetricInfo[], key: string): string {
  return metricByKey(metrics, key)?.field ?? key;
}
