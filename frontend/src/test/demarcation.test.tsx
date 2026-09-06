/**
 * The Area Demarcation tab over a stubbed MapLibre.
 *
 * Same seam as the other map tests: the engine is a recorder, and what is
 * pinned is what the page asks the map to do — which levels it fetches, that it
 * fetches them from the *coordinates* endpoint and not the figures one, that
 * each level becomes a symbol layer with its own icon, and that the two
 * symbol-layer traps are handled (an icon that must exist before the layer
 * references it, and MapLibre's default of hiding colliding symbols).
 *
 * jsdom has no 2D canvas context, so the shapes never actually rasterise here.
 * That is deliberate rather than tolerated: these tests are about the calls,
 * and a test that needed real pixels would be a canvas test wearing a map
 * test's name.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider, LOCATION_FILTERS } from '../contexts/FilterContext';
import BusinessMapPage from '../pages/BusinessMapPage';
import * as services from '../services';
import type {
  MapDesign, MapLayerConfig, MapLocationLayer, MapLocationsResponse,
} from '../types/api';
import { CONFIG, DESIGN, layerConfig } from './mapFixtures';

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

const ROUTE = '/map?tab=demarcation';

/** A demarcation layer: circle for region, square for territory. */
function demarcationLayer(
  level: string,
  order: number,
  overrides: Partial<MapLayerConfig> = {},
): MapLayerConfig {
  const base = layerConfig(level, order, overrides);
  return {
    ...base,
    style: {
      ...base.style,
      shape: level === 'region' ? 'circle' : 'square',
      point_color: level === 'region' ? '#2563eb' : '#db2777',
    },
  };
}

const DEMARCATION: MapDesign = {
  ...DESIGN,
  design_id: 9,
  name: 'Area Demarcation',
  purpose: 'demarcation',
  layers: [demarcationLayer('region', 1), demarcationLayer('territory', 2)],
};

function point(level: string, code: string, name: string, derived = false) {
  return {
    type: 'Feature' as const,
    id: `${level}:${code}`,
    geometry: { type: 'Point' as const, coordinates: [90.4 + Math.random() * 0.1, 23.7] as [number, number] },
    properties: {
      code, name, level,
      parent_level: null, parent_code: null,
      source: derived ? 'DERIVED' : 'UPLOAD',
      precision: 'EXACT',
      derived_from: derived ? 4 : null,
    },
  };
}

function locationLayer(
  level: string,
  order: number,
  points: ReturnType<typeof point>[],
  extra: Partial<MapLocationLayer> = {},
): MapLocationLayer {
  return {
    level,
    label: level === 'region' ? 'Region' : 'Territory',
    features: { type: 'FeatureCollection', features: points },
    total: points.length + (extra.missing ?? 0),
    placed: points.length,
    available: points.length,
    derived: points.filter((p) => p.properties.source === 'DERIVED').length,
    missing: 0,
    bounds: points.length
      ? { west: 90.3, east: 90.6, south: 23.6, north: 23.9,
          centre: { latitude: 23.75, longitude: 90.45 } }
      : null,
    notes: [],
    layer: demarcationLayer(level, order),
    ...extra,
  };
}

const RESPONSE: MapLocationsResponse = {
  design: DEMARCATION,
  levels: ['region', 'territory'],
  filters: {},
  scope_note: null,
  empty: false,
  layers: [
    locationLayer('region', 1, [point('region', 'REG001', 'Dhaka'),
                                point('region', 'REG002', 'Khulna', true)]),
    locationLayer('territory', 2, [point('territory', 'TR001', 'Kazipara')]),
  ],
};

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

async function fakeMap() {
  const { FakeMap } = await import('./fakeMapLibre');
  return FakeMap.last!;
}

