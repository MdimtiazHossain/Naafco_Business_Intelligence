/**
 * The mechanics both map renderers share, extracted rather than copied.
 *
 * There are two renderers because there are two maps. `useLayerRenderer` draws
 * figures — circles sized and coloured by a metric, with extents, class breaks
 * and a selection ring scaled off the radius expression. `useShapeRenderer`
 * draws coordinates: symbol layers with an icon per level, no metric anywhere.
 * Neither is a special case of the other, and a single hook branching on which
 * map it was called for would be the `if (level === …)` this codebase forbids,
 * wearing a different hat.
 *
 * What they genuinely have in common is everything that is not about *paint*:
 * how a source is named, when clustering applies, how a cluster is drawn and
 * expanded, how the map fits its data once per data set, and how a click or a
 * hover becomes a selection. That is this module. Both renderers call it, so a
 * fix to the pointer handling or the fit lands on both maps at once.
 */

import type {
  GeoJSONSource,
  Map as MapLibreInstance,
  MapMouseEvent,
} from 'maplibre-gl';
import { useEffect, useRef } from 'react';
import { CLUSTER_FILTER, LABEL_FONT, layerId, LAYER_SUFFIXES } from './mapExpressions';

/** What a point on either map carries; each renderer knows its own extras. */
export interface PointIdentity {
  level: string;
  code: string;
}

export interface MapBoundsLike {
  west: number;
  east: number;
  south: number;
  north: number;
}

const CLUSTER_RADIUS = 50;
const CLUSTER_MAX_ZOOM = 14;
const FIT_PADDING = 48;
const FIT_MAX_ZOOM = 12;

export const SOURCE_OPTIONS = {
  clusterRadius: CLUSTER_RADIUS,
  clusterMaxZoom: CLUSTER_MAX_ZOOM,
} as const;

/**
 * Whether a layer clusters: only when it names a threshold and has more points
 * than it.
 *
 * Taken as a count rather than as a data object so both renderers can ask —
 * the analysis map counts features with figures, the demarcation map counts
 * placed coordinates, and the rule is the same either way.
 */
export function clustersAt(clusterAt: number | null, featureCount: number): boolean {
  return clusterAt !== null && featureCount > clusterAt;
}

/** Cluster circles grow with the number of points they stand for. */
export function clusterRadiusExpression(): unknown {
  return ['step', ['get', 'point_count'], 14, 20, 18, 100, 24, 500, 30];
}

/**
 * The two layers a clustered source needs: the bubble and its count.
 *
 * Identical on both maps, because a cluster is a count of points and says
 * nothing about what the points measure.
 */
export function addClusterLayers(
  map: MapLibreInstance,
  sourceId: string,
  level: string,
  style: { color: string; text_color: string },
): void {
  const clustersId = layerId(level, LAYER_SUFFIXES.clusters);
  const countId = layerId(level, LAYER_SUFFIXES.clusterCount);
  if (map.getLayer(clustersId)) return;
  map.addLayer({
    id: clustersId,
    type: 'circle',
    source: sourceId,
    filter: CLUSTER_FILTER as never,
    paint: {
      'circle-color': style.color,
      'circle-radius': clusterRadiusExpression() as never,
      'circle-opacity': 0.85,
      'circle-stroke-width': 2,
      'circle-stroke-color': '#ffffff',
    },
  });
  map.addLayer({
    id: countId,
    type: 'symbol',
    source: sourceId,
    filter: CLUSTER_FILTER as never,
    layout: {
      'text-field': ['get', 'point_count_abbreviated'],
      'text-font': LABEL_FONT,
      'text-size': 12,
    },
    paint: { 'text-color': style.text_color },
  });
}

/**
 * Fit the map to its data once per data set.
 *
 * Keyed rather than run on every change, because a layer toggle or a selection
 * must not move the map under the reader — only a genuinely new data set (a
 * different design, filter set or period) re-frames it.
 */
export function useFitBounds(
  map: MapLibreInstance | null,
  bounds: (MapBoundsLike | null)[],
  fitKey: string,
): void {
  const fitted = useRef<string | null>(null);
  useEffect(() => {
    if (!map || fitted.current === fitKey) return;
    const bounded = bounds.filter((box): box is MapBoundsLike => box !== null);
    if (bounded.length === 0) return;
    fitted.current = fitKey;
    map.fitBounds(
      [
        [Math.min(...bounded.map((b) => b.west)), Math.min(...bounded.map((b) => b.south))],
        [Math.max(...bounded.map((b) => b.east)), Math.max(...bounded.map((b) => b.north))],
      ],
      { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM, duration: 600 },
    );
    // `bounds` is rebuilt on every render; `fitKey` is what says it changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, fitKey]);
}

export interface PointerOptions<P extends PointIdentity> {
  map: MapLibreInstance | null;
  /** The point layer ids to hit-test, newest value read at event time. */
  pointLayerIds: () => string[];
  /** The cluster layer ids to hit-test. */
  clusterLayerIds: () => string[];
  onSelect: (identity: P | null) => void;
  onHover: (hover: { properties: P; point: { x: number; y: number } } | null) => void;
}

/**
 * Click to select, hover to preview, click a cluster to zoom into it.
 *
 * Registered once per map instance and reading the layer ids through callbacks,
 * so a layer toggle does not re-register handlers — MapLibre would otherwise
 * accumulate one set per render.
 */
export function usePointerInteraction<P extends PointIdentity>({
  map,
  pointLayerIds,
  clusterLayerIds,
  onSelect,
  onHover,
}: PointerOptions<P>): void {
  const pointsRef = useRef(pointLayerIds);
  const clustersRef = useRef(clusterLayerIds);
  const selectRef = useRef(onSelect);
  const hoverRef = useRef(onHover);
  pointsRef.current = pointLayerIds;
  clustersRef.current = clusterLayerIds;
  selectRef.current = onSelect;
  hoverRef.current = onHover;

  useEffect(() => {
    if (!map) return undefined;

    const hit = (event: MapMouseEvent, ids: string[]) =>
      (ids.length ? map.queryRenderedFeatures(event.point, { layers: ids })[0] : undefined);

    const onClick = (event: MapMouseEvent) => {
      const cluster = hit(event, clustersRef.current());
      if (cluster) {
        const clusterId = cluster.properties?.cluster_id as number | undefined;
        const source = map.getSource(cluster.source) as GeoJSONSource | undefined;
        if (clusterId !== undefined && source) {
          void source.getClusterExpansionZoom(clusterId).then((zoom) => {
            const [lng, lat] = (cluster.geometry as { coordinates: [number, number] })
              .coordinates;
            map.easeTo({ center: [lng, lat], zoom });
          });
        }
        return;
      }
      const point = hit(event, pointsRef.current());
      selectRef.current(point ? (point.properties as unknown as P) : null);
    };

    const onMove = (event: MapMouseEvent) => {
      const found = hit(event, [...pointsRef.current(), ...clustersRef.current()]);
      map.getCanvas().style.cursor = found ? 'pointer' : '';
      if (!found || found.properties?.cluster) {
        hoverRef.current(null);
        return;
      }
      hoverRef.current({
        properties: found.properties as unknown as P,
        point: { x: event.point.x, y: event.point.y },
      });
    };

    const onLeave = () => hoverRef.current(null);

    map.on('click', onClick);
    map.on('mousemove', onMove);
    map.on('mouseout', onLeave);
    map.on('movestart', onLeave);
    return () => {
      map.off('click', onClick);
      map.off('mousemove', onMove);
      map.off('mouseout', onLeave);
      map.off('movestart', onLeave);
    };
  }, [map]);
}
