/**
 * The Area Demarcation tab: every placed coordinate, and nothing measured.
 *
 * A separate component from the analysis tab rather than a mode of it, because
 * the two share almost nothing above the map instance: no period, no metric, no
 * filters, no ranking, and a legend that explains shapes rather than colours.
 * What they do share — the reader's state living in the URL, Reset being one
 * navigation, a control absent rather than disabled — is convention, not code.
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
import type { MapConfig, MapDesign, MapLocationLayer } from '../../types/api';
import { ErrorState } from '../States';
import { ResultNotes } from '../PageHeader';
import { DemarcationMap } from './DemarcationMap';
import { ShapeSwatch } from './DemarcationLegend';
import { useMapLocations } from './mapQueries';
import type { ShapeSelection } from './useShapeRenderer';

export interface DemarcationTabProps {
  config: MapConfig;
  design: MapDesign | undefined;
  designs: MapDesign[];
  onDesignChange: (designId: number | null) => void;
  levels: string[];
  onLevelsChange: (levels: string[]) => void;
  selected: ShapeSelection | null;
  onSelect: (selection: ShapeSelection | null) => void;
  onMap?: (map: MapLibreInstance | null) => void;
}

export function DemarcationTab({
  config, design, designs, onDesignChange, levels, onLevelsChange,
  selected, onSelect, onMap,
}: DemarcationTabProps) {
  const t = useT();
  const [activeChoice, setActiveChoice] = useState<string | null>(null);
  const query = useMapLocations(design?.design_id, levels);
  const layers: MapLocationLayer[] = query.data?.layers ?? [];

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

  // A new design or a different level set is fitted once; a selection never
  // moves the map by itself.
  const fitKey = useMemo(
    () => JSON.stringify({ design: design?.design_id ?? null, levels: [...levels].sort() }),
    [design, levels],
  );

  const toggle = useCallback((level: string) => {
    onLevelsChange(levels.includes(level)
      ? levels.filter((candidate) => candidate !== level)
      : [...levels, level]);
  }, [levels, onLevelsChange]);

  const notes = useMemo(
    () => [...new Set(layers.flatMap((layer) => layer.notes))],
    [layers],
  );

  const selectedLayer = layers.find((layer) => layer.level === selected?.level);
  const selectedPoint = selectedLayer?.features.features.find(
    (feature) => feature.properties.code === selected?.code,
  );
  const totalPlaced = layers.reduce((sum, layer) => sum + layer.placed, 0);

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

        {layers.length > 0 && (
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {t('map.pointsDrawn', { count: String(totalPlaced) })}
          </span>
        )}
      </div>

      {query.error != null && (
        <div className="card mb-4">
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </div>
      )}

      {basemap && (
        <div className="card mb-4 h-[60vh] min-h-[420px] overflow-hidden sm:h-[68vh]">
          <DemarcationMap
            basemap={basemap}
            view={config.view}
            layers={layers}
            shapes={config.shapes}
            selected={selected}
            onSelect={onSelect}
            activeLevel={activeLevel}
            onActiveLevel={setActiveChoice}
            fitKey={fitKey}
            onMap={onMap}
          >
            {query.isLoading && (
              <div
                className="absolute left-3 top-3 z-10 flex items-center gap-2 rounded-lg bg-white/90 px-3 py-1.5 text-xs font-medium text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                <Loader2 size={14} className="animate-spin" />
                {t('map.loading')}
              </div>
            )}
            {!query.isLoading && query.error == null && levels.length === 0 && (
              <div
                className="absolute right-3 top-3 z-10 rounded-lg bg-white/90 px-3 py-1.5 text-xs text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                {t('map.noLayers')}
              </div>
            )}
            {!query.isLoading && query.data?.empty && levels.length > 0 && (
              <div
                className="absolute right-3 top-3 z-10 rounded-lg bg-white/90 px-3 py-1.5 text-xs text-slate-600 shadow dark:bg-slate-900/90 dark:text-slate-300"
                role="status"
              >
                {t('map.noCoordinates')}
              </div>
            )}
            {query.isFetching && !query.isLoading && (
              <div className="absolute right-3 bottom-8 z-10 rounded-full bg-white/90 p-1.5 shadow dark:bg-slate-900/90">
                <RefreshCw size={12} className="animate-spin text-slate-500" />
              </div>
            )}
          </DemarcationMap>
        </div>
      )}

      <ResultNotes notes={notes} />

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
