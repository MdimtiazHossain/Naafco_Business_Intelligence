/**
 * The Business Map page over a stubbed MapLibre.
 *
 * MapLibre needs a WebGL context, which jsdom has no notion of, so the engine
 * is replaced by a recorder that remembers what it was created with, which
 * sources and layers it was given, what paint it was told to apply and where
 * it was asked to move. That is the right seam anyway: what these tests are
 * about is *what the page asks the map to do* — open on the configured
 * basemap, follow the theme, fetch exactly the layers a design shows, draw
 * each one as its own source in the design's order, colour by the declared
 * style, select on click, rank in tables — none of which is a question about
 * WebGL.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import { NO_SELECTION } from '../components/map/mapExpressions';
import BusinessMapPage from '../pages/BusinessMapPage';
import * as services from '../services';
import { ApiError } from '../services';
import type { MapDesign } from '../types/api';
import { formatAmount } from '../utils/format';
import {
  CONFIG, DARK, DESIGN, LIGHT, REGIONS, feature, layerConfig, rankingRow, response,
} from './mapFixtures';

// ---------------------------------------------------------------------------
// A recording stand-in for MapLibre
// ---------------------------------------------------------------------------

const { FakeMap } = vi.hoisted(() => {
  interface SourceEntry {
    options: Record<string, unknown>;
    data: unknown;
    setData: (data: unknown) => void;
    getClusterExpansionZoom: () => Promise<number>;
  }

  class FakeMap {
    static instances: FakeMap[] = [];

    static get last(): FakeMap | undefined {
      return FakeMap.instances[FakeMap.instances.length - 1];
    }

    options: Record<string, unknown>;
    controls: unknown[] = [];
    handlers = new Map<string, ((event: unknown) => void)[]>();
    sources = new Map<string, SourceEntry>();
    layers = new Map<string, Record<string, unknown>>();
    jumps: unknown[] = [];
    eases: unknown[] = [];
    fits: unknown[] = [];
    styles: unknown[] = [];
    queryHits: unknown[] = [];
    canvas = { style: { cursor: '' } };
    removed = false;

    constructor(options: Record<string, unknown>) {
      this.options = options;
      FakeMap.instances.push(this);
      queueMicrotask(() => {
        this.fire('style.load');
        this.fire('load');
      });
    }

    on(event: string, handler: (event: unknown) => void) {
      this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
      return this;
    }

    off(event: string, handler: (event: unknown) => void) {
      this.handlers.set(event, (this.handlers.get(event) ?? []).filter((h) => h !== handler));
      return this;
    }

    fire(event: string, payload: unknown = {}) {
      (this.handlers.get(event) ?? []).forEach((handler) => handler(payload));
    }

    addControl(control: unknown) {
      this.controls.push(control);
      return this;
    }

    remove() {
      this.removed = true;
    }

    setStyle(style: unknown) {
      this.styles.push(style);
      this.options = { ...this.options, style };
      // A new style carries nothing of what was drawn on the old one.
      this.sources.clear();
      this.layers.clear();
      queueMicrotask(() => this.fire('style.load'));
    }

    jumpTo(options: unknown) {
      this.jumps.push(options);
    }

    easeTo(options: unknown) {
      this.eases.push(options);
    }

    fitBounds(bounds: unknown, options: unknown) {
      this.fits.push({ bounds, options });
    }

    isStyleLoaded() {
      return true;
    }

    addSource(id: string, options: Record<string, unknown>) {
      const entry: SourceEntry = {
        options,
        data: options.data,
        setData: (data: unknown) => {
          entry.data = data;
        },
        getClusterExpansionZoom: () => Promise.resolve(10),
      };
      this.sources.set(id, entry);
    }

    getSource(id: string) {
      return this.sources.get(id);
    }

    removeSource(id: string) {
      this.sources.delete(id);
    }

    addLayer(spec: Record<string, unknown>) {
      this.layers.set(spec.id as string, { ...spec });
    }

    getLayer(id: string) {
      return this.layers.get(id);
    }

    removeLayer(id: string) {
      this.layers.delete(id);
    }

    moveLayer(id: string) {
      const spec = this.layers.get(id);
      if (spec) {
        this.layers.delete(id);
        this.layers.set(id, spec);
      }
    }

    setPaintProperty(id: string, name: string, value: unknown) {
      const spec = this.layers.get(id);
      if (spec) spec.paint = { ...(spec.paint as Record<string, unknown>), [name]: value };
    }

    setLayoutProperty(id: string, name: string, value: unknown) {
      const spec = this.layers.get(id);
      if (spec) spec.layout = { ...(spec.layout as Record<string, unknown>), [name]: value };
    }

    setFilter(id: string, filter: unknown) {
      const spec = this.layers.get(id);
      if (spec) spec.filter = filter;
    }

    setLayerZoomRange() {}

    queryRenderedFeatures() {
      return this.queryHits;
    }

    getCanvas() {
      return this.canvas;
    }
  }
  return { FakeMap };
});

vi.mock('maplibre-gl', () => ({
  Map: FakeMap,
  NavigationControl: class {
    kind = 'navigation';
  },
  FullscreenControl: class {
    kind = 'fullscreen';
  },
  AttributionControl: class {
    kind = 'attribution';
    options: unknown;
    constructor(options?: unknown) {
      this.options = options;
    }
  },
  setWorkerUrl: vi.fn(),
}));
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({
  default: 'maplibre-gl-worker.js',
}));
vi.mock('../contexts/AuthContext', () => ({
  // A reader: opens the map, does not compose it.
  useAuth: () => ({
    user: { username: 'ceo', role: 'MANAGEMENT' },
    hasSection: (key: string) => key === 'map',
    can: () => false,
    isAdmin: false,
  }),
}));

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const ROUTE = '/map?period=CUSTOM&date_from=2026-08-01&date_to=2026-08-31';

function wrap(route = ROUTE) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}>
            <FilterProvider>
              <BusinessMapPage />
            </FilterProvider>
          </MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

function levelOf(call: unknown[]): string {
  return ((call[0] as { levels?: string[] }).levels ?? [])[0];
}

function spyOnData() {
  return vi.spyOn(services.mapService, 'data');
}

const RANKING = { top: [rankingRow('REG001', 'Dhaka', 1_500_000)], bottom: [rankingRow('REG002', 'Khulna', 900_000)] };

async function loadedMap() {
  await waitFor(() => expect(FakeMap.instances).toHaveLength(1));
  const map = FakeMap.last!;
  await waitFor(() => expect(map.sources.has('business-map-region')).toBe(true));
  return map;
}

/**
 * A map event, raised the way the engine raises one — inside `act`.
 *
 * MapLibre calls its handlers from outside React, so what a handler sets is
 * *scheduled* rather than applied, and a click schedules three things in
 * three different places: the page's own selection state, the router's — which
 * react-router raises as a transition, the lowest priority React has — and
 * then, once the card is on screen, the passive effect that asks for the
 * entity's ancestry. Unwrapped, each lands whenever the scheduler next gets
 * the thread; measured under the full suite that was 2.5 seconds after the
 * click, well past the second at which `findBy*` gives up, and the assertion
 * on `mapService.entity` could observe the committed DOM before the effect
 * that fetches had run at all. `act` flushes all three before returning,
 * which is what the act warning an unwrapped `fire` prints is asking for.
 */
