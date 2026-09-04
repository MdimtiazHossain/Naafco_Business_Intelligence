/**
 * The translation of a declared style into MapLibre expressions.
 *
 * Every number and colour asserted here is one the fixtures declare; the
 * builders add only the expression grammar, and the one rule of their own —
 * absent is neutral, never zero — is what each case pins.
 */

import { describe, expect, it } from 'vitest';
import {
  NO_SELECTION,
  colorExpression,
  labelExpression,
  layerId,
  radiusExpression,
  selectedFilter,
  shouldCluster,
  sourceId,
} from '../components/map/mapExpressions';
import { METRICS, STYLE, feature, layerConfig, response } from './mapFixtures';

const isNumber = (field: string) => ['==', ['typeof', ['get', field]], 'number'];

describe('map expressions', () => {
  it('names sources and layers by level', () => {
    expect(sourceId('sub_territory')).toBe('business-map-sub_territory');
    expect(layerId('sub_territory', 'points')).toBe('business-map-sub_territory-points');
  });

  it('colours achievement in the declared bands, ascending as MapLibre wants them', () => {
    const layer = layerConfig('region', 1);
    const data = response('region', [feature('region', 'R1', 'One', 10)]).layers[0];
    expect(colorExpression(layer, data, METRICS)).toEqual([
      'case', isNumber('achievement_percent'),
      ['step', ['get', 'achievement_percent'],
        STYLE.band_colors.critical, 50, STYLE.band_colors.low, 70, STYLE.band_colors.medium, 90, STYLE.band_colors.good],
      STYLE.no_data_color,
    ]);
  });

  it('colours a signed metric by its sign, with zero in the middle', () => {
    const layer = layerConfig('region', 1, { effective_color_metric: 'growth', color_mode: 'diverging' });
    const data = response('region').layers[0];
    expect(colorExpression(layer, data, METRICS)).toEqual([
      'case', isNumber('growth_percent'),
      ['case', ['<', ['get', 'growth_percent'], 0], STYLE.diverging.negative,
        ['>', ['get', 'growth_percent'], 0], STYLE.diverging.positive, STYLE.diverging.neutral],
      STYLE.no_data_color,
    ]);
  });

  it('colours a sequential metric by the data\'s own class breaks', () => {
    const layer = layerConfig('region', 1, { effective_color_metric: 'net_sales', color_mode: 'sequential' });
    const data = response('region').layers[0];
    data.breaks = { net_sales: [100, 1_000, 10_000] };
    expect(colorExpression(layer, data, METRICS)).toEqual([
      'case', isNumber('net_sales'),
      ['step', ['get', 'net_sales'], '#bfdbfe', 100, '#60a5fa', 1_000, '#2563eb', 10_000, '#1e3a8a'],
      STYLE.no_data_color,
    ]);
    // Fewer classes take the dark end of the ramp; none is one flat colour.
    data.breaks = { net_sales: [500] };
    expect(colorExpression(layer, data, METRICS)[2]).toEqual(
      ['step', ['get', 'net_sales'], '#2563eb', 500, '#1e3a8a'],
    );
    data.breaks = {};
    expect(colorExpression(layer, data, METRICS)).toEqual([
      'case', isNumber('net_sales'), '#1e3a8a', STYLE.no_data_color,
    ]);
  });

  it('sizes by the square root of the figure across the declared radius range', () => {
    const layer = layerConfig('region', 1);
    const data = response('region').layers[0];
    data.extents = { net_sales: { min: 0, max: 100 } };
    expect(radiusExpression(layer, data, METRICS)).toEqual([
      'case', isNumber('net_sales'),
      ['interpolate', ['linear'], ['sqrt', ['max', 0, ['get', 'net_sales']]], 0, 4, 10, 22],
      4,
    ]);
    // Every figure equal, or no figure at all: one middling size for all.
    data.extents = { net_sales: { min: 50, max: 50 } };
    expect(radiusExpression(layer, data, METRICS)).toBe(13);
    data.extents = {};
    expect(radiusExpression(layer, data, METRICS)).toBe(13);
  });

  it('clusters only above the count the layer names', () => {
    const many = Array.from({ length: 201 }, (_, i) => feature('customer', `C${i}`, `Customer ${i}`, i));
    const layer = layerConfig('customer', 1);
    expect(shouldCluster(layer, response('customer', many).layers[0])).toBe(true);
    expect(shouldCluster(layer, response('customer', many.slice(0, 200)).layers[0])).toBe(false);
    expect(shouldCluster(layerConfig('customer', 1, { cluster_at: null }), response('customer', many).layers[0])).toBe(false);
  });

  it('labels by name, by code or by a metric\'s figure', () => {
    expect(labelExpression(layerConfig('region', 1), METRICS)).toEqual(['get', 'name']);
    expect(labelExpression(layerConfig('region', 1, { label_field: 'code' }), METRICS)).toEqual(['get', 'code']);
    expect(labelExpression(layerConfig('region', 1, { label_field: 'net_sales' }), METRICS)).toEqual([
      'case', isNumber('net_sales'),
      ['number-format', ['get', 'net_sales'], { 'max-fraction-digits': 0 }],
      '',
    ]);
  });

  it('selects one code, or nothing', () => {
    expect(selectedFilter('REG001')).toEqual(['==', ['get', 'code'], 'REG001']);
    expect(selectedFilter(null)).toEqual(['==', ['get', 'code'], NO_SELECTION]);
  });
});
