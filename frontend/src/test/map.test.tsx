/**
 * The map's client side: the business data adapter, the local GeoJSON loader,
 * and the page over a stubbed MapLibre.
 *
 * MapLibre needs a WebGL context, which jsdom has no notion of, so the engine is
 * replaced here by a recorder that remembers the sources, layers and paint
 * properties it was given. That is the right seam anyway: what these tests are
 * about is *what the page asks the map to draw* — that boundaries come from
 * local files, that colours come from the database, that a hidden layer is
 * hidden rather than refetched — none of which is a question about WebGL.
 *
 * The adapter tests need no map at all. They are the pure half of the map, and
 * they are where the rules that must not regress live: an entity with no
 * coordinate is never invented onto the map, and `0, 0` is not a coordinate.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import {
  buildEntityCollection,
  collectionBounds,
  iconName,
  isDrawable,
} from '../components/map/businessGeoJson';
import { clearGeoCache, loadGeoJson } from '../components/map/geoData';
import { FALLBACK_AREA_STYLES, GEO_FILES, IDS } from '../components/map/mapConfig';
import * as services from '../services';
import type { MapEntityPoint, MapLayer } from '../types/api';

// ---------------------------------------------------------------------------
// A recording stand-in for MapLibre
// ---------------------------------------------------------------------------

interface RecordedLayer {
  id: string;
  type: string;
  source?: string;
  paint?: Record<string, unknown>;
  layout?: Record<string, unknown>;
  minzoom?: number;
}

class FakeMap {
  static last: FakeMap | null = null;
  /**
   * What the next map reports from `isStyleLoaded()` when it is created.
   *
   * The real MapLibre returns false until the style has parsed, and a request
   * to add layers during that window used to be dropped for good. Simulating it
   * needs the flag set *before* construction, because the component asks as
   * soon as the map reports `load`.
   */
  static nextStyleLoaded = true;

  sources = new Map<string, { data: unknown; promoteId?: string }>();
  layers = new Map<string, RecordedLayer>();
  images = new Set<string>();
  controls: unknown[] = [];
  handlers = new Map<string, ((event: unknown) => void)[]>();
  fitted: unknown[] = [];
  styleUrl: string;
  removed = false;

  styleLoaded = true;

  constructor(options: { style: string }) {
    this.styleUrl = options.style;
    this.styleLoaded = FakeMap.nextStyleLoaded;
    FakeMap.last = this;
    // The real map reports `style.load` when the style is parsed and `load`
    // once after that. A style that has not parsed yet fires neither, which is
    // exactly the slow-style case; `parseStyle()` below releases it.
    queueMicrotask(() => { if (this.styleLoaded) this.parseStyle(); });
  }

  isStyleLoaded() { return this.styleLoaded; }

  /** The style finishes parsing: what MapLibre announces as `style.load`. */
  parseStyle() {
    this.styleLoaded = true;
    this.fire('style.load', {});
    this.fire('load', {});
    this.fire('styledata', {});
  }

  addSource(id: string, spec: { data: unknown; promoteId?: string }) {
    this.sources.set(id, { data: spec.data, promoteId: spec.promoteId });
  }

  getSource(id: string) {
    const source = this.sources.get(id);
    if (!source) return undefined;
    return { setData: (data: unknown) => { source.data = data; } };
  }

  addLayer(layer: RecordedLayer) { this.layers.set(layer.id, { ...layer }); }
  getLayer(id: string) { return this.layers.get(id); }

  setLayoutProperty(id: string, key: string, value: unknown) {
    const layer = this.layers.get(id);
    if (layer) layer.layout = { ...layer.layout, [key]: value };
  }

  setPaintProperty(id: string, key: string, value: unknown) {
    const layer = this.layers.get(id);
    if (layer) layer.paint = { ...layer.paint, [key]: value };
  }

  setFeatureState() {}
  hasImage(name: string) { return this.images.has(name); }
  addImage(name: string) { this.images.add(name); }
  addControl(control: unknown) { this.controls.push(control); }
  setStyleCalls: unknown[] = [];
  setStyle(style: string) {
    this.setStyleCalls.push(style);
    this.styleUrl = style;
    this.fire('styledata', {});
  }
  fitBounds(bounds: unknown) { this.fitted.push(bounds); }
  resize() {}
  remove() { this.removed = true; }
  getCanvas() { return { style: {} } as HTMLCanvasElement; }

  on(event: string, second?: unknown, third?: unknown) {
    // MapLibre overloads this as (event, handler) and (event, layer, handler).
    const handler = (typeof second === 'function' ? second : third) as
      (event: unknown) => void;
    const key = typeof second === 'string' ? `${event}:${second}` : event;
    this.handlers.set(key, [...(this.handlers.get(key) ?? []), handler]);
  }

  off() {}

  fire(key: string, event: unknown) {
    for (const handler of this.handlers.get(key) ?? []) handler(event);
  }

  /** Drive a layer click the way a user would. */
  click(layerId: string, feature: unknown) {
    this.fire(`click:${layerId}`, {
      features: [feature],
      lngLat: { lng: 90, lat: 23 },
    });
  }
}

