/**
 * The Map Settings drawer: a reader's temporary toggles, and a composer's
 * saved designs and layers.
 *
 * Every write is asserted at the service boundary — what `replaceLayers` or
 * `createDesign` was called with — because that is the contract the backend
 * validates: the whole ordered layer list, with inherited values left
 * inherited. A refusal is shown as the server phrased it.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import { FilterProvider } from '../contexts/FilterContext';
import BusinessMapPage from '../pages/BusinessMapPage';
import * as services from '../services';
import { ApiError } from '../services';
import type { MapDesign, MapLayerInput } from '../types/api';
import { CONFIG, DESIGN, REGIONS, layerConfig, rankingRow, response } from './mapFixtures';

const auth = vi.hoisted(() => ({ composer: false }));

vi.mock('maplibre-gl', async () => (await import('./fakeMapLibre')).mapLibreModule());
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));
vi.mock('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url', () => ({ default: 'worker.js' }));
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { username: auth.composer ? 'root' : 'ceo', role: auth.composer ? 'SUPER_ADMIN' : 'MANAGEMENT' },
    hasSection: (key: string) => key === 'map' || (auth.composer && key === 'map_settings'),
    can: () => auth.composer,
    isAdmin: auth.composer,
  }),
}));

const ROUTE = '/map?period=CUSTOM&date_from=2026-08-01&date_to=2026-08-31';

const CUSTOM: MapDesign = {
  ...DESIGN,
  design_id: 2,
  name: 'Field View',
  is_default: false,
  is_system_default: false,
  layers: [layerConfig('region', 1), layerConfig('customer', 2, { is_visible: false })],
};

function wrap(route = ROUTE) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
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

function levelOf(query: unknown): string {
  return ((query as { levels?: string[] }).levels ?? [])[0];
}

async function openSettings() {
  fireEvent.click(await screen.findByRole('button', { name: 'Settings' }));
  return screen.findByRole('dialog', { name: 'Map settings' });
}

/** Typed spy on the data service, so its recorded calls keep their shape. */
function spyOnData() {
  return vi.spyOn(services.mapService, 'data');
}

