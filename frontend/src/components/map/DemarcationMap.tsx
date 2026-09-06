/**
 * The Area Demarcation canvas: coordinates, shaped by level, and no figures.
 *
 * The counterpart of `BusinessMap`, and separate from it for the reason
 * `useShapeRenderer` is separate from `useLayerRenderer`: what this draws is a
 * position, and everything the analysis map composes around a figure — the
 * metric selector, the colour legend, the tooltip of measures, the Top/Bottom
 * tables — has nothing to attach to here.
 *
 * The map instance, the basemap swap and the style-version counter are shared
 * through `MapLibreMap` and `useMapLibre`, so both tabs get one map for the
 * life of the page and a theme switch that keeps what is drawn.
 */

import type { Map as MapLibreInstance } from 'maplibre-gl';
import { useCallback, useState, type ReactNode } from 'react';
import { useT } from '../../contexts/I18nContext';
import type {
  MapBasemap, MapBoundarySet, MapLocationLayer, MapShape, MapStyle,
} from '../../types/api';
import { DemarcationLegend } from './DemarcationLegend';
import { LAYER_SUFFIXES, layerId } from './mapExpressions';
import { MapLibreMap } from './MapLibreMap';
import { useBoundaryLayer, type BoundarySelection } from './useBoundaryLayer';
import { useShapeRenderer, type ShapeHover, type ShapeSelection } from './useShapeRenderer';
import type { MapView } from './useMapLibre';

export interface DemarcationMapProps {
  basemap: MapBasemap;
  view: MapView;
  layers: MapLocationLayer[];
  shapes: MapShape[];
  /** Whether a filter is narrowing the points; the legend counts differently. */
  narrowed?: boolean;
  selected: ShapeSelection | null;
  onSelect: (selection: ShapeSelection | null) => void;
  activeLevel: string;
  onActiveLevel: (level: string) => void;
  fitKey: string;
  /** The administrative backdrop, or `null` for none. */
  boundary?: MapBoundarySet | null;
  maskUrl?: string | null;
  boundarySelected?: BoundarySelection | null;
  onBoundarySelect?: (selection: BoundarySelection | null) => void;
  style: MapStyle;
  onMap?: (map: MapLibreInstance | null) => void;
  children?: ReactNode;
}

export function DemarcationMap({
  basemap,
  view,
  layers,
  shapes,
  narrowed = false,
  selected,
  onSelect,
  activeLevel,
  onActiveLevel,
  fitKey,
  boundary = null,
  maskUrl,
  boundarySelected = null,
  onBoundarySelect,
  style,
  onMap,
  children,
}: DemarcationMapProps) {
  const t = useT();
  const [map, setMap] = useState<MapLibreInstance | null>(null);
  const [styleVersion, setStyleVersion] = useState(0);
  const [hover, setHover] = useState<ShapeHover | null>(null);

  const handleMap = useCallback((instance: MapLibreInstance | null, version: number) => {
    setMap(instance);
    setStyleVersion(version);
    onMap?.(instance);
  }, [onMap]);

  useShapeRenderer({
    map, styleVersion, layers, shapes, selected, onSelect, onHover: setHover, fitKey,
  });

  // Same backdrop, same hook, beneath the shapes for the same reason.
  useBoundaryLayer({
    map,
    styleVersion,
    boundary,
    maskUrl,
    style,
    selected: boundarySelected,
    onSelect: onBoundarySelect ?? (() => undefined),
    beforeId: layers[0] ? layerId(layers[0].level, LAYER_SUFFIXES.points) : undefined,
    pointLayerIds: () => layers.map((l) => layerId(l.level, LAYER_SUFFIXES.points)),
  });

  return (
    <div className="relative h-full w-full">
      <MapLibreMap basemap={basemap} view={view} onMap={handleMap}>
        <DemarcationLegend
          layers={layers}
          shapes={shapes}
          narrowed={narrowed}
          activeLevel={activeLevel}
          onActiveLevel={onActiveLevel}
        />
        {/*
          The hovered point's identity and where it came from — no figure to
          show, so this is a label rather than the analysis map's table of
          measures.

          `source` is on it because this map draws placed and derived
          coordinates alike and they are not the same claim: one is a position
          somebody stated, the other is the centroid of what sits below it, and
          a reader deciding whether a boundary falls in the right place needs to
          know which they are looking at. The hollow swatch says it too — colour
          and shape are never the only signal.
        */}
        {hover && (
          <div
            className="pointer-events-none absolute z-20 rounded bg-slate-900/90 px-2 py-1 text-xs text-white shadow"
            style={{ left: hover.point.x + 12, top: hover.point.y + 12 }}
            role="status"
          >
            <span className="font-medium">
              {hover.properties.name || hover.properties.code}
            </span>
            <span className="ml-1.5 text-slate-300">
              {layers.find((layer) => layer.level === hover.level)?.label
               ?? hover.properties.level}
              {' · '}
              {t(`map.source.${hover.properties.source}`)}
            </span>
          </div>
        )}
        {children}
      </MapLibreMap>
    </div>
  );
}
