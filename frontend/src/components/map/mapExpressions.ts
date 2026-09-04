/**
 * MapLibre paint expressions, built from what the backend declares.
 *
 * The renderer must not be the only thing that knows a marker's design, so no
 * threshold, colour or radius is written here: the bands, the ramp, the
 * neutral colour and the radius range come from the layer's effective style,
 * the class breaks and extents from the layer's own data, and the metric
 * being drawn from the layer's configuration. What this module adds is only
 * the translation into the expression grammar MapLibre evaluates per feature.
 *
 * Absent is drawn neutral, never as zero: every colour expression tests that
 * the property is a number before it classifies it, so a territory with no
 * target is grey rather than "critical", and a point with no size figure is
 * the smallest circle rather than a missing one.
 */

import type { ExpressionSpecification } from 'maplibre-gl';
import type { MapLayerConfig, MapLayerData, MapMetricInfo } from '../../types/api';
import { metricByKey } from './mapMetrics';

export const SOURCE_PREFIX = 'business-map';

export const LAYER_SUFFIXES = {
  clusters: 'clusters',
  clusterCount: 'cluster-count',
  points: 'points',
  labels: 'labels',
  selected: 'selected',
} as const;

export type LayerSuffix = (typeof LAYER_SUFFIXES)[keyof typeof LAYER_SUFFIXES];

/** The font every label uses; the OpenFreeMap styles all carry it. */
export const LABEL_FONT = ['Noto Sans Regular'];

export function sourceId(level: string): string {
  return `${SOURCE_PREFIX}-${level}`;
}

export function layerId(level: string, suffix: LayerSuffix): string {
  return `${sourceId(level)}-${suffix}`;
}

type Expr = ExpressionSpecification;

function get(field: string): Expr {
  return ['get', field] as unknown as Expr;
}

function isNumber(field: string): Expr {
  return ['==', ['typeof', ['get', field]], 'number'] as unknown as Expr;
}

/** Whether a layer's points are clustered: only above the count it names. */
export function shouldCluster(layer: MapLayerConfig, data: MapLayerData): boolean {
  return layer.cluster_at !== null && data.features.features.length > layer.cluster_at;
}

/** The circle colour of a point, by the layer's colour metric and mode. */
export function colorExpression(
  layer: MapLayerConfig,
  data: MapLayerData,
  metrics: MapMetricInfo[],
): Expr {
  const metric = metricByKey(metrics, layer.effective_color_metric);
  const field = metric?.field ?? layer.effective_color_metric;
  const style = layer.style;

  if (layer.color_mode === 'bands') {
    // `step` wants ascending stops; the thresholds are declared highest first.
    const [high, mid, low] = style.thresholds;
    const colours = style.band_colors;
    return [
      'case', isNumber(field),
      ['step', get(field), colours.critical, low, colours.low, mid, colours.medium, high, colours.good],
      style.no_data_color,
    ] as unknown as Expr;
  }

  if (layer.color_mode === 'diverging') {
    const { negative, neutral, positive } = style.diverging;
    return [
      'case', isNumber(field),
      ['case', ['<', get(field), 0], negative, ['>', get(field), 0], positive, neutral],
      style.no_data_color,
    ] as unknown as Expr;
  }

  const breaks = data.breaks[layer.effective_color_metric] ?? [];
  // One colour per class, taken from the dark end of the ramp so a layer with
  // two classes reads as two clearly different blues rather than two pale ones.
  const colours = style.sequential.slice(-(breaks.length + 1));
  if (breaks.length === 0) {
    return ['case', isNumber(field), colours[0], style.no_data_color] as unknown as Expr;
  }
  const stops: unknown[] = ['step', get(field), colours[0]];
  breaks.forEach((value, index) => stops.push(value, colours[index + 1]));
  return ['case', isNumber(field), stops, style.no_data_color] as unknown as Expr;
}

/**
 * The circle radius of a point, by the layer's size metric.
 *
 * Scaled by the square root of the figure, so a point with four times the
 * sales has twice the radius and four times the area — area is what the eye
 * compares. A layer whose figures are all equal draws every point at the
 * middle of the range; a point with no figure draws at the smallest.
 */
export function radiusExpression(
  layer: MapLayerConfig,
  data: MapLayerData,
  metrics: MapMetricInfo[],
): Expr | number {
  const metric = metricByKey(metrics, layer.effective_size_metric);
  const field = metric?.field ?? layer.effective_size_metric;
  const [smallest, largest] = layer.style.radius;
  const extent = data.extents[layer.effective_size_metric];
  const low = Math.sqrt(Math.max(0, extent?.min ?? 0));
  const high = Math.sqrt(Math.max(0, extent?.max ?? 0));
  if (!extent || high <= low) {
    return (smallest + largest) / 2;
  }
  return [
    'case', isNumber(field),
    ['interpolate', ['linear'], ['sqrt', ['max', 0, get(field)]], low, smallest, high, largest],
    smallest,
  ] as unknown as Expr;
}

/** Cluster circles grow with the number of points they stand for. */
export function clusterRadiusExpression(): Expr {
  return ['step', ['get', 'point_count'], 14, 20, 18, 100, 24, 500, 30] as unknown as Expr;
}

/** What a label reads: the name, the code, or a metric's figure. */
export function labelExpression(layer: MapLayerConfig, metrics: MapMetricInfo[]): Expr {
  if (layer.label_field === 'name' || layer.label_field === 'code') {
    return get(layer.label_field);
  }
  const field = metricByKey(metrics, layer.label_field)?.field ?? layer.label_field;
  return [
    'case', isNumber(field),
    ['number-format', get(field), { 'max-fraction-digits': 0 }],
    '',
  ] as unknown as Expr;
}

/**
 * A code no entity can have, so a selection ring with nothing selected
 * matches nothing. Business codes are trimmed on the way in, so a lone
 * space never survives to a feature.
 */
export const NO_SELECTION = ' ';

/** The filter that picks the selected entity's point, or nothing. */
export function selectedFilter(code: string | null): Expr {
  return ['==', ['get', 'code'], code ?? NO_SELECTION] as unknown as Expr;
}

export const POINT_FILTER = ['!', ['has', 'point_count']] as unknown as Expr;
export const CLUSTER_FILTER = ['has', 'point_count'] as unknown as Expr;
