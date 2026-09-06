/**
 * The Business Map: one map, every level of the hierarchy as a layer.
 *
 * The page composes what the backend declares. The design comes from the
 * database (the default unless the URL names another), its visible layers
 * decide what is fetched, and the reader's temporary choices — which layers
 * are on, which metric headlines, which entity is selected — live in the URL
 * so a filtered map is a link and Reset is one navigation. No figure is
 * computed here: every number on the map arrived from `GET /api/map/data`,
 * through the same tool the Performance page reads.
 */

import { Loader2, Lock, RefreshCw, RotateCcw, Settings } from 'lucide-react';
import type { Map as MapLibreInstance } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { BusinessMap } from '../components/map/BusinessMap';
import { BoundaryCard, BoundaryControl } from '../components/map/BoundaryControl';
import { DemarcationTab } from '../components/map/DemarcationTab';
import { useMapConfig, useMapDesigns, useMapLayers } from '../components/map/mapQueries';
import { SelectedEntityCard } from '../components/map/SelectedEntityCard';
import { MapSettingsDrawer } from '../components/map/MapSettingsDrawer';
import { TopBottomTable } from '../components/map/TopBottomTable';
import type {
  BoundaryLayerState, BoundarySelection,
} from '../components/map/useBoundaryLayer';
import type { MapSelection } from '../components/map/useLayerRenderer';
import { PageHeader, ResultNotes } from '../components/PageHeader';
import { CardSkeleton, ErrorState } from '../components/States';
import { useAuth } from '../contexts/AuthContext';
import { LOCATION_FILTERS, MAP_FILTERS, useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { ApiError } from '../services';

/** URL parameters that are the reader's temporary state, and Reset clears. */
const TEMPORARY_PARAMS = ['layers', 'selected', 'boundary', 'barea', 'bname'] as const;

/**
 * The two maps, in the order they are read.
 *
 * Business Map first because it is what somebody arrives for. Area Demarcation
 * is the same coordinates with every figure taken off — a different question
 * about the same points, which is why it is a tab here rather than a page of
 * its own: the reader who wants to know where a boundary falls has usually
 * just been looking at what is inside it.
 */
const TABS = ['map', 'demarcation'] as const;
type Tab = (typeof TABS)[number];

/**
 * `layers=none`: every layer switched off. An absent parameter means "the
 * design's own layers", so an empty toggle set needs its own spelling or
 * unticking the last layer would bring all of them back.
 */
const NO_LAYERS = 'none';

/**
 * `boundary=none`: no backdrop, said out loud.
 *
 * The same rule as `NO_LAYERS`, and it became necessary for the same reason.
 * An absent parameter means "the surface's own default", and Area Demarcation's
 * default is now upazilas — so without a spelling for "off", a reader who
 * switched the backdrop off would get 1.7 MB of it back on the next reload,
 * with the control they used apparently doing nothing.
 */
const NO_BOUNDARY = 'none';

function parseSelection(value: string | null): MapSelection | null {
  if (!value) return null;
  const separator = value.indexOf(':');
  if (separator <= 0) return null;
  return { level: value.slice(0, separator), code: value.slice(separator + 1), source: 'url' };
}

export default function BusinessMapPage() {
  const t = useT();
  const { queryFor } = useFilters();
  const [searchParams, setSearchParams] = useSearchParams();
  const mapRef = useRef<MapLibreInstance | null>(null);

  const query = queryFor(MAP_FILTERS);
  /**
   * The demarcation map's own filter set — the same URL state, a different
   * question of it.
   *
   * `queryFor` sends only the levels named, so a material or a batch left in
   * the URL by the analysis tab does not travel with a coordinate request that
   * could not honour it. The narrowing itself is the server's: `/api/map/
   * locations` resolves the selection to a subtree and filters there, so this
   * is a filter *set*, never a filter *implementation*.
   *
   * **No period**, which is the second argument and not a detail. A coordinate
   * has no date, so the endpoint ignores a range — and sending one anyway would
   * put it in the React Query key, so changing the period on the dashboard and
   * coming back here would refetch every coordinate to be told the same thing.
   * Material Stock leaves it out for exactly this reason.
   */
  const locationQuery = queryFor(LOCATION_FILTERS, false);
  const config = useMapConfig();
  const { hasSection } = useAuth();
  // A composer also sees designs taken off the shelf, to put them back.
  const composer = hasSection('map_settings');
  const designs = useMapDesigns(composer);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const tab: Tab = (TABS as readonly string[]).includes(searchParams.get('tab') ?? '')
    ? (searchParams.get('tab') as Tab)
    : 'map';
  // Fetched only for the tab that draws them: the two design lists are
  // different maps' designs and neither tab may offer the other's.
  const demarcationDesigns = useMapDesigns(composer, 'demarcation');

  // The design: the URL's, else the one the page opens with.
  const requestedDesign = Number(searchParams.get('design'));
  const design = useMemo(() => {
    const list = designs.data?.designs ?? [];
    return (
      list.find((candidate) => candidate.design_id === requestedDesign)
      ?? list.find((candidate) => candidate.design_id === designs.data?.default_design_id)
      ?? list[0]
    );
  }, [designs.data, requestedDesign]);

  const demarcationDesign = useMemo(() => {
    const list = demarcationDesigns.data?.designs ?? [];
    return (
      list.find((candidate) => candidate.design_id === requestedDesign)
      ?? list.find((c) => c.design_id === demarcationDesigns.data?.default_design_id)
      ?? list[0]
    );
  }, [demarcationDesigns.data, requestedDesign]);

  /** The design the open tab draws with; `design` alone would be the wrong one. */
  const activeDesign = tab === 'map' ? design : demarcationDesign;

  // The reader's layer toggles, else the layers the open tab's design shows.
  const levels = useMemo(() => {
    const fromUrl = searchParams.get('layers');
    if (fromUrl === NO_LAYERS) return [];
    if (fromUrl) return fromUrl.split(',').filter(Boolean);
    return activeDesign?.layers.filter((layer) => layer.is_visible)
      .map((layer) => layer.point_level) ?? [];
  }, [activeDesign, searchParams]);

  // The backdrop the reader chose, and the outline they clicked. Both live in
  // the URL like the layer toggles and the point selection, so a link
  // reproduces the whole view and Reset is still one navigation.
  // The URL wins; an absent parameter falls back to the open tab's own default,
  // which the server publishes per purpose — none for the analysis map, upazila
  // outlines for demarcation. Reset clears the parameter, so it returns a
  // reader to their surface's default rather than to no backdrop at all.
  const boundary = useMemo(() => {
    const requested = searchParams.get('boundary');
    if (requested === NO_BOUNDARY) return null;
    const key = requested
      ?? config.data?.boundaries.defaults[tab === 'map' ? 'analysis' : 'demarcation']
      ?? null;
    return config.data?.boundaries.sets.find((set) => set.key === key) ?? null;
  }, [config.data, searchParams, tab]);

  const boundarySelected: BoundarySelection | null = useMemo(() => {
    const code = searchParams.get('barea');
    if (!code || !boundary) return null;
    const name = searchParams.get('bname') ?? code;
    return { set: boundary.key, code, name };
  }, [boundary, searchParams]);

  const selectBoundary = useCallback((next: BoundarySelection | null) => {
    setSearchParams((previous) => {
      const params = new URLSearchParams(previous);
      if (next) {
        params.set('barea', next.code);
        params.set('bname', next.name);
      } else {
        params.delete('barea');
        params.delete('bname');
      }
      return params;
    }, { replace: true });
  }, [setSearchParams]);

  /**
   * Switch the backdrop, spelling "off" rather than leaving the parameter out.
   *
   * Written once and used by both tabs: dropping the parameter would hand the
   * reader back their surface's default, which on Area Demarcation is the very
   * thing they just turned off.
   */
  const changeBoundary = useCallback((key: string | null) => {
    selectBoundary(null);
    setParam('boundary', key ?? NO_BOUNDARY);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectBoundary, setSearchParams]);

  /**
   * Whether the backdrop is in flight, reported up from whichever map drew it.
   *
   * The hook that fetches it runs inside the map component and the control that
   * explains the wait sits above it, so the state has to be lifted — the same
   * arrangement `onMap` already uses. Held here rather than in each tab because
   * both tabs render the same control.
   */
  const [boundaryState, setBoundaryState] = useState<BoundaryLayerState>(
    { loading: false, error: null },
  );

  const metricParam = searchParams.get('metric') ?? undefined;
  const metric = metricParam ?? design?.default_metric;
  // Gated on the tab: the demarcation map reads no fact table, and leaving
  // this running behind it would aggregate five layers nobody is looking at.
  const layers = useMapLayers(design?.design_id, tab === 'map' ? levels : [],
                              query, metricParam);

  const basemap = useMemo(() => {
    if (!config.data) return undefined;
    const resolved = design?.basemap_resolved;
    return (
      config.data.basemaps.find((candidate) => candidate.key === resolved?.key)
      ?? config.data.basemaps.find((candidate) => candidate.key === config.data?.default_basemap)
      ?? config.data.basemaps[0]
    );
  }, [config.data, design]);

  // The selection travels in the URL; a click on the map carries its figures
  // along so the card can show them before the layer is looked up again.
  const [selectionDetail, setSelectionDetail] = useState<MapSelection | null>(null);
  const selected = useMemo(() => {
    const fromUrl = parseSelection(searchParams.get('selected'));
    if (!fromUrl) return null;
    if (selectionDetail && selectionDetail.level === fromUrl.level && selectionDetail.code === fromUrl.code) {
      return selectionDetail;
    }
    return fromUrl;
  }, [searchParams, selectionDetail]);

  // The layer the legend and the ranking describe: the reader's pick while it
  // is drawn, else the top-most drawn layer.
  const [activeChoice, setActiveChoice] = useState<string | null>(null);
  const activeLevel = useMemo(() => {
    const drawn = layers.layers.map((layer) => layer.level);
    if (activeChoice && drawn.includes(activeChoice)) return activeChoice;
    if (selected && drawn.includes(selected.level)) return selected.level;
    return drawn[drawn.length - 1] ?? '';
  }, [activeChoice, layers.layers, selected]);
  const activeLayer = layers.layers.find((layer) => layer.level === activeLevel);

  function setParam(name: string, value: string | undefined) {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: true },
    );
  }

  const select = useCallback((selection: MapSelection | null) => {
    setSelectionDetail(selection);
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (selection) next.set('selected', `${selection.level}:${selection.code}`);
        else next.delete('selected');
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  /** Back to the first frame: the default view, no toggles, nothing selected. */
  function reset() {
    setSelectionDetail(null);
    setActiveChoice(null);
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        TEMPORARY_PARAMS.forEach((name) => next.delete(name));
        return next;
      },
      { replace: true },
    );
    const view = config.data?.view;
    if (view && mapRef.current) {
      mapRef.current.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom });
    }
  }

  const onMap = useCallback((map: MapLibreInstance | null) => {
    mapRef.current = map;
  }, []);

  /** Switch design: the toggles and the selection belonged to the old one. */
  const changeDesign = useCallback((designId: number | null) => {
    setSelectionDetail(null);
    setActiveChoice(null);
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (designId) next.set('design', String(designId));
        else next.delete('design');
        TEMPORARY_PARAMS.forEach((name) => next.delete(name));
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  /** A reader's layer toggles: in the URL while they differ from the design. */
  const changeLevels = useCallback((next: string[]) => {
    const defaults = activeDesign?.layers
      .filter((layer) => layer.is_visible)
      .map((layer) => layer.point_level) ?? [];
    const same = next.length === defaults.length && next.every((level, index) => level === defaults[index]);
    setParam('layers', same ? undefined : next.length === 0 ? NO_LAYERS : next.join(','));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDesign, setSearchParams]);

  /**
   * Switch tab, dropping the toggles, the selection and the design.
   *
   * All three belonged to the map being left: the two tabs draw different
   * designs with different layer sets, so carrying a `layers=zone,region` from
   * one to the other would silently ask the new map for levels its design may
   * not have.
   */
  const changeTab = useCallback((next: Tab) => {
    setSelectionDetail(null);
    setActiveChoice(null);
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous);
        if (next === 'map') params.delete('tab');
        else params.set('tab', next);
        TEMPORARY_PARAMS.forEach((name) => params.delete(name));
        params.delete('design');
        return params;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  // A new data set — a different period, filter set or design — is fitted
  // once; a layer toggle or a selection never moves the map by itself.
  const fitKey = useMemo(
    () => JSON.stringify({ query, design: design?.design_id ?? null }),
    [query, design],
  );

  // A selection that is no longer among the drawn layers' entities still
  // shows (from its own properties); one whose layer is switched off stays in
  // the URL so switching it back on restores it.
  useEffect(() => {
    if (selected && selectionDetail && selectionDetail.code !== selected.code) {
      setSelectionDetail(null);
    }
  }, [selected, selectionDetail]);

  const notes = useMemo(() => {
    const collected: string[] = [];
    if (design?.basemap_note) collected.push(design.basemap_note);
    layers.layers.forEach((layer) => collected.push(...layer.notes));
    return [...new Set(collected)];
  }, [design, layers.layers]);

  const metrics = config.data?.metrics ?? [];
  const period = layers.first?.period;
  const failed = layers.error instanceof ApiError ? layers.error : null;

  return (
    <>
      <PageHeader
        title={t('map.title')}
        description={t('map.subtitle')}
        period={period}
        actions={
          <>
            <button type="button" className="btn-secondary" onClick={reset}>
              <RotateCcw size={14} />
              {t('map.reset')}
            </button>
            <button type="button" className="btn-secondary" onClick={() => setSettingsOpen(true)}>
              <Settings size={14} />
              {t('map.settings')}
            </button>
          </>
        }
      />

      <div className="tab-strip mb-4 gap-1 border-b border-slate-200 dark:border-slate-700">
        {TABS.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => changeTab(key)}
            className={`shrink-0 whitespace-nowrap px-3 py-2.5 text-sm font-medium ${
              tab === key
                ? 'border-b-2 border-brand-600 text-brand-700 dark:text-brand-300'
                : 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
            aria-current={tab === key ? 'page' : undefined}
          >
            {t(`map.tab.${key}`)}
          </button>
        ))}
      </div>

      {/*
        One bar, two filter sets — because the two tabs narrow different things.

        The analysis map reads `vw_sales_detail`, so it draws every filter that
        view honours, over a period. The demarcation map reads
        `map_entity_locations`, where a row is a code and a pair of coordinates:
        it has no material, no batch and no date, so those controls are absent
        rather than inert, and there is no period selector at all.

        Passing the set per tab is also what makes the chips and "clear all"
        follow the open tab: the bar describes exactly the levels it was handed,
        so a material left in the URL by the analysis tab shows no chip over a
        map that would ignore it.
      */}
      {tab === 'map' ? (
        <GlobalFilterBar />
      ) : (
        <GlobalFilterBar
          levels={[]}
          showIndependent={false}
          pageFilters={LOCATION_FILTERS}
          showDate={false}
        />
      )}

      {(config.isLoading || designs.isLoading) && <CardSkeleton rows={3} />}
      {(config.error || designs.error) && (
        <div className="card">
          <ErrorState
            error={config.error ?? designs.error}
            onRetry={() => {
              void config.refetch();
              void designs.refetch();
            }}
          />
        </div>
      )}

      {config.data && tab === 'demarcation' && (
        <DemarcationTab
          config={config.data}
          design={demarcationDesign}
          designs={demarcationDesigns.data?.designs ?? []}
          onDesignChange={changeDesign}
          levels={levels}
          onLevelsChange={changeLevels}
          query={locationQuery}
          // Narrowed to the identity both maps share. The analysis selection
          // carries a row of measures the demarcation map has no use for, and
          // widening either type so they interchange would say the two are the
          // same thing when they are not.
          selected={selected && { level: selected.level, code: selected.code,
                                  source: selected.source }}
          onSelect={(next) => select(next && { level: next.level, code: next.code,
                                               source: next.source })}
          boundary={boundary}
          boundarySelected={boundarySelected}
          onBoundaryChange={changeBoundary}
          onBoundarySelect={selectBoundary}
          onBoundaryState={setBoundaryState}
          boundaryState={boundaryState}
          onMap={onMap}
        />
      )}

      {config.data && designs.data && tab === 'map' && (
        <>
          <div className="card mb-4 flex flex-wrap items-center gap-4 p-3">
            <div className="flex items-center gap-2">
              <label className="label mb-0 whitespace-nowrap" htmlFor="map-metric">
                {t('map.metric')}
              </label>
              <select
                id="map-metric"
                className="input w-auto min-w-[10rem] py-1.5"
                value={metric ?? ''}
                onChange={(event) => setParam('metric', event.target.value || undefined)}
              >
                {metrics.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>

            {designs.data.designs.filter((option) => option.is_active).length > 1 && (
              <div className="flex items-center gap-2">
                <label className="label mb-0 whitespace-nowrap" htmlFor="map-design">
                  {t('map.design')}
                </label>
                <select
                  id="map-design"
                  className="input w-auto min-w-[10rem] py-1.5"
                  value={design?.design_id ?? ''}
                  onChange={(event) => changeDesign(Number(event.target.value) || null)}
                >
                  {designs.data.designs
                    .filter((option) => option.is_active || option.design_id === design?.design_id)
                    .map((option) => (
                      <option key={option.design_id} value={option.design_id}>
                        {option.name}
                      </option>
                    ))}
                </select>
              </div>
            )}

            <BoundaryControl
              catalogue={config.data.boundaries}
              value={boundary?.key ?? null}
              loading={boundaryState.loading}
              error={boundaryState.error}
              onChange={changeBoundary}
            />

            {levels.length > 0 && (
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {t('map.layersDrawn', {
                  drawn: String(layers.layers.length),
                  total: String(levels.length),
                })}
              </span>
            )}
          </div>

          {!design && (
            <div className="card p-8 text-center text-sm text-slate-500">{t('map.noDesign')}</div>
          )}

          {design && basemap && (
            <div className="card mb-4 h-[60vh] min-h-[420px] overflow-hidden sm:h-[68vh]">
              <BusinessMap
                basemap={basemap}
                view={config.data.view}
                layers={layers.layers}
                metrics={metrics}
                selected={selected}
                onSelect={select}
                activeLevel={activeLevel}
                onActiveLevel={setActiveChoice}
                fitKey={fitKey}
                boundary={boundary}
                maskUrl={config.data.boundaries.mask_url}
                boundarySelected={boundarySelected}
                onBoundarySelect={selectBoundary}
                onBoundaryState={setBoundaryState}
                style={config.data.style}
                onMap={onMap}
              >
                {layers.isLoading && (
                  <div
                    className="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-lg bg-white/90 px-3 py-1.5 text-xs font-medium text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                    role="status"
                  >
                    <Loader2 size={14} className="animate-spin" />
                    {t('map.loading')}
                  </div>
                )}
                {layers.error != null && (
                  <div className="absolute inset-x-3 top-3 z-10 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-red-200 bg-white/95 px-3 py-2 text-sm shadow dark:border-red-900 dark:bg-slate-900/95">
                    <span className="flex items-center gap-2 text-slate-700 dark:text-slate-200">
                      {failed?.isForbidden ? <Lock size={14} className="text-amber-500" /> : null}
                      {failed?.isForbidden ? failed.message : t('map.loadError')}
                    </span>
                    {!failed?.isForbidden && (
                      <button type="button" className="btn-secondary py-1" onClick={layers.refetch}>
                        <RefreshCw size={14} />
                        {t('common.retry')}
                      </button>
                    )}
                  </div>
                )}
                {!layers.isLoading && layers.error == null && layers.isEmpty && (
                  <div
                    className="absolute right-3 top-3 z-10 rounded-lg bg-white/90 px-3 py-1.5 text-xs text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                    role="status"
                  >
                    {t('map.empty')}
                  </div>
                )}
              </BusinessMap>
            </div>
          )}

          <ResultNotes notes={notes} />

          {design && (
            <>
              <BoundaryCard
                selection={boundarySelected}
                set={boundary}
                onClear={() => selectBoundary(null)}
              />
              <SelectedEntityCard
                selection={selected}
                layers={layers.layers}
                metrics={metrics}
                onClear={() => select(null)}
              />
              <TopBottomTable layer={activeLayer} metrics={metrics} onSelect={select} />
            </>
          )}

          <MapSettingsDrawer
            open={settingsOpen}
            onClose={() => setSettingsOpen(false)}
            config={config.data}
            designs={designs.data.designs}
            design={design}
            onDesignChange={changeDesign}
            levels={levels}
            onLevelsChange={changeLevels}
            drawn={layers.layers}
            activeLevel={activeLevel}
            onActiveLevel={setActiveChoice}
          />
        </>
      )}
    </>
  );
}