class NoopControl {}

class FakePopup {
  setDOMContent() { return this; }
  setLngLat() { return this; }
  addTo() { return this; }
  remove() { return this; }
  on() { return this; }
}

// Named exports, matching how `maplibre-gl` v6 publishes them and how
// `BusinessMap` imports them. `GeoJSONSource` and `LngLat` are imported only as
// types there, but the module must still provide them.
vi.mock('maplibre-gl', () => ({
  Map: FakeMap,
  NavigationControl: NoopControl,
  FullscreenControl: NoopControl,
  ScaleControl: NoopControl,
  Popup: FakePopup,
  GeoJSONSource: class {},
  LngLat: class {},
  // BusinessMap points MapLibre at the worker Vite emits; under test there is
  // no worker and no bundler, so this only has to exist to be callable.
  setWorkerUrl: () => {},
}));

vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
// `?worker&url` is a Vite build feature; vitest resolves the bare module, so
// the suffixed specifier needs a stub the same way the stylesheet does.
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({
  default: 'maplibre-gl-worker.js',
}));

// ---------------------------------------------------------------------------
// The business data adapter — the pure half
// ---------------------------------------------------------------------------

function entity(overrides: Partial<MapEntityPoint> = {}): MapEntityPoint {
  return {
    type: 'region',
    id: 'REG001',
    code: 'REG001',
    name: 'Dhaka',
    parent_type: 'zone',
    parent_id: 'Z001',
    parent_code: 'Z001',
    latitude: 23.8041,
    longitude: 90.3868,
    value: 7893636,
    location_source: 'DERIVED',
    ...overrides,
  };
}

const ALL_LAYERS = new Set<MapLayer>(['region', 'customer', 'territory']);

describe('business data adapter', () => {
  it('turns entities into one GeoJSON collection', () => {
    const collection = buildEntityCollection(
      [entity(), entity({ code: 'C001', name: 'Ali Traders', type: 'customer' })],
      { visibleLayers: ALL_LAYERS },
    );

    expect(collection.type).toBe('FeatureCollection');
    expect(collection.features).toHaveLength(2);
    // GeoJSON is [longitude, latitude] — the reverse of how people say it, and
    // the single most likely thing to be wrong.
    expect(collection.features[0].geometry.coordinates).toEqual([90.3868, 23.8041]);
    expect(collection.features[0].properties.icon).toBe(iconName('region'));
  });

  it('draws nothing for an entity with no coordinate', () => {
    // The count still reports it and the page lists it under "Not placed". What
    // must never happen is a position being invented for it.
    const collection = buildEntityCollection(
      [entity({ latitude: null, longitude: null })],
      { visibleLayers: ALL_LAYERS },
    );
    expect(collection.features).toHaveLength(0);
  });

  it('refuses the null island and anything out of range', () => {
    expect(isDrawable(0, 0)).toBe(false);
    expect(isDrawable(23.8, null)).toBe(false);
    expect(isDrawable(Number.NaN, 90)).toBe(false);
    expect(isDrawable(95, 90)).toBe(false);
    expect(isDrawable(23.8, 200)).toBe(false);
    expect(isDrawable(23.8041, 90.3868)).toBe(true);
  });

  it('omits a layer that is switched off', () => {
    const collection = buildEntityCollection(
      [entity(), entity({ code: 'C001', type: 'customer' })],
      { visibleLayers: new Set<MapLayer>(['region']) },
    );
    expect(collection.features).toHaveLength(1);
    expect(collection.features[0].properties.entityType).toBe('region');
  });

  it('dims what a focus excludes rather than dropping it', () => {
    // Arriving from a table asking for one customer, the rest stay on screen:
    // the point of looking at a selection on a map is to see it in context.
    const collection = buildEntityCollection(
      [entity(), entity({ code: 'C001', type: 'customer' })],
      {
        visibleLayers: ALL_LAYERS,
        focusLayer: 'customer',
        focusCodes: new Set(['C001']),
      },
    );

    expect(collection.features).toHaveLength(2);
    const byCode = Object.fromEntries(
      collection.features.map((f) => [f.properties.code, f.properties.inFocus]),
    );
    expect(byCode.C001).toBe(true);
    expect(byCode.REG001).toBe(false);
  });

  it('bounds the collection it built, and reports none for an empty one', () => {
    const collection = buildEntityCollection(
      [entity(), entity({ code: 'K', latitude: 22.8456, longitude: 89.5403 })],
      { visibleLayers: ALL_LAYERS },
    );
    expect(collectionBounds(collection)).toEqual([89.5403, 22.8456, 90.3868, 23.8041]);
    expect(collectionBounds({ type: 'FeatureCollection', features: [] })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The local GeoJSON loader
// ---------------------------------------------------------------------------

describe('local GeoJSON', () => {
  beforeEach(() => clearGeoCache());

  it('fetches a file from the frontend, not from an API', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ type: 'FeatureCollection', features: [] }),
    });
    vi.stubGlobal('fetch', fetchSpy);

    await loadGeoJson('upazila');
    expect(fetchSpy).toHaveBeenCalledWith(GEO_FILES.upazila);
    expect(GEO_FILES.upazila.startsWith('/geo/')).toBe(true);
  });

  it('shares one request between concurrent callers', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ type: 'FeatureCollection', features: [] }),
    });
    vi.stubGlobal('fetch', fetchSpy);

    await Promise.all([loadGeoJson('country'), loadGeoJson('country')]);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('reports a missing file instead of throwing something opaque', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(loadGeoJson('district')).rejects.toThrow(/build_map_geojson/);
  });

  it('rejects a file that is not a FeatureCollection', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ type: 'Feature' }),
    }));
    await expect(loadGeoJson('division')).rejects.toThrow(/FeatureCollection/);
  });

  it('retries after a failure rather than caching the rejection', async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ type: 'FeatureCollection', features: [] }),
      });
    vi.stubGlobal('fetch', fetchSpy);

    await expect(loadGeoJson('mask')).rejects.toThrow();
    await expect(loadGeoJson('mask')).resolves.toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 1, username: 'root', role: 'SUPER_ADMIN', is_admin: true },
    isAdmin: true,
    hasSection: () => true,
  }),
}));

