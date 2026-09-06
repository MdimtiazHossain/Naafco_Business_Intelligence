/**
 * What the backend declares for the map, as the map tests see it.
 *
 * Shaped exactly like `GET /api/map/config`, `/designs` and `/data` answer —
 * the same style defaults, the same inherited-versus-effective layer fields —
 * so a test that passes here is exercising the page against the contract the
 * server keeps, not a convenient simplification of it.
 */

import type {
  MapConfig,
  MapDataResponse,
  MapDesign,
  MapFeature,
  MapLayerConfig,
  MapMetricInfo,
  MapRankingRow,
  MapStyle,
} from '../types/api';

export const LIGHT = 'https://tiles.test/styles/positron';
export const DARK = 'https://tiles.test/styles/dark';

export const STYLE: MapStyle = {
  thresholds: [90, 70, 50],
  band_colors: { good: '#16a34a', medium: '#f59e0b', low: '#ea580c', critical: '#dc2626' },
  bands: [
    { key: 'good', min: 90, max: null, label: '90% and above', color: '#16a34a' },
    { key: 'medium', min: 70, max: 90, label: '70% – 90%', color: '#f59e0b' },
    { key: 'low', min: 50, max: 70, label: '50% – 70%', color: '#ea580c' },
    { key: 'critical', min: null, max: 50, label: 'Below 50%', color: '#dc2626' },
  ],
  no_data_color: '#94a3b8',
  sequential: ['#bfdbfe', '#60a5fa', '#2563eb', '#1e3a8a'],
  diverging: { negative: '#dc2626', neutral: '#94a3b8', positive: '#16a34a' },
  radius: [4, 22],
  cluster: { color: '#1d4ed8', text_color: '#ffffff' },
  shape: 'circle',
  point_color: '#2563eb',
  derived_opacity: 0.25,
  derived_stroke_width: 1.5,
  // The categorical palette, as the server declares it. Four colours rather
  // than the real twelve, so a test can cross the threshold without seeding
  // a dozen groups — the rule under test is "more groups than colours", and
  // where that line sits is the server's business, not this fixture's.
  categorical: ['#0072b2', '#e69f00', '#009e73', '#cc79a7'],
  max_categorical_groups: 4,
  focus_color: '#dc2626',
  group_neutral_color: '#94a3b8',
  boundary: {
    fill_color: '#64748b', fill_opacity: 0.06,
    line_color: '#475569', line_width: 1, line_opacity: 0.55,
    hover_fill_opacity: 0.14,
    selected_line_color: '#0f172a', selected_line_width: 2.5,
    selected_fill_opacity: 0.18,
    mask_color: '#0f172a', mask_opacity: 0.1,
    label_min_zoom: 7,
  },
};

export const LEVEL_LABELS: Record<string, string> = {
  zone: 'Zone', region: 'Region', customer: 'Customer',
};

export const METRICS: MapMetricInfo[] = [
  { key: 'net_sales', label: 'Sales Amount', field: 'net_sales', kind: 'currency', higher_is_better: true, signed: false, unavailable_at: [], description: '' },
  { key: 'target_amount', label: 'Target Amount', field: 'target_amount', kind: 'currency', higher_is_better: true, signed: false, unavailable_at: [], description: '' },
  { key: 'achievement', label: 'Achievement %', field: 'achievement_percent', kind: 'percent', higher_is_better: true, signed: false, unavailable_at: [], description: '' },
  { key: 'growth', label: 'Growth %', field: 'growth_percent', kind: 'percent', higher_is_better: true, signed: true, unavailable_at: [], description: '' },
  { key: 'customer_count', label: 'Customer Count', field: 'customer_count', kind: 'count', higher_is_better: true, signed: false, unavailable_at: ['customer'], description: '' },
];

