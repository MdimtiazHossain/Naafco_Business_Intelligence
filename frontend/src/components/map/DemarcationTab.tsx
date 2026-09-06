/**
 * The Area Demarcation tab: every placed coordinate, and nothing measured.
 *
 * A separate component from the analysis tab rather than a mode of it, because
 * the two share almost nothing above the map instance: no period, no metric, no
 * ranking, a different filter set, and a legend that explains shapes rather than
 * colours. What they do share — the reader's state living in the URL, Reset
 * being one navigation, a control absent rather than disabled — is convention,
 * not code.
 *
 * **What it draws is exactly the rows of `map_entity_locations`.** Every
 * coordinate at every level, placed and derived alike, and no fact table is
 * read here at all: for the same filter selection the number of points on this
 * map equals the number of rows Data Management > Map Locations lists. That is
 * the acid test, and it is why the level toggles come from the design rather
 * than from a visibility default that could quietly omit a level.
 *
 * **Read-only for coordinates, on purpose.** Placing, moving and removing a
 * point already have two ways in (Data Management and the Upload Centre's Map
 * Locations file), and both route their writes through `app.map`'s own module
 * so the derivation, the coordinate rules and the DERIVED-removal refusal live
 * in one place each. A third write path here would have to reimplement all
 * three. What this tab offers instead is the way *out*: a selected point links
 * to its Data Management record, so noticing that something sits in the wrong
 * place has somewhere to go.
 */

import { ExternalLink, Loader2, RefreshCw, X } from 'lucide-react';
import type { Map as MapLibreInstance } from 'maplibre-gl';
import { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useT } from '../../contexts/I18nContext';
import type {
  GlobalFilters, MapBoundarySet, MapConfig, MapDesign, MapLocationLayer,
} from '../../types/api';
import { ErrorState } from '../States';
import { ResultNotes } from '../PageHeader';
import { BoundaryCard, BoundaryControl } from './BoundaryControl';
import { DemarcationMap } from './DemarcationMap';
import { ShapeSwatch } from './DemarcationLegend';
import { useMapLocations } from './mapQueries';
import type { BoundaryLayerState, BoundarySelection } from './useBoundaryLayer';
import type { ShapeSelection } from './useShapeRenderer';

export interface DemarcationTabProps {
  config: MapConfig;
  design: MapDesign | undefined;
  designs: MapDesign[];
  onDesignChange: (designId: number | null) => void;
  levels: string[];
  onLevelsChange: (levels: string[]) => void;
  /**
   * The reader's filter selection, as `LOCATION_FILTERS` names it.
   *
   * Sent to the server and applied there. **A map narrows by containment where
   * a table narrows by row**: selecting Region R01 shows R01's own coordinate
   * and everything below it — its areas, units, territories, sub-territories
   * and customers — because a coordinate is a position inside an area rather
   * than a fact about one level. Applying the report filters' row semantics
   * here would draw the region's single centroid and call the other 200 points
   * excluded.
   */
  query: GlobalFilters;
  selected: ShapeSelection | null;
  onSelect: (selection: ShapeSelection | null) => void;
  /** The administrative backdrop, chosen by the reader and held in the URL. */
  boundary: MapBoundarySet | null;
  boundarySelected: BoundarySelection | null;
  onBoundaryChange: (key: string | null) => void;
  onBoundarySelect: (selection: BoundarySelection | null) => void;
  /**
   * The backdrop's loading state, lifted from the map and handed to the picker.
   *
   * It matters most on this tab: Area Demarcation opens with upazila outlines,
   * 1.7 MB of them, so the longest wait on either map happens on first paint
   * before anybody has touched the control.
   */
  onBoundaryState: (state: BoundaryLayerState) => void;
  /** What that state currently is, so the picker can explain the wait. */
  boundaryState: BoundaryLayerState;
  onMap?: (map: MapLibreInstance | null) => void;
}

