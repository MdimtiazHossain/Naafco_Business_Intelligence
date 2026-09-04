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
import { useMapConfig, useMapDesigns, useMapLayers } from '../components/map/mapQueries';
import { SelectedEntityCard } from '../components/map/SelectedEntityCard';
import { MapSettingsDrawer } from '../components/map/MapSettingsDrawer';
import { TopBottomTable } from '../components/map/TopBottomTable';
import type { MapSelection } from '../components/map/useLayerRenderer';
import { PageHeader, ResultNotes } from '../components/PageHeader';
import { CardSkeleton, ErrorState } from '../components/States';
import { useAuth } from '../contexts/AuthContext';
import { MAP_FILTERS, useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { ApiError } from '../services';

/** URL parameters that are the reader's temporary state, and Reset clears. */
const TEMPORARY_PARAMS = ['layers', 'selected'] as const;

/**
 * `layers=none`: every layer switched off. An absent parameter means "the
 * design's own layers", so an empty toggle set needs its own spelling or
 * unticking the last layer would bring all of them back.
 */
const NO_LAYERS = 'none';

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
  const config = useMapConfig();
  const { hasSection } = useAuth();
  // A composer also sees designs taken off the shelf, to put them back.
  const composer = hasSection('map_settings');
  const designs = useMapDesigns(composer);
  const [settingsOpen, setSettingsOpen] = useState(false);

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

  // The reader's layer toggles, else the layers the design shows.
  const levels = useMemo(() => {
    const fromUrl = searchParams.get('layers');
    if (fromUrl === NO_LAYERS) return [];
    if (fromUrl) return fromUrl.split(',').filter(Boolean);
    return design?.layers.filter((layer) => layer.is_visible).map((layer) => layer.point_level) ?? [];
  }, [design, searchParams]);

  const metricParam = searchParams.get('metric') ?? undefined;
  const metric = metricParam ?? design?.default_metric;
  const layers = useMapLayers(design?.design_id, levels, query, metricParam);

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
    const defaults = design?.layers
      .filter((layer) => layer.is_visible)
      .map((layer) => layer.point_level) ?? [];
    const same = next.length === defaults.length && next.every((level, index) => level === defaults[index]);
    setParam('layers', same ? undefined : next.length === 0 ? NO_LAYERS : next.join(','));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [design, setSearchParams]);

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

      <GlobalFilterBar />

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

      {config.data && designs.data && (
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