describe('Map settings drawer', () => {
  let designList: MapDesign[];
  let data: ReturnType<typeof spyOnData>;

  beforeEach(() => {
    auth.composer = false;
    designList = [DESIGN];
    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG);
    vi.spyOn(services.mapService, 'designs').mockImplementation(() =>
      Promise.resolve({ purpose: 'analysis' as const, designs: designList, default_design_id: 1 }));
    vi.spyOn(services.mapService, 'entity').mockResolvedValue({
      level: 'region', label: 'Region', code: 'REG001', name: 'Dhaka', ancestors: [], location: null,
    });
    data = spyOnData().mockImplementation((query) => {
      const level = levelOf(query);
      const ranking = { top: [rankingRow('REG001', 'Dhaka', 1_500_000)], bottom: [] };
      return Promise.resolve(level === 'region' ? response('region', REGIONS, ranking) : response(level));
    });
  });

  // -- a reader ----------------------------------------------------------------

  it("lets a reader switch layers on and off for this view, and saves nothing", async () => {
    wrap();
    const drawer = await openSettings();
    expect(within(drawer).getByLabelText('Map design')).toBeInTheDocument();
    expect(within(drawer).queryByRole('button', { name: 'New design' })).not.toBeInTheDocument();
    expect(within(drawer).queryByLabelText('Edit layer: Region')).not.toBeInTheDocument();

    const zone = within(drawer).getByLabelText('Zone') as HTMLInputElement;
    const region = within(drawer).getByLabelText('Region') as HTMLInputElement;
    const customer = within(drawer).getByLabelText('Customer') as HTMLInputElement;
    expect([zone.checked, region.checked, customer.checked]).toEqual([true, true, false]);

    fireEvent.click(region);
    await waitFor(() => expect((within(drawer).getByLabelText('Region') as HTMLInputElement).checked).toBe(false));
    expect(await screen.findByText('1 of 1 layers drawn')).toBeInTheDocument();

    fireEvent.click(within(drawer).getByLabelText('Customer'));
    await waitFor(() => expect(data.mock.calls.map((call) => levelOf(call[0]))).toContain('customer'));
    expect(await screen.findByText('2 of 2 layers drawn')).toBeInTheDocument();
    expect(services.mapService.replaceLayers).toBeDefined();
  });

  it('shows how many entities of each layer are placed, so an empty layer explains itself', async () => {
    vi.spyOn(services.mapService, 'config').mockResolvedValue({
      ...CONFIG,
      coverage: [{ entity_type: 'region', label: 'Region', total: 13, placed: 9, derived: 9, missing: 4 }],
    });
    wrap();
    const drawer = await openSettings();
    expect(within(drawer).getByText(/9 of 13 placed/)).toBeInTheDocument();
  });

  // -- a composer --------------------------------------------------------------

  it('creates a design from the editor and opens it', async () => {
    auth.composer = true;
    const created = vi.spyOn(services.mapService, 'createDesign').mockImplementation((body) => {
      const saved: MapDesign = {
        ...CUSTOM, design_id: 3, name: body.name,
        layers: body.layers.map((layer, index) => layerConfig(layer.point_level, index + 1)),
      };
      designList = [...designList, saved];
      return Promise.resolve(saved);
    });
    wrap();
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByRole('button', { name: 'New design' }));
    const editor = await screen.findByRole('dialog', { name: 'New design' });
    fireEvent.change(within(editor).getByLabelText(/Design name/), { target: { value: 'Territory Focus' } });
    // Zone and Region start ticked (Customer does not); leave Region only.
    fireEvent.click(within(editor).getByLabelText('Zone'));
    fireEvent.click(within(editor).getByRole('button', { name: 'Save design' }));

    await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
    expect(created.mock.calls[0][0]).toEqual({
      name: 'Territory Focus', description: null, basemap: 'standard', default_metric: 'net_sales',
      layers: [{ point_level: 'region', is_visible: true, cluster_at: null }],
    });
    // The page switched to the new design.
    await waitFor(() => expect(
      data.mock.calls.some((call) => (call[0] as { design_id?: number }).design_id === 3),
    ).toBe(true));
  });

  it('saves a layer edit as the whole ordered list, inherited values left inherited', async () => {
    auth.composer = true;
    const replaced = vi.spyOn(services.mapService, 'replaceLayers').mockResolvedValue(DESIGN);
    wrap();
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByLabelText('Edit layer: Region'));
    const editor = await screen.findByRole('dialog', { name: 'Edit layer' });
    expect(within(editor).getByText(/no boundary source/)).toBeInTheDocument();
    fireEvent.change(within(editor).getByLabelText('Label'), { target: { value: 'code' } });
    fireEvent.click(within(editor).getByLabelText('Show labels'));
    fireEvent.change(within(editor).getByLabelText(/Cluster above/), { target: { value: '300' } });
    fireEvent.change(within(editor).getByLabelText('Good from'), { target: { value: '95' } });
    fireEvent.change(within(editor).getByLabelText('Medium from'), { target: { value: '80' } });
    fireEvent.change(within(editor).getByLabelText('Low from'), { target: { value: '60' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Save layer' }));

    await waitFor(() => expect(replaced).toHaveBeenCalledTimes(1));
    const [designId, layers] = replaced.mock.calls[0] as [number, MapLayerInput[]];
    expect(designId).toBe(1);
    expect(layers.map((layer) => layer.point_level)).toEqual(['zone', 'region', 'customer']);
    expect(layers[1]).toMatchObject({
      label_field: 'code', show_label: true, cluster_at: 300, metric: null,
      tooltip_fields: null, style_config: { thresholds: [95, 80, 60] },
    });
    expect(layers[0]).toMatchObject({ label_field: 'name', show_label: false, cluster_at: null });
  });

  it('reorders layers with the arrows, saving the new order', async () => {
    auth.composer = true;
    const replaced = vi.spyOn(services.mapService, 'replaceLayers').mockResolvedValue(DESIGN);
    wrap();
    const drawer = await openSettings();
    const buttons = within(drawer).getAllByLabelText('Move up');
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(replaced).toHaveBeenCalledTimes(1));
    const layers = replaced.mock.calls[0][1] as MapLayerInput[];
    expect(layers.map((layer) => layer.point_level)).toEqual(['region', 'zone', 'customer']);
  });

  it('adds a layer only for a level the design does not draw yet', async () => {
    auth.composer = true;
    vi.spyOn(services.mapService, 'config').mockResolvedValue({
      ...CONFIG,
      levels: [...CONFIG.levels, {
        key: 'area', label: 'Area', code_field: 'area_code', name_field: 'area_name', table: 'dim_area',
        parent: 'region', group: 'Organisation', depth: 5, promoted: true, boundary_available: false,
        view_modes: ['point'],
      }],
    });
    const replaced = vi.spyOn(services.mapService, 'replaceLayers').mockResolvedValue(DESIGN);
    wrap();
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByRole('button', { name: 'Add layer' }));
    const editor = await screen.findByRole('dialog', { name: 'Add layer' });
    const level = within(editor).getByLabelText('Point level') as HTMLSelectElement;
    expect([...level.options].map((option) => option.value)).toEqual(['area']);
    fireEvent.click(within(editor).getByRole('button', { name: 'Save layer' }));
    await waitFor(() => expect(replaced).toHaveBeenCalledTimes(1));
    const layers = replaced.mock.calls[0][1] as MapLayerInput[];
    expect(layers.map((layer) => layer.point_level)).toEqual(['zone', 'region', 'customer', 'area']);
    expect(layers[3]).toMatchObject({ point_level: 'area', is_visible: true, cluster_at: null });
  });

  it('deletes a custom design after confirmation and falls back to the default', async () => {
    auth.composer = true;
    designList = [DESIGN, CUSTOM];
    const removed = vi.spyOn(services.mapService, 'deleteDesign').mockImplementation((designId) => {
      designList = designList.filter((candidate) => candidate.design_id !== designId);
      return Promise.resolve({ deleted_design_id: designId, name: 'Field View', layers_removed: 2, default_design_id: 1 });
    });
    wrap(`${ROUTE}&design=2`);
    await waitFor(() => expect(data.mock.calls.some((call) => (call[0] as { design_id?: number }).design_id === 2)).toBe(true));
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByRole('button', { name: 'Delete' }));
    const confirm = await screen.findByRole('dialog', { name: 'Delete map design?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(removed).toHaveBeenCalledWith(2));
    await waitFor(() => expect(
      data.mock.calls.at(-1)![0] as { design_id?: number },
    ).toMatchObject({ design_id: 1 }));
  });

  it('never offers to delete the system default, and shows a refusal as the server phrased it', async () => {
    auth.composer = true;
    vi.spyOn(services.mapService, 'replaceLayers').mockRejectedValue(
      new ApiError(409, 'Conflict', {
        detail: { error_code: 'MAP_LAYER_INVALID', message: 'Region: cluster_at must be a whole number of points, at least 1, or empty to never cluster.' },
      }),
    );
    wrap();
    const drawer = await openSettings();
    expect(within(drawer).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(within(drawer).getByText(/System default/)).toBeInTheDocument();

    fireEvent.click(within(drawer).getAllByLabelText('Move down')[0]);
    expect(await screen.findByRole('alert')).toHaveTextContent('Region: cluster_at must be a whole number');
  });
});