// The map takes the global filter bar like every other page; the stub keeps
// these tests about the map rather than about the bar's own controls.
vi.mock('../filters/GlobalFilterBar', () => ({
  GlobalFilterBar: () => <div data-testid="filter-bar" />,
}));

// Only the hook is stubbed. The rest of the module — `FILTER_LABELS` and the
// level tables the page reads — comes through as itself, because a second copy
// of those in here would pass while the real ones drifted. The stub supplies
// every field the page uses: the period `DateFilter` reads, the query it sends,
// and the filter state and setter the ranking panel drills with.
vi.mock('../contexts/FilterContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../contexts/FilterContext')>()),
  useFilters: () => ({
    query: { period: 'THIS_MONTH' },
    period: { period: 'THIS_MONTH', date_from: null, date_to: null },
    setPeriod: () => {},
    filters: {},
    setFilterResolved: () => {},
  }),
  FilterProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const RANKING_ROWS = [
  { code: 'R01', label: 'Dhaka Region', net_sales: 15_000_000 },
  { code: 'R02', label: 'Chattogram Region', net_sales: 7_200_000 },
];

/** The configured blue. Nothing in the frontend may hardcode it. */
const AREA_STYLE = {
  entity_type: 'upazila',
  configured: true,
  fill_color: '#2563EB',
  fill_opacity: 0.2,
  stroke_color: '#2563EB',
  stroke_opacity: 1,
  stroke_width: 1.5,
  z_index: 1,
  rules: null,
  is_system_default: true,
};

const CONFIG = {
  basemap: {
    engine: 'maplibre-gl',
    provider: 'openfreemap',
    style_url: 'https://tiles.openfreemap.org/styles/positron',
    style_url_dark: 'https://tiles.openfreemap.org/styles/dark',
    attribution: '© OpenStreetMap contributors, © OpenFreeMap',
  },
  default_view: { latitude: 23.777, longitude: 90.399, zoom: 7 },
  levels: [
    { key: 'zone', label: 'Zone', depth: 0, child: 'region', parent: null },
    { key: 'region', label: 'Region', depth: 1, child: 'area', parent: 'zone' },
    { key: 'area', label: 'Area', depth: 2, child: null, parent: 'region' },
  ],
  metrics: [
    { key: 'net_sales', label: 'Net Sales', unit: 'currency', inverse: false, description: '' },
  ],
  cluster_threshold: 300,
  coverage: [
    { entity_type: 'region', label: 'Region', total: 4, placed: 4, derived: 4, missing: 0 },
  ],
  has_locations: true,
  currency: 'BDT',
  administrative_areas: {
    layer_key: 'admin_area',
    default_level: 'upazila',
    levels: [],
    styles: { upazila: AREA_STYLE },
    coverage: [],
    default_stock: 1,
    has_boundaries: true,
  },
};

/** `/api/map/areas?geometry=false` — the metric, with the polygons left local. */
const AREA_METRICS = {
  type: 'FeatureCollection',
  level: 'district',
  layer_key: 'admin_area',
  style: AREA_STYLE,
  total: 1,
  returned: 1,
  truncated: false,
  boundaries_loaded: true,
  unresolved_territories: [],
  bounds: null,
  period: { type: 'THIS_MONTH', date_from: '2026-08-01', date_to: '2026-08-31', label: 'This Month' },
  metric: 'stock',
  default_stock: 1,
  features: [
    {
      type: 'Feature',
      id: 'BD3026',
      bbox: [90, 24, 91, 25],
      geometry: null,
      properties: {
        district_code: 'BD3026', district_name: 'Mymensingh',
        stock: 1, stock_source: 'default', warehouse_count: 0,
      },
    },
  ],
};

