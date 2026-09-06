/**
 * The administrative backdrop, on both maps.
 *
 * What these pin is the half that is easy to get wrong and invisible when it
 * is: that the outlines go *beneath* the points rather than over them, that a
 * click on a point is not stolen by the district under it, that nothing is
 * fetched until somebody asks for it, and that the map survives a boundary
 * file that will not load.
 *
 * The engine is the shared recorder, as everywhere else in these tests — what
 * is asserted is what the page asks the map to do, never what WebGL made of it.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import { BOUNDARY_LAYERS } from '../components/map/useBoundaryLayer';
import { clearGeoCache } from '../components/map/geoData';
import BusinessMapPage from '../pages/BusinessMapPage';
import * as services from '../services';
import type { MapDesign } from '../types/api';
import { CONFIG, DESIGN, REGIONS, layerConfig, rankingRow, response } from './mapFixtures';

vi.mock('maplibre-gl', async () => (await import('./fakeMapLibre')).mapLibreModule());
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({ default: 'worker.js' }));
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'ceo', role: 'MANAGEMENT' },
    hasSection: (key: string) => key === 'map',
    can: () => false,
    isAdmin: false,
  }),
}));

const WINDOW = 'period=CUSTOM&date_from=2026-08-01&date_to=2026-08-31';

const DEMARCATION: MapDesign = {
  ...DESIGN,
  design_id: 9,
  name: 'Area Demarcation',
  purpose: 'demarcation',
  layers: [layerConfig('region', 1)],
};

/** Two districts, enough to select one and not the other. */
const DISTRICTS = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: { code: 'BD1004', name: 'Barguna', parent_code: 'BD10' },
      geometry: { type: 'Polygon', coordinates: [[[90, 23], [91, 23], [91, 24], [90, 23]]] },
    },
    {
      type: 'Feature',
      properties: { code: 'BD3026', name: 'Gazipur', parent_code: 'BD30' },
      geometry: { type: 'Polygon', coordinates: [[[89, 22], [90, 22], [90, 23], [89, 22]]] },
    },
  ],
};

const MASK = { type: 'FeatureCollection', features: [] };