function fireOnMap(map: InstanceType<typeof FakeMap>, event: string, payload?: unknown): void {
  act(() => {
    map.fire(event, payload);
  });
}

function regionHit() {
  return {
    properties: { ...REGIONS[0].properties },
    geometry: REGIONS[0].geometry,
    source: 'business-map-region',
  };
}

describe('Business Map page', () => {
  let data: ReturnType<typeof spyOnData>;

  beforeEach(() => {
    FakeMap.instances.length = 0;
    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(services.mapService, 'designs').mockResolvedValue({ purpose: 'analysis', designs: [DESIGN], default_design_id: 1 });
    vi.spyOn(services.mapService, 'entity').mockImplementation((level, code) => Promise.resolve({
      level, label: 'Region', code,
      name: REGIONS.find((region) => region.properties.code === code)?.properties.name ?? code,
      ancestors: [
        { level: 'company', label: 'Company', code: 'C001', name: 'Example Industries Ltd.', known: true },
        { level: 'zone', label: 'Zone', code: 'Z001', name: 'Dhaka Zone', known: true },
      ],
      location: null,
    }));
    data = spyOnData().mockImplementation((query) => {
      const level = levelOf([query]);
      return Promise.resolve(level === 'region' ? response('region', REGIONS, RANKING) : response(level));
    });
  });

  // -- the canvas ------------------------------------------------------------

  it('opens the default design on the configured basemap and fetches only its visible layers', async () => {
    wrap();
    expect(await screen.findByRole('heading', { name: 'Business Map' })).toBeInTheDocument();

    await waitFor(() => expect(FakeMap.instances).toHaveLength(1));
    const map = FakeMap.last!;
    expect(map.options.style).toBe(LIGHT);
    expect(map.options.center).toEqual([90.356, 23.685]);
    expect(map.options.zoom).toBe(6.5);
    expect(map.controls.map((control) => (control as { kind: string }).kind)).toEqual([
      'attribution', 'navigation', 'fullscreen',
    ]);

    await waitFor(() => expect(data).toHaveBeenCalledTimes(2));
    expect(data.mock.calls.map(levelOf).sort()).toEqual(['region', 'zone']);
    expect(data.mock.calls[0][0]).toMatchObject({
      design_id: 1, date_from: '2026-08-01', date_to: '2026-08-31',
    });
    expect(await screen.findByText('2 of 2 layers drawn')).toBeInTheDocument();
    expect(screen.queryByText('Loading business map…')).not.toBeInTheDocument();
  });

  it('follows the dark theme with the basemap the deployment configured for it', async () => {
    localStorage.setItem('bi.theme', 'dark');
    wrap();
    await waitFor(() => expect(FakeMap.instances).toHaveLength(1));
    expect(FakeMap.last!.options.style).toBe(DARK);
  });

  it('wraps a raster tile provider in the style MapLibre needs, with its credit and glyphs', async () => {
    vi.spyOn(services.mapService, 'config').mockResolvedValue({
      ...CONFIG,
      basemaps: [{
        key: 'standard', label: 'Map', kind: 'raster', attribution: '© Example Tiles',
        style_url: 'https://tiles.test/{z}/{x}/{y}.png', style_url_dark: null,
        glyphs_url: 'https://fonts.test/{fontstack}/{range}.pbf',
      }],
    });
    wrap();
    await waitFor(() => expect(FakeMap.instances).toHaveLength(1));
    const style = FakeMap.last!.options.style as {
      glyphs: string;
      sources: { basemap: { tiles: string[]; attribution: string } };
      layers: { type: string }[];
    };
    expect(style.sources.basemap.tiles).toEqual(['https://tiles.test/{z}/{x}/{y}.png']);
    expect(style.sources.basemap.attribution).toBe('© Example Tiles');
    expect(style.glyphs).toBe('https://fonts.test/{fontstack}/{range}.pbf');
    expect(style.layers[0].type).toBe('raster');
  });

  // -- the renderer ----------------------------------------------------------

  it('draws every loaded layer as its own source, in the design\'s order, coloured by the declared bands', async () => {
    wrap();
    const map = await loadedMap();
    await waitFor(() => expect(map.sources.has('business-map-zone')).toBe(true));

    const region = map.sources.get('business-map-region')!;
    expect(region.options.cluster).toBe(false);
    expect((region.data as { features: unknown[] }).features).toHaveLength(2);

    const order = [...map.layers.keys()];
    expect(order.indexOf('business-map-zone-points')).toBeLessThan(order.indexOf('business-map-region-points'));
    // Labels ride above every point, whichever layer they belong to.
    expect(order.indexOf('business-map-region-points')).toBeLessThan(order.indexOf('business-map-zone-labels'));

    const paint = map.layers.get('business-map-region-points')!.paint as Record<string, unknown>;
    expect((paint['circle-color'] as unknown[])[0]).toBe('case');
    expect(JSON.stringify(paint['circle-color'])).toContain('"#16a34a"');
    expect((paint['circle-radius'] as unknown[])[0]).toBe('case');
    expect(map.layers.has('business-map-region-clusters')).toBe(false);

    // The map fitted the data once.
    expect(map.fits).toHaveLength(1);
  });

  it('clusters a layer only above the count it names, and re-adds every layer after a style swap', async () => {
    const many = Array.from({ length: 201 }, (_, i) => feature('customer', `C${i}`, `Customer ${i}`, i + 1));
    data.mockImplementation((query) => Promise.resolve(response(levelOf([query]), many)));
    wrap(`${ROUTE}&layers=customer`);
    await waitFor(() => expect(FakeMap.instances).toHaveLength(1));
    const map = FakeMap.last!;
    await waitFor(() => expect(map.sources.has('business-map-customer')).toBe(true));
    expect(map.sources.get('business-map-customer')!.options.cluster).toBe(true);
    expect(map.layers.has('business-map-customer-clusters')).toBe(true);
    expect(map.layers.has('business-map-customer-cluster-count')).toBe(true);

    // A theme switch discards the style and everything on it; the renderer
    // puts the business layers back on the new one.
    map.setStyle(DARK);
    expect(map.sources.size).toBe(0);
    await waitFor(() => expect(map.sources.has('business-map-customer')).toBe(true));
    expect(map.layers.has('business-map-customer-points')).toBe(true);
  });

  // -- selection -------------------------------------------------------------

  it('selects a point on click, shows its figures and ancestry, and remembers it in the URL', async () => {
    wrap();
    const map = await loadedMap();
    expect(screen.getByText('Select a point on the map, or a row in the tables below, to see its figures.')).toBeInTheDocument();

    map.queryHits = [regionHit()];
    fireOnMap(map, 'click', { point: { x: 10, y: 10 } });

    expect(await screen.findByRole('heading', { name: 'Dhaka' })).toBeInTheDocument();
    expect(screen.getByText('Selected: Region')).toBeInTheDocument();
    expect(screen.getAllByText(formatAmount(1_500_000)).length).toBeGreaterThan(0);
    expect(screen.getAllByText('75%').length).toBeGreaterThan(0);
    expect(services.mapService.entity).toHaveBeenCalledWith('region', 'REG001');
    expect(await screen.findByText(/Dhaka Zone/)).toBeInTheDocument();
    // The ring follows the selection.
    await waitFor(() => expect(map.layers.get('business-map-region-selected')!.filter)
      .toEqual(['==', ['get', 'code'], 'REG001']));

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(await screen.findByText(/Select a point on the map/)).toBeInTheDocument();
    await waitFor(() => expect(map.layers.get('business-map-region-selected')!.filter)
      .toEqual(['==', ['get', 'code'], NO_SELECTION]));
  });

  it('opens on the entity the URL names', async () => {
    wrap(`${ROUTE}&selected=region:REG002`);
    await loadedMap();
    expect(await screen.findByRole('heading', { name: 'Khulna' })).toBeInTheDocument();
    expect(screen.getAllByText(formatAmount(900_000)).length).toBeGreaterThan(0);
  });

  it('ranks the active layer in Top and Bottom tables, and a row selects and pans to its point', async () => {
    wrap();
    const map = await loadedMap();
    const top = (await screen.findByText('Top Region')).closest('section')!;
    const bottom = screen.getByText('Bottom Region').closest('section')!;
    expect(within(top).getByText('Dhaka')).toBeInTheDocument();
    expect(within(top).getByText(formatAmount(1_500_000))).toBeInTheDocument();
    expect(within(bottom).getByText('Khulna')).toBeInTheDocument();

    fireEvent.click(within(bottom).getByText('Khulna'));
    expect(await screen.findByRole('heading', { name: 'Khulna' })).toBeInTheDocument();
    await waitFor(() => expect(map.eases).toHaveLength(1));
    expect((map.eases[0] as { center: unknown }).center).toEqual([89.54, 22.85]);
  });

  it('shows the legend for the active layer from the declared style', async () => {
    wrap();
    await loadedMap();
    const legend = await screen.findByLabelText('Legend');
    expect(within(legend).getByText('90% and above')).toBeInTheDocument();
    expect(within(legend).getByText('Below 50%')).toBeInTheDocument();
    expect(within(legend).getByText('No data')).toBeInTheDocument();
    expect(within(legend).getByText('Colour: Achievement %')).toBeInTheDocument();
    expect(within(legend).getByText('Size: Sales Amount')).toBeInTheDocument();
    // Two layers are drawn, so the legend offers a choice of which to explain.
    const picker = within(legend).getByLabelText('Active layer') as HTMLSelectElement;
    expect(picker.value).toBe('region');
    fireEvent.change(picker, { target: { value: 'zone' } });
    expect(await screen.findByText('Top Zone')).toBeInTheDocument();
  });

  it('shows a tooltip with the layer\'s configured fields on hover, and hides it when the map moves', async () => {
    wrap();
    const map = await loadedMap();
    map.queryHits = [regionHit()];
    fireOnMap(map, 'mousemove', { point: { x: 20, y: 20 } });
    const tooltip = await screen.findByRole('tooltip');
    expect(within(tooltip).getByText('Dhaka')).toBeInTheDocument();
    expect(within(tooltip).getByText('Sales Amount')).toBeInTheDocument();
    expect(within(tooltip).getByText('Target Amount')).toBeInTheDocument();
    expect(within(tooltip).getByText('+11%')).toBeInTheDocument();
    expect(map.canvas.style.cursor).toBe('pointer');

    fireOnMap(map, 'movestart');
    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument());
  });

  // -- the reader's controls -------------------------------------------------

  it('draws the layers a reader toggled in the URL, and saves nothing', async () => {
    wrap(`${ROUTE}&layers=customer`);
    await waitFor(() => expect(data).toHaveBeenCalledTimes(1));
    expect(levelOf(data.mock.calls[0])).toBe('customer');
    expect(await screen.findByText('1 of 1 layers drawn')).toBeInTheDocument();
  });

  it('keeps the metric in the URL and sends it with every layer', async () => {
    wrap();
    const select = await screen.findByLabelText('Metric');
    expect((select as HTMLSelectElement).value).toBe('net_sales');
    await waitFor(() => expect(data).toHaveBeenCalledTimes(2));

    fireEvent.change(select, { target: { value: 'achievement' } });
    await waitFor(() => expect(data).toHaveBeenCalledTimes(4));
    const later = data.mock.calls.slice(2).map((call) => call[0]);
    expect(later.every((query) => query.metric === 'achievement')).toBe(true);
    // A metric change is not a new data set: the map stays where it was.
    expect(FakeMap.last!.fits).toHaveLength(1);
  });

  it('resets the view, the toggles and the selection', async () => {
    wrap(`${ROUTE}&layers=region&selected=region:REG001`);
    const map = await loadedMap();
    expect(await screen.findByRole('heading', { name: 'Dhaka' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    expect(map.jumps).toEqual([{ center: [90.356, 23.685], zoom: 6.5 }]);
    expect(await screen.findByText(/Select a point on the map/)).toBeInTheDocument();
    await waitFor(() => expect(data.mock.calls.map(levelOf)).toContain('zone'));
  });

  it('offers the design selector only when there is a choice, and swaps designs by URL', async () => {
    const other: MapDesign = {
      ...DESIGN, design_id: 2, name: 'Territory Focus', is_default: false, is_system_default: false,
      layers: [layerConfig('customer', 1)],
    };
    vi.spyOn(services.mapService, 'designs').mockResolvedValue({ purpose: 'analysis', designs: [DESIGN, other], default_design_id: 1 });
    wrap();
    const select = await screen.findByLabelText('Map design');
    expect((select as HTMLSelectElement).value).toBe('1');
    await waitFor(() => expect(data).toHaveBeenCalledTimes(2));

    fireEvent.change(select, { target: { value: '2' } });
    await waitFor(() => expect(data.mock.calls.map(levelOf)).toContain('customer'));
    expect(data.mock.calls.at(-1)![0].design_id).toBe(2);
  });

  // -- when it cannot draw ---------------------------------------------------

  it('says when map data cannot be loaded, and retries on request', async () => {
    data.mockRejectedValue(new ApiError(500, 'Something went wrong. Please try again.'));
    wrap();
    expect(await screen.findByText('Unable to load map data.')).toBeInTheDocument();
    const before = data.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(data.mock.calls.length).toBeGreaterThan(before));
    expect(FakeMap.last?.removed).toBe(false);
  });

  it('shows the refusal itself, without a retry, when the map is not theirs to see', async () => {
    data.mockRejectedValue(new ApiError(403, 'No data scope is configured for your account.'));
    wrap();
    expect(await screen.findByText('No data scope is configured for your account.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
  });

  it('says when nothing matches the filters, and keeps the map and its controls', async () => {
    data.mockImplementation((query) => Promise.resolve(response(levelOf([query]), [])));
    wrap();
    expect(await screen.findByText('No map data available for the selected filters.')).toBeInTheDocument();
    expect(FakeMap.instances).toHaveLength(1);
    expect(FakeMap.last?.removed).toBe(false);
    expect(screen.getByLabelText('Metric')).toBeInTheDocument();
  });
});