function marker(entityType: string) {
  return {
    entity_type: entityType,
    entity_code: null,
    design_id: 1,
    design_name: `${entityType} Default`,
    source: 'system_default',
    marker: { kind: 'svg' },
    preview_svg:
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4"/></svg>',
    anchor: { x: 5, y: 5 },
    size: { width: 10, height: 10 },
  };
}

const ENTITIES = {
  period: { type: 'THIS_MONTH', date_from: '2026-08-01', date_to: '2026-08-31', label: 'This Month' },
  filters: {},
  applied_filters: {},
  scope_description: 'all regions',
  metric: 'net_sales',
  metric_label: 'Net Sales',
  entities: [
    entity(),
    entity({ code: 'REG004', id: 'REG004', name: 'Khulna', latitude: 22.87, longitude: 89.5403, value: 1200000 }),
    entity({
      type: 'customer', id: 'C001', code: 'C001', name: 'Ali Traders',
      parent_type: 'sub_territory', parent_id: 'STR001', parent_code: 'STR001',
      latitude: 23.75, longitude: 90.38, value: null, location_source: 'UPLOAD',
    }),
  ],
  markers: { region: marker('region'), customer: marker('customer') },
  layers: ['region', 'customer'],
  counts: { region: 3, customer: 1 },
  placed_counts: { region: 2, customer: 1 },
  totals: { entities: 4, placed: 3, unplaced: 1 },
  clusters: {},
  unplaced: [{ type: 'region', code: 'REG009', name: 'Barishal', reason: 'no_coordinate' }],
  bounds: {
    north: 23.9, south: 22.8, east: 90.5, west: 89.5,
    centre: { latitude: 23.3, longitude: 90.0 },
  },
};

const EMPTY_GEOJSON = { type: 'FeatureCollection', features: [], bbox: [88, 20.5, 92.7, 26.7] };

function wrap(ui: React.ReactNode, route = '/map') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            {/* The bar the map renders reads FilterContext — the same provider
                App.tsx puts above every page. */}
            <FilterProvider>{ui}</FilterProvider>
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** The map instance the page created, once it has finished mounting. */
async function mountedMap(): Promise<FakeMap> {
  await waitFor(() => expect(FakeMap.last).not.toBeNull());
  const map = FakeMap.last as FakeMap;
  await waitFor(() => expect(map.layers.has(IDS.entityIcons)).toBe(true));
  return map;
}

