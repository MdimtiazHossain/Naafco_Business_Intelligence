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
import type { MapBasemap, MapLocationLayer, MapShape } from '../../types/api';
import { DemarcationLegend } from './DemarcationLegend';
import { MapLibreMap } from './MapLibreMap';
import { useShapeRenderer, type ShapeHover, type ShapeSelection } from './useShapeRenderer';
import type { MapView } from './useMapLibre';

export interface DemarcationMapProps {
  basemap: MapBasemap;
  view: MapView;
  layers: MapLocationLayer[];
  shapes: MapShape[];
  selected: ShapeSelection | null;
  onSelect: (selection: ShapeSelection | null) => void;
  activeLevel: string;
  onActiveLevel: (level: string) => void;
  fitKey: string;
  onMap?: (map: MapLibreInstance | null) => void;
  children?: ReactNode;
}

export function DemarcationMap({
  basemap,
  view,
  layers,
  shapes,
  selected,
  onSelect,
  activeLevel,
  onActiveLevel,
  fitKey,
  onMap,
  children,
}: DemarcationMapProps) {
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

  return (
    <div className="relative h-full w-full">
      <MapLibreMap basemap={basemap} view={view} onMap={handleMap}>
        <DemarcationLegend
          layers={layers}
          shapes={shapes}
          activeLevel={activeLevel}
          onActiveLevel={onActiveLevel}
        />
        {/* The hovered point's own name and level — no figure to show, so this
            is a label rather than the analysis map's table of measures. */}
        {hover && (
          <div
            className="pointer-events-none absolute z-20 rounded bg-slate-900/90 px-2 py-1 text-xs text-white shadow"
            style={{ left: hover.point.x + 12, top: hover.point.y + 12 }}
            role="status"
          >
            {hover.properties.name || hover.properties.code}
          </div>
        )}
        {children}
      </MapLibreMap>
    </div>
  );
}