describe('Area Demarcation tab', () => {
  let locations: ReturnType<typeof vi.spyOn>;
  let data: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    const { FakeMap } = await import('./fakeMapLibre');
    FakeMap.instances = [];
    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(services.mapService, 'designs').mockImplementation((_inactive, purpose) =>
      Promise.resolve(
        purpose === 'demarcation'
          ? { purpose: 'demarcation' as const, designs: [DEMARCATION], default_design_id: 9 }
          : { purpose: 'analysis' as const, designs: [DESIGN], default_design_id: 1 },
      ));
    locations = vi.spyOn(services.mapService, 'locations')
      .mockResolvedValue(RESPONSE);
    data = vi.spyOn(services.mapService, 'data');
  });

  it('is reachable by its tab and draws the demarcation design', async () => {
    wrap();
    expect(await screen.findByRole('button', { name: 'Area Demarcation' })).toBeInTheDocument();
    await waitFor(() => expect(locations).toHaveBeenCalled());
    const call = locations.mock.calls[0][0] as { design_id?: number; levels?: string[] };
    expect(call.design_id).toBe(9);
    expect(call.levels).toEqual(['region', 'territory']);
  });

  it('reads no fact table: the figures endpoint is never called', async () => {
    wrap();
    await waitFor(() => expect(locations).toHaveBeenCalled());
    // The whole point of the second tab. `/api/map/data` aggregates sales and
    // targets; this map has no period and no metric to aggregate over.
    expect(data).not.toHaveBeenCalled();
  });

  it('draws no period control and no metric selector', async () => {
    wrap();
    await waitFor(() => expect(locations).toHaveBeenCalled());
    // A coordinate has no date and no measure. The filter bar itself is here —
    // see below — but neither of these two controls could change a point on
    // this map, and a control whose only outcome is nothing is absent.
    expect(screen.queryByLabelText('Metric')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Period')).not.toBeInTheDocument();

    // The contrast is what makes the two assertions above mean anything: the
    // analysis tab draws both, from the same bar, in the same page.
    fireEvent.click(screen.getByRole('button', { name: 'Business Map' }));
    expect(await screen.findByLabelText('Period')).toBeInTheDocument();
    expect(await screen.findByLabelText('Metric')).toBeInTheDocument();
  });

  /**
   * The browser's filter set and the server's must name the same levels.
   *
   * `LOCATION_FILTERS` decides which controls the bar draws; `location_filters`
   * on `GET /api/map/config` is *derived* from `map.levels.MAP_LEVELS` and
   * decides which the endpoint will narrow by. A level added to the chain that
   * reached only one of them would either offer a control that does nothing or
   * hide a narrowing that works — the stale-list failure the top of `CLAUDE.md`
   * opens with, in both directions.
   */
  it('offers exactly the filters the server says it can narrow by', () => {
    expect([...LOCATION_FILTERS].sort())
      .toEqual([...CONFIG.location_filters].sort());
  });

  it('draws the filter bar for coordinates, not the analysis map’s', async () => {
    wrap('/map?tab=demarcation');
    await waitFor(() => expect(locations).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: /Filters/ }));
    // Company heads the organisational chain, so it narrows a coordinate.
    expect(await screen.findByLabelText('Company')).toBeInTheDocument();
    // A coordinate has no material and no batch, so neither is offered: those
    // are `MAP_FILTERS`, and this tab is not that map.
    expect(screen.queryByLabelText('Material')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Batch')).not.toBeInTheDocument();
  });

  it('sends the filter to the server rather than narrowing in the browser', async () => {
    wrap('/map?tab=demarcation&region_code=REG001');
    await waitFor(() => expect(locations).toHaveBeenCalled());
    const call = locations.mock.calls[0][0] as Record<string, unknown>;
    // The narrowing is a subtree resolved against the org hierarchy, which the
    // browser holds none of. It travels as a parameter and comes back applied.
    expect(call.region_code).toBe('REG001');
  });

  it('does not send a filter this map cannot honour', async () => {
    wrap('/map?tab=demarcation&material_code=M1&batch_code=B1');
    await waitFor(() => expect(locations).toHaveBeenCalled());
    const call = locations.mock.calls[0][0] as Record<string, unknown>;
    // Left in the URL by the analysis tab. Sending them would be a request the
    // endpoint must either ignore in silence or refuse.
    expect(call.material_code).toBeUndefined();
    expect(call.batch_code).toBeUndefined();
    // Nor a period. A coordinate has no date, so a range in the request would
    // only put itself in the cache key and refetch for an identical answer.
    expect(call.period).toBeUndefined();
    expect(call.date_from).toBeUndefined();
  });

  it('gives every level its own source and a symbol layer', async () => {
    wrap();
    await waitFor(() => expect(locations).toHaveBeenCalled());
    const map = await fakeMap();
    await waitFor(() => expect(map.sources.has('business-map-region')).toBe(true));
    expect(map.sources.has('business-map-territory')).toBe(true);

    const points = map.layers.get('business-map-region-points');
    expect(points?.type).toBe('symbol');
  });

  it('never lets MapLibre hide a colliding point', async () => {
    // The trap that makes this a separate renderer: symbol layers cull by
    // default, so a demarcation map would silently drop its densest points.
    wrap();
    await waitFor(() => expect(locations).toHaveBeenCalled());
    const map = await fakeMap();
    await waitFor(() => expect(map.layers.has('business-map-region-points')).toBe(true));
    const layout = map.layers.get('business-map-region-points')?.layout as Record<string, unknown>;
    expect(layout['icon-allow-overlap']).toBe(true);
    expect(layout['icon-ignore-placement']).toBe(true);
  });

  it('lists each level with its placed count in the legend', async () => {
    wrap();
    // Scoped to the legend: the layer toggles above the map also name every
    // level, and an unscoped query would match either and prove neither.
    const legend = (await screen.findByText('Levels')).closest('div')!;
    const region = within(legend).getByRole('button', { name: /Region/ });
    expect(region).toHaveTextContent('2');
    expect(within(legend).getByRole('button', { name: /Territory/ }))
      .toHaveTextContent('1');
  });

  it('reports a level that has no coordinates rather than drawing nothing', async () => {
    locations.mockResolvedValue({
      ...RESPONSE,
      layers: [
        RESPONSE.layers[0],
        locationLayer('territory', 2, [], {
          total: 154,
          missing: 154,
          notes: ['None of the 154 territory records has a coordinate.'],
        }),
      ],
    });
    wrap();
    expect(await screen.findByText(/None of the 154 territory records/)).toBeInTheDocument();
  });

  it('counts matched of available once a filter is narrowing', async () => {
    locations.mockResolvedValue({
      ...RESPONSE,
      filters: { region_code: ['REG001'] },
      layers: [
        locationLayer('region', 1, [point('region', 'REG001', 'Dhaka')],
                      { available: 8 }),
        locationLayer('territory', 2, [point('territory', 'TR001', 'Kazipara')],
                      { available: 94 }),
      ],
    });
    wrap('/map?tab=demarcation&region_code=REG001');
    // "1 point" and "1 of 94" are different findings: one is a narrow filter,
    // the other a level nobody has finished surveying.
    expect(await screen.findByText('2 of 102 points')).toBeInTheDocument();
    const legend = (await screen.findByText('Levels')).closest('div')!;
    expect(within(legend).getByRole('button', { name: /Territory/ }))
      .toHaveTextContent('1 of 94');
  });

  it('names the selection that emptied a level', async () => {
    locations.mockResolvedValue({
      ...RESPONSE,
      filters: { region_code: ['REG001'] },
      layers: [locationLayer('territory', 2, [], {
        available: 94,
        notes: ['None of the 94 placed territory coordinates is inside '
                + 'region REG001.'],
      })],
    });
    wrap('/map?tab=demarcation&region_code=REG001');
    // Never a blank map: "there is nothing there" and "look elsewhere" are
    // indistinguishable on screen and mean opposite things.
    expect(await screen.findByText(/inside region REG001/)).toBeInTheDocument();
  });

  it('explains an empty map to a reader with no data scope', async () => {
    locations.mockResolvedValue({
      ...RESPONSE,
      empty: true,
      scope_note: 'Your role restricts you to your own data scope, and no '
                  + 'scope has been granted. Ask an administrator for one.',
      layers: [locationLayer('region', 1, [], { available: 0, total: 8 })],
    });
    wrap('/map?tab=demarcation');
    expect(await screen.findByText(/no scope has been granted/)).toBeInTheDocument();
  });

  it('switching tabs drops the layer toggles that belonged to the other map', async () => {
    wrap('/map?tab=demarcation&layers=region&selected=region:REG001');
    await waitFor(() => expect(locations).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Business Map' }));
    await waitFor(() =>
      expect(window.location.search.includes('layers=region')).toBe(false));
  });
});