describe('MapPage', () => {
  let entitiesSpy: ReturnType<typeof vi.spyOn>;
  let areasSpy: ReturnType<typeof vi.spyOn>;
  let geoFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    clearGeoCache();
    FakeMap.last = null;

    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });

    // Marker artwork is decoded through an <img>; jsdom implements neither
    // `createObjectURL` nor image decoding, so both are stubbed rather than left
    // to time out. Only the two statics are replaced — stubbing `URL` wholesale
    // would take the constructor with it, which `fetch` needs.
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:marker');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    Object.defineProperty(globalThis.Image.prototype, 'src', {
      configurable: true,
      set(this: HTMLImageElement) {
        queueMicrotask(() => this.onload?.(new Event('load')));
      },
    });

    geoFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => EMPTY_GEOJSON });
    vi.stubGlobal('fetch', geoFetch);

    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG as never);
    entitiesSpy = vi
      .spyOn(services.mapService, 'entities')
      .mockResolvedValue(ENTITIES as never);
    areasSpy = vi
      .spyOn(services.mapService, 'areas')
      .mockResolvedValue(AREA_METRICS as never);
    // The ranking panel reads the Performance page's endpoint. Stubbed here for
    // the same reason the map's own calls are: these tests are about the map.
    vi.spyOn(services.performanceService, 'page').mockResolvedValue({
      level: 'region',
      next_level: 'area',
      drill_chain: ['zone', 'region', 'area', 'unit', 'territory', 'sub_territory'],
      performance: { rows: RANKING_ROWS, row_count: RANKING_ROWS.length,
                     truncated: false, notes: [] },
      achievement: {
        rows: [{ code: 'R01', achievement_percent: 88.4 }],
        row_count: 1, truncated: false, notes: [],
        // Deliberately not the sum of the rows above: these are the scope's own
        // totals as the server states them, and the page must show these.
        values: { target: 24_000_000, actual: 21_000_000, achievement_percent: 87.5 },
      },
    } as never);
    vi.spyOn(services.markerService, 'legend').mockResolvedValue({
      generation: 1,
      entries: [
        {
          entity_type: 'region', label: 'Region', group: 'Organisation', promoted: true,
          design_name: 'Region Default', source: 'system_default', design_id: 1,
          entity_code: null, marker: {},
          preview_svg: '<svg xmlns="http://www.w3.org/2000/svg"/>',
          anchor: { x: 0, y: 0 }, size: { width: 10, height: 10 },
        },
      ],
    } as never);
  });

  afterEach(() => vi.unstubAllGlobals());

  // --- the engine and the basemap ------------------------------------------

  it('draws with MapLibre over the basemap the server named', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    const map = await mountedMap();
    expect(map.styleUrl).toContain('openfreemap.org');
  });

  it('loads administrative geometry from local files, never from an API', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    await mountedMap();

    const requested = geoFetch.mock.calls.map((call) => call[0] as string);
    expect(requested).toEqual(expect.arrayContaining([
      GEO_FILES.country, GEO_FILES.division, GEO_FILES.district,
      GEO_FILES.upazila, GEO_FILES.mask,
    ]));
    // Every *geometry* request is a static asset path, not an endpoint. The
    // page makes other calls through the same stubbed fetch — period options,
    // for one — and those are not geometry; what must never happen is a
    // boundary being asked for from the server.
    const geometry = requested.filter((url) => url.includes('geojson'));
    expect(geometry.length).toBeGreaterThan(0);
    for (const url of geometry) expect(url).toMatch(/^\/geo\//);
  });

  it('asks the server for area figures without asking for the polygons again', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    await mountedMap();

    await waitFor(() => expect(areasSpy).toHaveBeenCalled());
    const query = areasSpy.mock.calls.at(-1)?.[0] as { geometry?: boolean };
    expect(query.geometry).toBe(false);
  });

  it('sends no renderer or API-key parameter anywhere', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    await mountedMap();

    for (const spy of [entitiesSpy, areasSpy]) {
      for (const call of spy.mock.calls) {
        expect(JSON.stringify(call[0] ?? {})).not.toMatch(/renderer|api_key|google/i);
      }
    }
  });

  // --- the Bangladesh focus effect -----------------------------------------

  it('dims the world outside Bangladesh with a mask over the basemap', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    const mask = map.layers.get(IDS.maskLayer);
    expect(mask?.type).toBe('fill');
    expect(mask?.paint?.['fill-opacity']).toBeGreaterThan(0);
    // The mask is added before the boundaries and the markers, so it dims the
    // basemap without dimming what the dashboard put on top of it.
    const order = [...map.layers.keys()];
    expect(order.indexOf(IDS.maskLayer)).toBeLessThan(order.indexOf(IDS.entityIcons));
  });

  it('frames Bangladesh on load instead of guessing a zoom', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    // The bbox comes from the country file itself.
    await waitFor(() => expect(map.fitted.length).toBeGreaterThan(0));
    expect(map.fitted.at(-1)).toEqual(EMPTY_GEOJSON.bbox);
  });

  // --- administrative boundaries -------------------------------------------

  it('draws each level from its own local source, keyed on the P-code', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    for (const level of ['country', 'division', 'district', 'upazila'] as const) {
      const source = map.sources.get(IDS.boundarySource(level));
      expect(source, level).toBeDefined();
      // `promoteId` is what makes hover and selection addressable by code.
      expect(source?.promoteId).toBe('code');
      expect(map.layers.has(IDS.boundaryLine(level))).toBe(true);
    }
  });

  it('paints boundaries with the configured colour, not a hardcoded one', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    await waitFor(() => {
      const line = map.layers.get(IDS.boundaryLine('upazila'));
      expect(line?.paint?.['line-color']).toBe(AREA_STYLE.stroke_color);
    });
    const line = map.layers.get(IDS.boundaryLine('upazila'));
    expect(line?.paint?.['line-width']).toBe(AREA_STYLE.stroke_width);
  });

  it('draws the layers once a slow style parses, rather than dropping them', async () => {
    // The regression: `applyLayers` returned when the style was not ready and
    // nothing asked again, so a style that parsed a moment after the map
    // reported `load` left a basemap with no boundaries, no mask and no
    // markers — permanently, and with nothing logged.
    FakeMap.nextStyleLoaded = false;
    try {
      const { default: MapPage } = await import('../pages/MapPage');
      wrap(<MapPage />);
      await waitFor(() => expect(FakeMap.last).not.toBeNull());
      const map = FakeMap.last as FakeMap;

      // Nothing is drawn while the style is still parsing — correctly so.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(map.layers.get(IDS.boundaryLine('country'))).toBeUndefined();

      // The style finishes. The deferred request must now run on its own.
      map.parseStyle();

      await waitFor(() => {
        expect(map.layers.get(IDS.boundaryLine('country'))).toBeDefined();
      });
      expect(map.layers.has(IDS.maskLayer)).toBe(true);
      expect(map.layers.has(IDS.entityIcons)).toBe(true);
    } finally {
      FakeMap.nextStyleLoaded = true;
    }
  });

  it('never discards a slow basemap — only a failed one falls back', async () => {
    // The regression: a watchdog swapped in the empty style once a grace period
    // expired. `setStyle` discards every source and layer the basemap had, so a
    // basemap that was merely slow was destroyed and could never come back —
    // the map kept its boundaries and lost its tiles for good. Slow is not
    // failed, and waiting must cost the basemap nothing.
    FakeMap.nextStyleLoaded = false;
    try {
      const { default: MapPage } = await import('../pages/MapPage');
      wrap(<MapPage />);
      await waitFor(() => expect(FakeMap.last).not.toBeNull());
      const map = FakeMap.last as FakeMap;

      // The style is still on its way. Whatever else happens, nothing may
      // replace it — that is what made the basemap unrecoverable.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(map.setStyleCalls).toHaveLength(0);

      // It arrives late. The local layers go on, over the real basemap.
      map.parseStyle();
      await waitFor(() => {
        expect(map.layers.get(IDS.boundaryLine('country'))).toBeDefined();
      });
      expect(map.setStyleCalls).toHaveLength(0);
      expect(map.styleUrl).toContain('positron');
    } finally {
      FakeMap.nextStyleLoaded = true;
    }
  });

  it('offers a mode for every real metric and disables the ones with no data', async () => {
    // The rule this pins: a mode is available only when the server publishes the
    // metric behind it, and one that carries a reason is always disabled. The
    // map must never label itself "Sales Achievement" over a fill it computed
    // from something else, and must never invent the number it lacks.
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    const select = (await screen.findByLabelText('Map view')) as HTMLSelectElement;
    const options = () =>
      Object.fromEntries([...select.options].map((o) => [o.value, o.disabled]));

    // Availability is derived from `/api/map/config`, so it settles only once
    // that query resolves — before then every metric mode is correctly disabled.
    await waitFor(() => expect(options().administrative).toBe(false));
    const byValue = options();

    // Availability follows the server's metric list, which this fixture sets to
    // `net_sales` alone. The measuring modes read `achievement`, which this
    // fixture does not publish, so they are correctly unavailable here even
    // though the real deployment now publishes it. That is the contract: the
    // frontend never assumes a metric exists.
    expect(byValue.bubble).toBe(true);
    expect(byValue.performance).toBe(true);

    // Modes needing no metric are available whenever the map is.
    expect(byValue.administrative).toBe(false);
    expect(byValue.density).toBe(false);

    // Declared, listed, and inert because the platform holds no such data.
    expect(byValue.promotion).toBe(true);
  });

  it('takes the global filter bar, and keeps its own controls out of the map', async () => {
    // The map sends the global filters already — they go into all three of its
    // queries — so the bar is the control for a scope the endpoints have always
    // honoured. Everything the reader *sets* now lives in the page: what is
    // drawn, at what grain, and which administrative levels are on. What is
    // left over the map is only the two framings, which act on the camera and
    // on nothing else.
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    expect(await screen.findByTestId('filter-bar')).toBeInTheDocument();

    // The overlay only exists once the map itself has mounted.
    await mountedMap();
    const overlay = screen
      .getByRole('button', { name: 'Fit to Bangladesh' })
      .closest('.absolute');
    expect(overlay).not.toBeNull();
    for (const control of [
      screen.getByLabelText('Map view'),
      screen.getByLabelText('Sales level'),
      screen.getByLabelText('Division'),
    ]) {
      expect(overlay).not.toContainElement(control);
    }
  });

  it('shows the scope totals the server states, and derives none of them', async () => {
    // The strip's money and percentage are `get_target_achievement`'s own
    // totals. The stub's totals are deliberately not the sum of the rows beside
    // them, so a browser-side sum would show a different number and fail here —
    // which is the whole point: no business figure is computed in the browser.
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    expect(await screen.findByText('87.5%')).toBeInTheDocument();
    expect(screen.getByText('৳2.10 Cr')).toBeInTheDocument();
    expect(screen.getByText('৳2.40 Cr')).toBeInTheDocument();
  });

  it('offers achievement bands only where the map measures achievement', async () => {
    // A mode that measures nothing has no band to filter on, and a control that
    // silently did nothing would be worse than no control at all.
    const { default: MapPage } = await import('../pages/MapPage');

    wrap(<MapPage />, '/map');
    await mountedMap();
    expect(screen.queryByText('Achievement bands')).toBeNull();

    cleanup();
    wrap(<MapPage />, '/map?mapMode=performance');
    expect(await screen.findByText('Achievement bands')).toBeInTheDocument();

    const chips = ['Below 50%', '50-70%', '70-90%', '90-100%', 'Above 100%'].map(
      (label) => screen.getByRole('button', { name: label }),
    );
    expect(chips.every((chip) => chip.getAttribute('aria-pressed') === 'true')).toBe(true);

    fireEvent.click(chips[0]);
    expect(chips[0]).toHaveAttribute('aria-pressed', 'false');

    // The last one on cannot be switched off: an empty map with every chip dark
    // reads as "no data" rather than as a filter somebody set.
    chips.slice(1).forEach((chip) => fireEvent.click(chip));
    expect(chips.filter((chip) => chip.getAttribute('aria-pressed') === 'true'))
      .toHaveLength(1);
  });

  it('reads the map mode from the URL, and falls back when it is not a mode', async () => {
    // The mode belongs in the URL for the same reason the filters do: a map
    // someone is reading should survive a refresh and be shareable as what it
    // shows. A stale or hand-typed value must fall back rather than break the
    // page, which is the case a shared link degrades into once a mode is renamed.
    const { default: MapPage } = await import('../pages/MapPage');

    wrap(<MapPage />, '/map?mapMode=density');
    let select = (await screen.findByLabelText('Map view')) as HTMLSelectElement;
    expect(select.value).toBe('density');

    cleanup();
    wrap(<MapPage />, '/map?mapMode=not_a_real_mode');
    select = (await screen.findByLabelText('Map view')) as HTMLSelectElement;
    expect(select.value).toBe('administrative');

    cleanup();
    wrap(<MapPage />, '/map');
    select = (await screen.findByLabelText('Map view')) as HTMLSelectElement;
    expect(select.value).toBe('administrative');
  });

  it('adds no duplicate source or layer when applied repeatedly', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();
    const sources = map.sources.size;
    const layers = map.layers.size;

    // A style swap re-runs the whole application; so does any prop change that
    // rebuilds it. Neither may accumulate a second copy of anything.
    map.fire('styledata', {});
    map.fire('styledata', {});
    await waitFor(() => expect(map.layers.has(IDS.entityIcons)).toBe(true));

    expect(map.sources.size).toBe(sources);
    expect(map.layers.size).toBe(layers);
  });

  it('keeps every boundary blue and the hierarchy carried by weight and value', () => {
    // The fallback is what paints the first frame before the API answers, so it
    // has to agree with the configured database rather than approximate it.
    // These four are the values `map_area_styles` holds; changing one here
    // without changing the row is what this pins.
    expect(FALLBACK_AREA_STYLES.country.stroke_color).toBe('#1D4ED8');
    expect(FALLBACK_AREA_STYLES.division.stroke_color).toBe('#2563EB');
    expect(FALLBACK_AREA_STYLES.district.stroke_color).toBe('#60A5FA');
    expect(FALLBACK_AREA_STYLES.upazila.stroke_color).toBe('#93C5FD');

    // Weight: broader is heavier.
    expect(FALLBACK_AREA_STYLES.country.stroke_width)
      .toBeGreaterThan(FALLBACK_AREA_STYLES.division.stroke_width);
    expect(FALLBACK_AREA_STYLES.division.stroke_width)
      .toBeGreaterThan(FALLBACK_AREA_STYLES.district.stroke_width);
    expect(FALLBACK_AREA_STYLES.district.stroke_width)
      .toBeGreaterThan(FALLBACK_AREA_STYLES.upazila.stroke_width);

    // Value: broader is darker, and every level stays one hue — a blue whose
    // blue channel dominates. Four unrelated colours would read as four
    // unrelated things, which is the thing this ordering exists to prevent.
    const lightness = (hex: string) => parseInt(hex.slice(1, 3), 16);
    for (const level of ['country', 'division', 'district', 'upazila'] as const) {
      const [r, g, b] = [1, 3, 5].map((i) =>
        parseInt(FALLBACK_AREA_STYLES[level].stroke_color.slice(i, i + 2), 16),
      );
      expect(b).toBeGreaterThan(r);
      expect(b).toBeGreaterThan(g);
    }
    expect(lightness(FALLBACK_AREA_STYLES.country.stroke_color))
      .toBeLessThan(lightness(FALLBACK_AREA_STYLES.district.stroke_color));
    expect(lightness(FALLBACK_AREA_STYLES.district.stroke_color))
      .toBeLessThan(lightness(FALLBACK_AREA_STYLES.upazila.stroke_color));
  });

  // --- business data --------------------------------------------------------

  it('puts the drawn business level in one source, whichever level it is', async () => {
    // One source for business data, not one per level — that is what lets a
    // filter change swap the contents with `setData` instead of rebuilding
    // layers. The Sales Level control decides *which* level fills it; the
    // guarantee is that it is always exactly one source.
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />, '/map?salesLevel=region');
    const map = await mountedMap();

    await waitFor(() => {
      const source = map.sources.get(IDS.entitySource)?.data as {
        features: { properties: { code: string } }[];
      };
      expect(source.features.map((f) => f.properties.code).sort())
        .toEqual(['REG001', 'REG004']);
    });

    // And the customers are reachable through the same one source.
    cleanup();
    FakeMap.last = null; // or `mountedMap` returns the map we just unmounted
    wrap(<MapPage />, '/map?salesLevel=customer');
    const second = await mountedMap();
    await waitFor(() => {
      const source = second.sources.get(IDS.entitySource)?.data as {
        features: { properties: { code: string } }[];
      };
      expect(source.features.map((f) => f.properties.code)).toEqual(['C001']);
    });
  });

  it('registers the designed marker artwork as map images', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    await waitFor(() => expect(map.images.has(iconName('region'))).toBe(true));
    expect(map.images.has(iconName('customer'))).toBe(true);
  });

  it('changes what is drawn without narrowing the scope', async () => {
    // The rule this has always guarded: choosing what to *look at* is not the
    // same as choosing what is *in scope*. It used to be a layer toggle and is
    // now the Sales Level control, but the guarantee is unchanged — changing it
    // must not smuggle a filter into the query.
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    await mountedMap();

    const select = (await screen.findByLabelText('Sales level')) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'region' } });

    await waitFor(() => {
      const last = entitiesSpy.mock.calls.at(-1)?.[0] as { layers: string };
      expect(last.layers.split(',')).toContain('region');
    });
    const last = entitiesSpy.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last.region_code).toBeUndefined();
  });

  it('drills into the entity that was clicked', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    map.click(IDS.entityIcons, {
      properties: {
        code: 'REG001', name: 'Dhaka', entityType: 'region',
        parentType: 'zone', parentCode: 'Z001', value: 7893636,
        latitude: 23.8041, longitude: 90.3868, inFocus: true, locationSource: 'DERIVED',
      },
      layer: { id: IDS.entityIcons },
    });

    await waitFor(() => {
      const last = entitiesSpy.mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(last.region_code).toBe('REG001');
    });
    expect(screen.getByRole('navigation', { name: 'Drill path' })).toHaveTextContent('Dhaka');
  });

  it('shows the area figure when a boundary is clicked', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();
    await waitFor(() => expect(areasSpy).toHaveBeenCalled());

    map.click(IDS.boundaryFill('district'), {
      id: 'BD3026',
      properties: { code: 'BD3026', name: 'Mymensingh' },
      layer: { id: IDS.boundaryFill('district') },
    });

    expect(await screen.findByText('Mymensingh')).toBeInTheDocument();
    expect(screen.getByText('BD3026')).toBeInTheDocument();
    // The metric came from the API, keyed on the code the local polygon carries.
    expect(screen.getByText(/No stock is attributable to this area yet/))
      .toBeInTheDocument();
  });

  // --- honesty and failure --------------------------------------------------

  it('reports entities that have data but no coordinate', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    await waitFor(() => expect(screen.getByText('Barishal')).toBeInTheDocument());
    expect(screen.getByRole('heading', { name: 'Not on the map' })).toBeInTheDocument();
  });

  it('prompts for coordinates when nothing has been placed', async () => {
    vi.spyOn(services.mapService, 'config')
      .mockResolvedValue({ ...CONFIG, has_locations: false } as never);
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    await waitFor(() =>
      expect(screen.getByText('No coordinates have been loaded yet')).toBeInTheDocument(),
    );
  });

  it('survives a payload with no entities', async () => {
    entitiesSpy.mockResolvedValue({
      ...ENTITIES,
      entities: [], unplaced: [], counts: {}, placed_counts: {}, bounds: null,
      totals: { entities: 0, placed: 0, unplaced: 0 },
    } as never);
    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);

    await waitFor(() => expect(screen.getByLabelText('Map view')).toBeInTheDocument());
    const map = await mountedMap();
    const source = map.sources.get(IDS.entitySource)?.data as { features: unknown[] };
    expect(source.features).toHaveLength(0);
  });

  it('keeps the page up when a boundary file cannot be loaded', async () => {
    // One missing local file must not take the dashboard down with it — the
    // remaining layers still draw and the problem is stated, not swallowed.
    geoFetch.mockImplementation((url: string) =>
      url === GEO_FILES.upazila
        ? Promise.resolve({ ok: false, status: 404 })
        : Promise.resolve({ ok: true, json: async () => EMPTY_GEOJSON }),
    );

    const { default: MapPage } = await import('../pages/MapPage');
    wrap(<MapPage />);
    const map = await mountedMap();

    expect(map.layers.has(IDS.boundaryLine('district'))).toBe(true);
    expect(await screen.findByText(/build_map_geojson/)).toBeInTheDocument();
  });

  it('tears the map down when the page unmounts', async () => {
    const { default: MapPage } = await import('../pages/MapPage');
    const { unmount } = wrap(<MapPage />);
    const map = await mountedMap();

    unmount();
    expect(map.removed).toBe(true);
  });
});
