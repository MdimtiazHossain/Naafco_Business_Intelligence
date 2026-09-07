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
import { CONFIG, DESIGN, REGIONS, STYLE, layerConfig, rankingRow, response } from './mapFixtures';

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

/**
 * The other map's design, with a shape and a colour per level.
 *
 * Those two fields are why the drawer had to reach this tab at all: Area
 * Demarcation draws no figure, so a point's shape and colour are the whole of
 * how a reader tells a territory from a customer — and until the drawer was
 * mounted here they were editable only for the map that does not need them.
 */
const DEMARCATION: MapDesign = {
  ...DESIGN,
  design_id: 9,
  name: 'Area Demarcation',
  purpose: 'demarcation',
  is_system_default: true,
  layers: [
    layerConfig('region', 1, {
      style_config: { shape: 'circle', point_color: '#2563eb' },
      style: { ...STYLE, shape: 'circle', point_color: '#2563eb' },
    }),
    layerConfig('customer', 2, {
      style_config: { shape: 'square', point_color: '#db2777' },
      style: { ...STYLE, shape: 'square', point_color: '#db2777' },
    }),
  ],
};

/** Enough of a coordinates response for the demarcation tab to render. */
const LOCATIONS = {
  design: DEMARCATION,
  levels: ['region', 'customer'],
  filters: {},
  scope_note: null,
  color_by: null,
  empty: false,
  layers: [] as [],
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
    // Answered by purpose, because the drawer is now on both tabs and the two
    // lists are different maps' designs. A mock that ignored it would let a
    // demarcation test pass against the analysis design.
    vi.spyOn(services.mapService, 'designs').mockImplementation((_inactive, purpose) =>
      Promise.resolve(purpose === 'demarcation'
        ? { purpose: 'demarcation' as const, designs: [DEMARCATION], default_design_id: 9 }
        : { purpose: 'analysis' as const, designs: designList, default_design_id: 1 }));
    vi.spyOn(services.mapService, 'locations').mockResolvedValue(LOCATIONS);
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
      // Stated rather than left to the server's default. The value is the same
      // one the server would have chosen here; it is the demarcation tab that
      // needs it said out loud, and one code path says it for both.
      purpose: 'analysis',
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

  it('carries the shape and the point colour, which nothing else did', async () => {
    // The two controls this change exists for, and the two the suite had never
    // pinned: everything else in the editor is covered above.
    auth.composer = true;
    const replaced = vi.spyOn(services.mapService, 'replaceLayers').mockResolvedValue(DESIGN);
    wrap();
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByLabelText('Edit layer: Region'));
    const editor = await screen.findByRole('dialog', { name: 'Edit layer' });

    // The control shows what the map *draws*, so an unset field reads as the
    // catalogue default rather than as a blank.
    const shape = within(editor).getByLabelText('Shape') as HTMLSelectElement;
    expect(shape.value).toBe(STYLE.shape);
    expect((within(editor).getByLabelText('Point colour') as HTMLInputElement).value)
      .toBe(STYLE.point_color);

    fireEvent.change(shape, { target: { value: 'triangle' } });
    fireEvent.change(within(editor).getByLabelText('Point colour'),
                     { target: { value: '#ff8800' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Save layer' }));

    await waitFor(() => expect(replaced).toHaveBeenCalledTimes(1));
    const layers = replaced.mock.calls[0][1] as MapLayerInput[];
    expect(layers[1].style_config).toMatchObject({ shape: 'triangle', point_color: '#ff8800' });
    // Only what differs from the declared default is stored, so a layer that
    // never chose a shape keeps inheriting one instead of pinning today's.
    expect(layers[0].style_config).toBeNull();
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

/**
 * The drawer on the other tab.
 *
 * It used to be mounted inside the analysis branch of the page, so Settings --
 * drawn in the header for both tabs -- did nothing at all here, and left
 * `settingsOpen` true so the drawer sprang open by itself on the way back.
 * Every test below fails against that arrangement.
 */
describe('Map settings drawer on the Area Demarcation tab', () => {
  const DEMARCATION_ROUTE = `${ROUTE}&tab=demarcation`;

  beforeEach(() => {
    // A sibling describe, so the suite above's `beforeEach` does not run here.
    auth.composer = true;
    vi.spyOn(services.mapService, 'config').mockResolvedValue(CONFIG);
    // Answered by purpose: the two tabs are different maps' designs, and a mock
    // that ignored it would let these tests pass against the analysis design.
    vi.spyOn(services.mapService, 'designs').mockImplementation((_inactive, purpose) =>
      Promise.resolve(purpose === 'demarcation'
        ? { purpose: 'demarcation' as const, designs: [DEMARCATION], default_design_id: 9 }
        : { purpose: 'analysis' as const, designs: [DESIGN], default_design_id: 1 }));
    vi.spyOn(services.mapService, 'locations').mockResolvedValue(LOCATIONS);
    spyOnData();
  });

  it('opens, and shows this map own design rather than the other one', async () => {
    wrap(DEMARCATION_ROUTE);
    const drawer = await openSettings();
    // Matched loosely because an option reads "Name . Default".
    expect(within(drawer).getByRole('option', { name: /Area Demarcation/ }))
      .toBeInTheDocument();
    // The assertion that bites: the analysis design must not be reachable from
    // a drawer opened over the map that cannot draw it.
    expect(within(drawer).queryByRole('option', { name: /Business Overview/ }))
      .not.toBeInTheDocument();
    expect(within(drawer).getAllByLabelText(/^Edit layer: /)
      .map((button) => button.getAttribute('aria-label')))
      .toEqual(['Edit layer: Region', 'Edit layer: Customer']);
  });

  it('leaves out the Active layer picker, which this map has nothing to rank', async () => {
    wrap(DEMARCATION_ROUTE);
    const drawer = await openSettings();
    // Absent rather than inert: that picker names the layer the legend explains
    // and the Top / Bottom tables rank, and this map has no ranking and a
    // legend that names every level at once.
    expect(within(drawer).queryByLabelText('Active layer')).not.toBeInTheDocument();
  });

  it('edits a demarcation layer shape and colour, the controls this map lives by', async () => {
    const replaced = vi.spyOn(services.mapService, 'replaceLayers')
      .mockResolvedValue(DEMARCATION);
    wrap(DEMARCATION_ROUTE);
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByLabelText('Edit layer: Customer'));
    const editor = await screen.findByRole('dialog', { name: 'Edit layer' });
    expect((within(editor).getByLabelText('Shape') as HTMLSelectElement).value).toBe('square');
    expect((within(editor).getByLabelText('Point colour') as HTMLInputElement).value)
      .toBe('#db2777');

    fireEvent.change(within(editor).getByLabelText('Shape'), { target: { value: 'triangle' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Save layer' }));

    await waitFor(() => expect(replaced).toHaveBeenCalledTimes(1));
    const [designId, layers] = replaced.mock.calls[0] as [number, MapLayerInput[]];
    expect(designId).toBe(9);
    expect(layers[1].style_config).toMatchObject({ shape: 'triangle', point_color: '#db2777' });
    // The region layer keeps its own, untouched.
    expect(layers[0].style_config).toMatchObject({ shape: 'circle', point_color: '#2563eb' });
  });

  it('creates a design for this map, not the other, and starts it with every level', async () => {
    // A level the analysis map does not promote, so "every level" and "the
    // promoted levels" are different answers and the assertion means something.
    vi.spyOn(services.mapService, 'config').mockResolvedValue({
      ...CONFIG,
      levels: [...CONFIG.levels, {
        key: 'sales_force', label: 'Sales Force', code_field: 'sales_force_code',
        name_field: 'sales_force_name', table: 'dim_sales_force', parent: 'territory',
        group: 'Business', depth: 9, promoted: false, boundary_available: false,
        view_modes: ['point'],
      }],
    });
    const created = vi.spyOn(services.mapService, 'createDesign')
      .mockResolvedValue(DEMARCATION);
    wrap(DEMARCATION_ROUTE);
    const drawer = await openSettings();
    fireEvent.click(within(drawer).getByRole('button', { name: 'New design' }));
    const editor = await screen.findByRole('dialog', { name: 'New design' });
    fireEvent.change(within(editor).getByLabelText(/Design name/),
                     { target: { value: 'Field lines' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Save design' }));

    await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
    const body = created.mock.calls[0][0];
    // Without this the server defaults to `analysis`, and the design the
    // composer just made vanishes from the list they made it in.
    expect(body.purpose).toBe('demarcation');
    // Every level, because this map accounts for every stored coordinate:
    // `drawn + derived == stored`. A design omitting a level breaks that
    // partition for whoever opens it, which is what revision 0036 repaired.
    expect(body.layers.map((layer) => layer.point_level))
      .toEqual(['zone', 'region', 'customer', 'sales_force']);
  });
});
