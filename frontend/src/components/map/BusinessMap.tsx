/**
 * The business map proper: the canvas, the renderer, the legend and the
 * tooltip, composed once. The page decides what is drawn (which design, which
 * layers, which period and filters) and what is selected; this component
 * draws it and reports what the reader points at or clicks.
 */

import type { Map as MapLibreInstance } from 'maplibre-gl';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import type {
  MapBasemap, MapBoundarySet, MapLayerData, MapMetricInfo, MapStyle,
} from '../../types/api';
import { MapLegend } from './MapLegend';
import { MapLibreMap } from './MapLibreMap';
import { MapTooltip } from './MapTooltip';
import { LAYER_SUFFIXES, layerId } from './mapExpressions';
import {
  useBoundaryLayer,
  type BoundaryLayerState,
  type BoundarySelection,
} from './useBoundaryLayer';
import { useLayerRenderer, type MapHover, type MapSelection } from './useLayerRenderer';
import type { MapView } from './useMapLibre';

export interface BusinessMapProps {
  basemap: MapBasemap;
  view: MapView;
  layers: MapLayerData[];
  metrics: MapMetricInfo[];
  selected: MapSelection | null;
  onSelect: (selection: MapSelection | null) => void;
  activeLevel: string;
  onActiveLevel: (level: string) => void;
  fitKey: string;
  /** The administrative backdrop, or `null` for none. */
  boundary?: MapBoundarySet | null;
  maskUrl?: string | null;
  boundarySelected?: BoundarySelection | null;
  onBoundarySelect?: (selection: BoundarySelection | null) => void;
  /** Lifts the backdrop's loading state to whoever draws the picker. */
  onBoundaryState?: (state: BoundaryLayerState) => void;
  /** The style defaults, for the backdrop's paint. */
  style?: MapStyle;
  /** Called with the live map so the page can reset its view. */
  onMap?: (map: MapLibreInstance | null) => void;
  /** Overlays the page adds: the loading banner, an error, an empty note. */
  children?: ReactNode;
}

export function BusinessMap({
  basemap,
  view,
  layers,
  metrics,
  selected,
  onSelect,
  activeLevel,
  onActiveLevel,
  fitKey,
  boundary = null,
  maskUrl,
  boundarySelected = null,
  onBoundarySelect,
  onBoundaryState,
  style,
  onMap,
  children,
}: BusinessMapProps) {
  const [map, setMap] = useState<MapLibreInstance | null>(null);
  const [styleVersion, setStyleVersion] = useState(0);
  const [hover, setHover] = useState<MapHover | null>(null);
  const [width, setWidth] = useState(0);
  const wrapper = useRef<HTMLDivElement | null>(null);

  const handleMap = useCallback((instance: MapLibreInstance | null, version: number) => {
    setMap(instance);
    setStyleVersion(version);
    onMap?.(instance);
  }, [onMap]);

  useEffect(() => {
    const element = wrapper.current;
    if (!element) return undefined;
    setWidth(element.clientWidth);
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useLayerRenderer({
    map, styleVersion, layers, metrics, selected, onSelect, onHover: setHover, fitKey,
  });

  // The backdrop goes *beneath* the lowest business layer, so a point is never
  // hidden by a polygon and a click on a point still selects the point.
  const boundaryState = useBoundaryLayer({
    map,
    styleVersion,
    boundary,
    maskUrl,
    style: style ?? layers[0]?.layer.style ?? null as never,
    selected: boundarySelected,
    onSelect: onBoundarySelect ?? (() => undefined),
    beforeId: layers[0] ? layerId(layers[0].level, LAYER_SUFFIXES.points) : undefined,
    pointLayerIds: () => layers.map((l) => layerId(l.level, LAYER_SUFFIXES.points)),
  });

  // In an effect, not during render: a parent setter called while rendering a
  // child is a React warning and, across a tab switch, a loop.
  useEffect(() => {
    onBoundaryState?.(boundaryState);
  }, [onBoundaryState, boundaryState]);

  return (
    <div ref={wrapper} className="relative h-full w-full">
      <MapLibreMap basemap={basemap} view={view} onMap={handleMap}>
        {layers.length > 0 && (
          <MapLegend
            layers={layers}
            activeLevel={activeLevel}
            onActiveLevel={onActiveLevel}
            metrics={metrics}
          />
        )}
        <MapTooltip hover={hover} layers={layers} metrics={metrics} containerWidth={width} />
        {children}
      </MapLibreMap>
    </div>
  );
}