export function layerConfig(
  level: string,
  order: number,
  overrides: Partial<MapLayerConfig> = {},
): MapLayerConfig {
  return {
    layer_id: order,
    layer_name: LEVEL_LABELS[level] ?? level,
    point_level: level,
    level_label: LEVEL_LABELS[level] ?? level,
    parent_level: null,
    view_mode: 'point',
    view_modes: ['point'],
    boundary_available: false,
    metric: null,
    effective_metric: 'net_sales',
    color_metric: 'achievement',
    effective_color_metric: 'achievement',
    color_mode: 'bands',
    size_metric: 'net_sales',
    effective_size_metric: 'net_sales',
    is_visible: true,
    display_order: order,
    min_zoom: 0,
    cluster_at: level === 'customer' ? 200 : null,
    label_field: 'name',
    show_label: false,
    label_min_zoom: 8,
    tooltip_fields: ['net_sales', 'target_amount', 'achievement', 'growth'],
    tooltip_inherited: true,
    style_config: null,
    style: STYLE,
    ...overrides,
  };
}

export const CONFIG: MapConfig = {
  basemaps: [
    { key: 'standard', label: 'Map', style_url: LIGHT, style_url_dark: DARK, kind: 'style', attribution: null, glyphs_url: null },
  ],
  default_basemap: 'standard',
  view: { latitude: 23.685, longitude: 90.356, zoom: 6.5 },
  levels: ['zone', 'region', 'customer'].map((key, index) => ({
    key,
    label: LEVEL_LABELS[key],
    code_field: `${key}_code`,
    name_field: `${key}_name`,
    table: `dim_${key}`,
    parent: null,
    group: 'Organisation',
    depth: index,
    promoted: true,
    boundary_available: false,
    view_modes: ['point'],
  })),
  promoted_levels: ['zone', 'region', 'customer'],
  view_modes: [
    { key: 'point', label: 'Point', requires_boundary: false },
    { key: 'boundary', label: 'Boundary', requires_boundary: true },
    { key: 'both', label: 'Both', requires_boundary: true },
  ],
  metrics: METRICS,
  shapes: [
    { key: 'circle', label: 'Circle', path: 'M22 12 A10 10 0 1 1 2 12 A10 10 0 1 1 22 12 Z', viewbox: 24 },
    { key: 'square', label: 'Square', path: 'M3 3 H21 V21 H3 Z', viewbox: 24 },
    { key: 'triangle', label: 'Triangle', path: 'M12 2 L22 20 H2 Z', viewbox: 24 },
  ],
  purposes: ['analysis', 'demarcation'],
  boundaries: {
    sets: [
      { key: 'division', label: 'Divisions', url: '/geo/bgd_admin1.geojson',
        file: 'bgd_admin1.geojson', admin_level: 1, table: 'dim_division',
        features: 8, bytes: 95337 },
      { key: 'district', label: 'Districts', url: '/geo/bgd_admin2.geojson',
        file: 'bgd_admin2.geojson', admin_level: 2, table: 'dim_district',
        features: 64, bytes: 369934 },
      { key: 'upazila', label: 'Upazilas', url: '/geo/bgd_admin3.geojson',
        file: 'bgd_admin3.geojson', admin_level: 3, table: 'dim_upazila',
        features: 507, bytes: 1727737 },
    ],
    // Per surface, as the server publishes it: the analysis map opens bare and
    // the demarcation tab opens with upazila outlines.
    defaults: { analysis: null, demarcation: 'upazila' },
    mask_url: '/geo/bgd_mask.geojson',
    note: 'Administrative reference outlines. They are not business boundaries.',
  },
  defaults: { metric: 'net_sales', color_metric: 'achievement', size_metric: 'net_sales', tooltip_fields: ['net_sales'] },
  // The levels a coordinate can be narrowed by, as the server derives them
  // from `map.levels.MAP_LEVELS`. `boundaries.test.tsx` pins the browser's
  // `LOCATION_FILTERS` equal to the real list; this is the fixture's stand-in.
  color_by_levels: [
    'company', 'bu', 'sales_line', 'zone', 'region',
    'area', 'unit', 'territory', 'sub_territory',
  ],
  location_filters: [
    'company_code', 'bu_code', 'sales_line_code', 'zone_code', 'region_code',
    'area_code', 'unit_code', 'territory_code', 'sub_territory_code',
    'customer_code', 'sales_force_code',
  ],
  style: STYLE,
  coverage: [],
};