export function DemarcationTab({
  config, design, designs, onDesignChange, levels, onLevelsChange,
  query, selected, onSelect, boundary, boundarySelected, onBoundaryChange,
  onBoundarySelect, onBoundaryState, boundaryState, onMap,
}: DemarcationTabProps) {
  const t = useT();
  const [activeChoice, setActiveChoice] = useState<string | null>(null);
  const locations = useMapLocations(design?.design_id, levels, query);
  const layers: MapLocationLayer[] = locations.data?.layers ?? [];

  const basemap = useMemo(() => (
    config.basemaps.find((candidate) => candidate.key === design?.basemap_resolved?.key)
    ?? config.basemaps.find((candidate) => candidate.key === config.default_basemap)
    ?? config.basemaps[0]
  ), [config, design]);

  const activeLevel = useMemo(() => {
    const drawn = layers.map((layer) => layer.level);
    if (activeChoice && drawn.includes(activeChoice)) return activeChoice;
    if (selected && drawn.includes(selected.level)) return selected.level;
    return drawn[drawn.length - 1] ?? '';
  }, [activeChoice, layers, selected]);

  // A new design, level set or filter selection is fitted once; a selection
  // never moves the map by itself. The filters are in the key because narrowing
  // to one region is a different set of points, and leaving the view over the
  // whole country would show the reader an apparently empty map.
  const fitKey = useMemo(
    () => JSON.stringify({
      design: design?.design_id ?? null, levels: [...levels].sort(), query,
    }),
    [design, levels, query],
  );

  const toggle = useCallback((level: string) => {
    onLevelsChange(levels.includes(level)
      ? levels.filter((candidate) => candidate !== level)
      : [...levels, level]);
  }, [levels, onLevelsChange]);

  /**
   * What the map could not draw, and why — the layers' own notes plus the one
   * the server writes about the caller's scope.
   *
   * The scope note comes first: "you have no data scope" explains every empty
   * layer below it, and reading eleven per-level notes before the sentence that
   * accounts for all of them is the wrong order.
   */
  const notes = useMemo(() => [...new Set([
    ...(locations.data?.scope_note ? [locations.data.scope_note] : []),
    ...layers.flatMap((layer) => layer.notes),
  ])], [layers, locations.data]);

  const selectedLayer = layers.find((layer) => layer.level === selected?.level);
  const selectedPoint = selectedLayer?.features.features.find(
    (feature) => feature.properties.code === selected?.code,
  );
  /**
   * What is drawn, and what there was to draw.
   *
   * Two numbers rather than one, because "9 points" cannot be told apart from
   * "9 points and there are 94" — a narrow filter and a barely-mapped level
   * look identical otherwise, and on a map used to judge where a boundary falls
   * they are opposite findings. `available` is what exists at these levels
   * inside the caller's scope; `placed` is what survived their filter.
   *
   * The pair is shown only while a filter is in effect. The server echoes the
   * narrowing it applied, so this reads the response rather than the URL: a
   * filter the request did not carry must not put a count on screen claiming it
   * did.
   */
  const totalPlaced = layers.reduce((sum, layer) => sum + layer.placed, 0);
  const totalAvailable = layers.reduce((sum, layer) => sum + layer.available, 0);
  const narrowed = Object.keys(locations.data?.filters ?? {}).length > 0;

  // Which Data Management table a level's records live in, read from the level
  // registry the server publishes rather than written down here. A hand-kept
  // level -> table map is exactly the list that outlives what it names.
  const masterTable = config.levels.find(
    (level) => level.key === selected?.level,
  )?.table;

  if (!design) {
    return <div className="card p-8 text-center text-sm text-slate-500">{t('map.noDesign')}</div>;
  }

  return (
    <>
      <div className="card mb-4 flex flex-wrap items-center gap-4 p-3">
        {designs.filter((option) => option.is_active).length > 1 && (
          <div className="flex items-center gap-2">
            <label className="label mb-0 whitespace-nowrap" htmlFor="demarcation-design">
              {t('map.design')}
            </label>
            <select
              id="demarcation-design"
              className="input w-auto min-w-[10rem] py-1.5"
              value={design.design_id}
              onChange={(event) => onDesignChange(Number(event.target.value) || null)}
            >
              {designs
                .filter((option) => option.is_active || option.design_id === design.design_id)
                .map((option) => (
                  <option key={option.design_id} value={option.design_id}>{option.name}</option>
                ))}
            </select>
          </div>
        )}

        {/* Layer toggles, drawn from the design rather than from a list here:
            a level added to a design appears without this file changing. */}
        <div className="flex flex-wrap items-center gap-1.5">
          {design.layers.map((layer) => {
            const on = levels.includes(layer.point_level);
            const shape = config.shapes.find((s) => s.key === layer.style.shape);
            return (
              <button
                key={layer.point_level}
                type="button"
                onClick={() => toggle(layer.point_level)}
                aria-pressed={on}
                className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                  on
                    ? 'border-brand-300 bg-brand-50 text-brand-800 dark:border-brand-700 dark:bg-brand-950 dark:text-brand-200'
                    : 'border-slate-200 text-slate-500 dark:border-slate-700 dark:text-slate-400'
                }`}
              >
                <ShapeSwatch
                  shape={shape}
                  color={on ? layer.style.point_color : '#94a3b8'}
                  size={12}
                />
                {layer.level_label}
              </button>
            );
          })}
        </div>

        <BoundaryControl
          id="demarcation-boundary"
          catalogue={config.boundaries}
          value={boundary?.key ?? null}
          loading={boundaryState.loading}
          error={boundaryState.error}
          onChange={onBoundaryChange}
        />

        {layers.length > 0 && (
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {narrowed
              ? t('map.pointsMatched', { count: String(totalPlaced),
                                         total: String(totalAvailable) })
              : t('map.pointsDrawn', { count: String(totalPlaced) })}
          </span>
        )}
      </div>

      {locations.error != null && (
        <div className="card mb-4">
          <ErrorState error={locations.error} onRetry={() => void locations.refetch()} />
        </div>
      )}

      {basemap && (
        <div className="card mb-4 h-[60vh] min-h-[420px] overflow-hidden sm:h-[68vh]">
          <DemarcationMap
            basemap={basemap}
            view={config.view}
            layers={layers}
            shapes={config.shapes}
            narrowed={narrowed}
            selected={selected}
            onSelect={onSelect}
            activeLevel={activeLevel}
            onActiveLevel={setActiveChoice}
            fitKey={fitKey}
            style={config.style}
            boundary={boundary}
            maskUrl={config.boundaries.mask_url}
            boundarySelected={boundarySelected}
            onBoundarySelect={onBoundarySelect}
            onBoundaryState={onBoundaryState}
            onMap={onMap}
          >
            {locations.isLoading && (
              <div
                className="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-lg bg-white/90 px-3 py-1.5 text-xs font-medium text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                <Loader2 size={14} className="animate-spin" />
                {t('map.loading')}
              </div>
            )}
            {!locations.isLoading && locations.error == null && levels.length === 0 && (
              <div
                className="absolute right-3 top-3 z-10 rounded-lg bg-white/90 px-3 py-1.5 text-xs text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                {t('map.noLayers')}
              </div>
            )}
            {!locations.isLoading && locations.data?.empty && levels.length > 0 && (
              <div
                className="absolute right-3 top-3 z-10 rounded-lg bg-white/90 px-3 py-1.5 text-xs text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                {t('map.noCoordinates')}
              </div>
            )}
            {locations.isFetching && !locations.isLoading && (
              <div className="absolute right-3 bottom-8 z-10 rounded-full bg-white/90 p-1.5 shadow dark:bg-slate-900/90">
                <RefreshCw size={12} className="animate-spin text-slate-500" />
              </div>
            )}
          </DemarcationMap>
        </div>
      )}

      <ResultNotes notes={notes} />

      <BoundaryCard
        selection={boundarySelected}
        set={boundary}
        onClear={() => onBoundarySelect(null)}
      />

      {selected && selectedPoint && (
        <div className="card mb-4 flex flex-wrap items-center gap-3 p-3 text-sm">
          <ShapeSwatch
            shape={config.shapes.find((s) => s.key === selectedLayer?.layer.style.shape)}
            color={selectedLayer?.layer.style.point_color ?? '#2563eb'}
            size={16}
            hollow={selectedPoint.properties.source === 'DERIVED'}
          />
          <span className="font-medium text-slate-800 dark:text-slate-100">
            {selectedPoint.properties.name}
          </span>
          <span className="text-slate-500 dark:text-slate-400">
            {selectedLayer?.label} · {selectedPoint.properties.code}
          </span>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {selectedPoint.geometry.coordinates[1].toFixed(4)},{' '}
            {selectedPoint.geometry.coordinates[0].toFixed(4)}
          </span>
          {/* A derived point says so here as well as by being hollow: colour
              and shape are never the only signal. */}
          {selectedPoint.properties.source === 'DERIVED' && (
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {t('map.derivedPoint')}
            </span>
          )}
          {masterTable && (
            <Link
              className="btn-secondary ml-auto py-1 text-xs"
              to={`/data-management/master/${masterTable}`
                  + `/${encodeURIComponent(selected.code)}`}
            >
              <ExternalLink size={12} />
              {t('map.openRecord')}
            </Link>
          )}
          <button type="button" className="btn-secondary py-1 text-xs"
                  onClick={() => onSelect(null)}>
            <X size={12} />
            {t('common.clear')}
          </button>
        </div>
      )}
    </>
  );
}