function wrap(route: string) {
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

/**
 * The live map, once the page has built one.
 *
 * Waited for rather than read straight after `render`: the map is created in an
 * effect, so grabbing it in the same tick yields `undefined` and a failure that
 * looks like a rendering bug rather than a timing one.
 */
async function fakeMap() {
  const { FakeMap } = await import('./fakeMapLibre');
  await waitFor(() => expect(FakeMap.last).toBeDefined());
  return FakeMap.last!;
}

describe('Administrative backdrop', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let geoResponse: { ok: boolean; status: number };

  beforeEach(async () => {
    const { FakeMap } = await import('./fakeMapLibre');
    FakeMap.instances = [];
    clearGeoCache();

    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(services.mapService, 'designs').mockImplementation((_i, purpose) =>
      Promise.resolve(
        purpose === 'demarcation'
          ? { purpose: 'demarcation' as const, designs: [DEMARCATION], default_design_id: 9 }
          : { purpose: 'analysis' as const, designs: [DESIGN], default_design_id: 1 },
      ));
    vi.spyOn(services.mapService, 'data').mockImplementation(() =>
      Promise.resolve(response('region', REGIONS, {
        top: [rankingRow('REG001', 'Dhaka', 1_500_000)], bottom: [],
      })));
    vi.spyOn(services.mapService, 'locations').mockResolvedValue({
      design: DEMARCATION,
      levels: ['region'],
      filters: {},
      scope_note: null,
      empty: false,
      layers: [{
        level: 'region', label: 'Region',
        features: { type: 'FeatureCollection', features: [] },
        total: 13, placed: 0, derived: 0, missing: 13,
        bounds: null, notes: [], layer: layerConfig('region', 1),
      }],
    } as never);

    // Only `/geo/` is answered here. The page makes its own API calls through
    // the same global, and a stub that returned polygons to `/api/period-options`
    // would be testing something that never happens.
    geoResponse = { ok: true, status: 200 };
    fetchMock = vi.fn((url: string) => {
      if (!String(url).includes('/geo/')) {
        return Promise.resolve({
          ok: true, status: 200, json: () => Promise.resolve({}),
        });
      }
      return Promise.resolve({
        ...geoResponse,
        json: () => Promise.resolve(String(url).includes('mask') ? MASK : DISTRICTS),
      });
    }) as never;
    vi.stubGlobal('fetch', fetchMock);
  });

  /** Only the boundary requests; the app's own traffic is not the subject. */
  function geoCalls(): string[] {
    return fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes('/geo/'));
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ========================================================================
  // Nothing until it is asked for
  // ========================================================================

  it('fetches no outline until a backdrop is chosen, on the Business Map', async () => {
    wrap(`/map?${WINDOW}`);
    await waitFor(() => expect(services.mapService.data).toHaveBeenCalled());
    // 370 KB of districts nobody asked for, over a connection nobody chose.
    // Unchanged by the demarcation tab opening with a backdrop: that reasoning
    // is about a map of figures, where the outlines would be ink over the
    // subject rather than the subject.
    expect(geoCalls()).toEqual([]);
  });

  it('opens Area Demarcation with the upazila outlines already asked for', async () => {
    wrap(`/map?tab=demarcation&${WINDOW}`);
    // The tab exists to judge where a line falls, so it opens able to answer
    // its own question rather than showing a blank map and a control.
    await waitFor(() => expect(geoCalls().some((u) => u.includes('admin3'))).toBe(true));
  });

  it('lets a reader turn the backdrop off and keeps it off', async () => {
    wrap(`/map?tab=demarcation&boundary=none&${WINDOW}`);
    await waitFor(() => expect(services.mapService.locations).toHaveBeenCalled());
    // `boundary=none` is spelled out for the reason `layers=none` is: an absent
    // parameter means the surface's own default, so without it the 1.7 MB a
    // reader just dismissed would come back on the next reload.
    expect(geoCalls().filter((u) => !u.includes('mask'))).toEqual([]);
  });

  it('names each set without predicting how long it will take', async () => {
    wrap(`/map?${WINDOW}`);
    const select = await screen.findByLabelText('Boundaries');
    // "Upazilas", not "Upazilas (1.7 MB)". A size in the label is a prediction,
    // and it is wrong on the second visit when the file is already cached; the
    // spinner below fires when there is actually a wait.
    expect(select).toHaveTextContent('Upazilas');
    expect(select).not.toHaveTextContent('MB');
    expect(select).not.toHaveTextContent('KB');
  });

  it('says the outlines are loading while they are', async () => {
    // Held open so the spinner has something to describe. Without this the
    // fetch resolves in the same tick and the loading state is never observed
    // — which is how it went unnoticed that the state reached no component.
    let release: (value: unknown) => void = () => undefined;
    const held = new Promise((resolve) => { release = resolve; });
    fetchMock.mockImplementation((url: string) => {
      if (!String(url).includes('/geo/')) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      }
      return held.then(() => ({
        ok: true, status: 200,
        json: () => Promise.resolve(String(url).includes('mask') ? MASK : DISTRICTS),
      }));
    });

    wrap(`/map?tab=demarcation&${WINDOW}`);
    expect(await screen.findByText('Loading outlines…')).toBeInTheDocument();
    release(undefined);
    await waitFor(() =>
      expect(screen.queryByText('Loading outlines…')).not.toBeInTheDocument());
  });

  // ========================================================================
  // Drawn beneath, on both maps
  // ========================================================================

  it('draws the outlines under the points on the Business Map', async () => {
    wrap(`/map?${WINDOW}&boundary=district`);
    const map = await fakeMap();
    await waitFor(() => expect(map.layers.has(BOUNDARY_LAYERS.fill)).toBe(true));
    expect(map.layers.has(BOUNDARY_LAYERS.line)).toBe(true);
    // `beforeId` is what keeps a polygon from covering the points it exists to
    // give context to.
    const fill = map.layers.get(BOUNDARY_LAYERS.fill) as { beforeId?: string };
    expect(fill.beforeId).toBe('business-map-region-points');
  });

  it('draws the same outlines on Area Demarcation', async () => {
    wrap(`/map?tab=demarcation&boundary=district`);
    const map = await fakeMap();
    await waitFor(() => expect(map.layers.has(BOUNDARY_LAYERS.fill)).toBe(true));
    expect(map.layers.has(BOUNDARY_LAYERS.labels)).toBe(true);
  });

  it('fetches the chosen file and the mask, and only those', async () => {
    wrap(`/map?${WINDOW}&boundary=district`);
    await waitFor(() => expect(geoCalls().length).toBeGreaterThan(0));
    const urls = geoCalls();
    expect(urls).toContain('/geo/bgd_admin2.geojson');
    expect(urls.some((url) => url.includes('mask'))).toBe(true);
    // Not the 1.7 MB upazila file, which nobody selected.
    expect(urls.some((url) => url.includes('admin3'))).toBe(false);
  });

  // ========================================================================
  // Nothing here is a business boundary
  // ========================================================================

  it('says so beside the control', async () => {
    wrap(`/map?${WINDOW}&boundary=district`);
    expect(await screen.findByText('Reference only')).toBeInTheDocument();
  });

  it('says so again beside a selected outline', async () => {
    wrap(`/map?${WINDOW}&boundary=district&barea=BD3026&bname=Gazipur`);
    expect(await screen.findByText('Gazipur')).toBeInTheDocument();
    expect(
      await screen.findByText('Administrative area — not a sales boundary'),
    ).toBeInTheDocument();
  });

  it('keeps the selection in the URL so the view is a link', async () => {
    wrap(`/map?${WINDOW}&boundary=district&barea=BD1004&bname=Barguna`);
    expect(await screen.findByText('Barguna')).toBeInTheDocument();
    expect(await screen.findByText(/BD1004/)).toBeInTheDocument();
  });

  // ========================================================================
  // A backdrop that fails is a note, not a broken map
  // ========================================================================

  it('keeps the map when the outlines cannot be loaded', async () => {
    geoResponse = { ok: false, status: 404 };
    wrap(`/map?${WINDOW}&boundary=district`);
    const map = await fakeMap();
    // The business layer is still drawn; only the backdrop is missing.
    await waitFor(() => expect(map.sources.has('business-map-region')).toBe(true));
    expect(map.layers.has(BOUNDARY_LAYERS.fill)).toBe(false);
  });
});