export const DESIGN: MapDesign = {
  design_id: 1,
  name: 'Business Overview',
  description: null,
  purpose: 'analysis',
  basemap: 'standard',
  basemap_resolved: CONFIG.basemaps[0],
  basemap_note: null,
  default_metric: 'net_sales',
  is_default: true,
  is_active: true,
  is_system_default: true,
  created_by: 'system',
  created_at: null,
  updated_at: null,
  layer_count: 3,
  layers: [layerConfig('zone', 1), layerConfig('region', 2), layerConfig('customer', 3, { is_visible: false })],
};

export const PERIOD = {
  type: 'CUSTOM',
  date_from: '2026-08-01',
  date_to: '2026-08-31',
  label: '2026-08-01 to 2026-08-31',
  compare_from: '2026-07-01',
  compare_to: '2026-07-31',
};

export function feature(
  level: string,
  code: string,
  name: string,
  netSales: number,
  coordinates: [number, number] = [90.4, 23.78],
): MapFeature {
  return {
    type: 'Feature',
    id: `${level}:${code}`,
    geometry: { type: 'Point', coordinates },
    properties: {
      code, name, level, parent_level: null, parent_code: null,
      location_source: 'UPLOAD', location_precision: 'APPROXIMATE', derived_from: null,
      net_sales: netSales, quantity: 10, volume: null, customer_count: 3,
      target_amount: 1_000_000, target_quantity: null, target_volume: null,
      achievement_percent: 75, shortfall: -250_000, previous_net_sales: 900_000,
      growth_percent: 11.1,
    },
  };
}

export function rankingRow(code: string, name: string, netSales: number, placed = true): MapRankingRow {
  return {
    code, name, placed, parent_code: null,
    net_sales: netSales, quantity: 10, volume: null, customer_count: 3,
    target_amount: 1_000_000, target_quantity: null, target_volume: null,
    achievement_percent: 75, shortfall: -250_000, previous_net_sales: 900_000,
    growth_percent: 11.1,
  };
}

export const REGIONS = [
  feature('region', 'REG001', 'Dhaka', 1_500_000, [90.4, 23.78]),
  feature('region', 'REG002', 'Khulna', 900_000, [89.54, 22.85]),
];

export function response(
  level: string,
  features: MapFeature[] = [],
  ranking: { top: MapRankingRow[]; bottom: MapRankingRow[] } = { top: [], bottom: [] },
): MapDataResponse {
  const layer = DESIGN.layers.find((candidate) => candidate.point_level === level) ?? layerConfig(level, 9);
  const values = features.map((f) => f.properties.net_sales ?? 0);
  return {
    period: PERIOD,
    filters: {},
    design: DESIGN,
    metric: 'net_sales',
    levels: [level],
    empty: features.length === 0,
    layers: [{
      level,
      label: LEVEL_LABELS[level] ?? level,
      group_by: level,
      entity_count: features.length,
      placed_count: features.length,
      features: { type: 'FeatureCollection', features },
      unplaced: [],
      unassigned: null,
      notes: [],
      bounds: features.length
        ? { north: 23.78, south: 22.85, east: 90.4, west: 89.54, centre: { latitude: 23.3, longitude: 89.97 } }
        : null,
      extents: features.length
        ? { net_sales: { min: Math.min(...values), max: Math.max(...values) }, achievement: { min: 75, max: 75 } }
        : {},
      breaks: features.length ? { net_sales: [Math.max(...values)] } : {},
      layer,
      ranking: {
        metric: 'net_sales', field: 'net_sales', ...ranking,
        ranked_count: ranking.top.length + ranking.bottom.length, unranked_count: 0,
      },
    }],
  };
}
