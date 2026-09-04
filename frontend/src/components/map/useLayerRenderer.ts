/**
 * The generic renderer: every business level drawn by the same code.
 *
 * One GeoJSON source and five MapLibre layers per business layer — clusters,
 * cluster counts, points, labels and the selection ring — added once and
 * then *updated*: a filter change calls `setData` on the source, a style
 * change re-applies paint properties, and a theme switch (which discards
 * everything drawn on the basemap) re-adds the lot, because the hook is keyed
 * on the map's style version. The map instance itself is never rebuilt.
 *
 * There is no `if (level === 'zone')` anywhere here. What differs between a
 * zone layer and a customer layer is data — the layer's configuration, its
 * extents, whether its point count is above its clustering threshold — and
 * all of it arrives from the backend.
 */

import type {
  GeoJSONSource,
  Map as MapLibreInstance,
  MapMouseEvent,
} from 'maplibre-gl';
import { useEffect, useRef } from 'react';
import type { MapFeatureProperties, MapLayerData, MapMetricInfo } from '../../types/api';
import {
  CLUSTER_FILTER,
  LABEL_FONT,
  LAYER_SUFFIXES,
  POINT_FILTER,
  clusterRadiusExpression,
  colorExpression,
  labelExpression,
  layerId,
  radiusExpression,
  selectedFilter,
  shouldCluster,
  sourceId,
} from './mapExpressions';

export interface MapSelection {
  level: string;
  code: string;
  /** Where the selection came from: the map itself, or a table beside it. */
  source: 'map' | 'table' | 'url';
  properties?: MapFeatureProperties;
}

export interface MapHover {
  level: string;
  properties: MapFeatureProperties;
  /** Pixel position inside the map container. */
  point: { x: number; y: number };
}

export interface LayerRendererOptions {
  map: MapLibreInstance | null;
  styleVersion: number;
  /** Bottom to top. */
  layers: MapLayerData[];
  metrics: MapMetricInfo[];
  selected: MapSelection | null;
  onSelect: (selection: MapSelection | null) => void;
  onHover: (hover: MapHover | null) => void;
  /** Changes when the data set changes; the map fits its bounds once per value. */
  fitKey: string;
}

const CLUSTER_RADIUS = 50;
const CLUSTER_MAX_ZOOM = 14;
const FIT_PADDING = 48;
const FIT_MAX_ZOOM = 12;

function pointLayerIds(map: MapLibreInstance, layers: MapLayerData[]): string[] {
  return layers
    .map((layer) => layerId(layer.level, LAYER_SUFFIXES.points))
    .filter((id) => map.getLayer(id) !== undefined);
}

function clusterLayerIds(map: MapLibreInstance, layers: MapLayerData[]): string[] {
  return layers
    .map((layer) => layerId(layer.level, LAYER_SUFFIXES.clusters))
    .filter((id) => map.getLayer(id) !== undefined);
}

function removeLayer(map: MapLibreInstance, level: string): void {
  Object.values(LAYER_SUFFIXES).forEach((suffix) => {
    const id = layerId(level, suffix);
    if (map.getLayer(id)) map.removeLayer(id);
  });
  const id = sourceId(level);
  if (map.getSource(id)) map.removeSource(id);
}

export function useLayerRenderer({
  map,
  styleVersion,
  layers,
  metrics,
  selected,
  onSelect,
  onHover,
  fitKey,
}: LayerRendererOptions): void {
  /** What is drawn, and whether it was drawn clustered — per style version. */
  const drawn = useRef<{ version: number; levels: Map<string, boolean> }>({
    version: -1,
    levels: new Map(),
  });
  const layersRef = useRef(layers);
  const selectRef = useRef(onSelect);
  const hoverRef = useRef(onHover);
  const fitted = useRef<string | null>(null);
  layersRef.current = layers;
  selectRef.current = onSelect;
  hoverRef.current = onHover;

  // Sources and layers: add what is missing, update what is there, drop what
  // is no longer wanted, and keep the stacking order the design asks for.
  useEffect(() => {
    if (!map) return;
    if (drawn.current.version !== styleVersion) {
      // A new style has nothing of ours on it, whatever the last one had.
      drawn.current = { version: styleVersion, levels: new Map() };
    }
    const wanted = new Set(layers.map((layer) => layer.level));
    drawn.current.levels.forEach((_, level) => {
      if (!wanted.has(level)) {
        removeLayer(map, level);
        drawn.current.levels.delete(level);
      }
    });

    layers.forEach((data) => {
      const { level, layer } = data;
      const clustered = shouldCluster(layer, data);
      const id = sourceId(level);
      const existing = map.getSource(id) as GeoJSONSource | undefined;
      if (existing && drawn.current.levels.get(level) !== clustered) {
        // Clustering is fixed when a source is created; a layer that crossed
        // its threshold is rebuilt rather than left un-clustered.
        removeLayer(map, level);
      }
      const source = map.getSource(id) as GeoJSONSource | undefined;
      if (source) {
        source.setData(data.features);
      } else {
        map.addSource(id, {
          type: 'geojson',
          data: data.features,
          cluster: clustered,
          clusterRadius: CLUSTER_RADIUS,
          clusterMaxZoom: CLUSTER_MAX_ZOOM,
        });
      }
      drawn.current.levels.set(level, clustered);

      const colour = colorExpression(layer, data, metrics);
      const radius = radiusExpression(layer, data, metrics);
      const clustersId = layerId(level, LAYER_SUFFIXES.clusters);
      const countId = layerId(level, LAYER_SUFFIXES.clusterCount);
      const pointsId = layerId(level, LAYER_SUFFIXES.points);
      const labelsId = layerId(level, LAYER_SUFFIXES.labels);
      const selectedId = layerId(level, LAYER_SUFFIXES.selected);

      if (clustered && !map.getLayer(clustersId)) {
        map.addLayer({
          id: clustersId,
          type: 'circle',
          source: id,
          filter: CLUSTER_FILTER,
          paint: {
            'circle-color': layer.style.cluster.color,
            'circle-radius': clusterRadiusExpression(),
            'circle-opacity': 0.85,
            'circle-stroke-width': 2,
            'circle-stroke-color': '#ffffff',
          },
        });
        map.addLayer({
          id: countId,
          type: 'symbol',
          source: id,
          filter: CLUSTER_FILTER,
          layout: {
            'text-field': ['get', 'point_count_abbreviated'],
            'text-font': LABEL_FONT,
            'text-size': 12,
          },
          paint: { 'text-color': layer.style.cluster.text_color },
        });
      }

      if (!map.getLayer(pointsId)) {
        map.addLayer({
          id: pointsId,
          type: 'circle',
          source: id,
          filter: POINT_FILTER,
          paint: {
            'circle-color': colour,
            'circle-radius': radius,
            'circle-opacity': 0.85,
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#ffffff',
          },
        });
      } else {
        map.setPaintProperty(pointsId, 'circle-color', colour);
        map.setPaintProperty(pointsId, 'circle-radius', radius);
      }

      if (!map.getLayer(labelsId)) {
        map.addLayer({
          id: labelsId,
          type: 'symbol',
          source: id,
          filter: POINT_FILTER,
          minzoom: layer.label_min_zoom,
          layout: {
            'text-field': labelExpression(layer, metrics),
            'text-font': LABEL_FONT,
            'text-size': 11,
            'text-anchor': 'top',
            'text-offset': [0, 1],
            'text-optional': true,
            visibility: layer.show_label ? 'visible' : 'none',
          },
          paint: {
            'text-color': '#1e293b',
            'text-halo-color': '#ffffff',
            'text-halo-width': 1.2,
          },
        });
      } else {
        map.setLayoutProperty(labelsId, 'text-field', labelExpression(layer, metrics));
        map.setLayoutProperty(labelsId, 'visibility', layer.show_label ? 'visible' : 'none');
        map.setLayerZoomRange(labelsId, layer.label_min_zoom, 24);
      }

      const selectedCode = selected?.level === level ? selected.code : null;
      if (!map.getLayer(selectedId)) {
        map.addLayer({
          id: selectedId,
          type: 'circle',
          source: id,
          filter: selectedFilter(selectedCode),
          paint: {
            'circle-color': 'rgba(0,0,0,0)',
            'circle-radius': ['+', radius as never, 5] as never,
            'circle-stroke-width': 3,
            'circle-stroke-color': '#0f172a',
          },
        });
      } else {
        map.setFilter(selectedId, selectedFilter(selectedCode));
        map.setPaintProperty(selectedId, 'circle-radius', ['+', radius as never, 5]);
      }

      // A layer that appears only past a zoom keeps its points and labels
      // together; the selection ring follows the points.
      map.setLayerZoomRange(pointsId, layer.min_zoom, 24);
      map.setLayerZoomRange(selectedId, layer.min_zoom, 24);
      if (map.getLayer(clustersId)) {
        map.setLayerZoomRange(clustersId, layer.min_zoom, 24);
        map.setLayerZoomRange(countId, layer.min_zoom, 24);
      }
    });

    // Stacking: bottom to top in the design's order, each moved above the
    // last. Labels ride above every point so nothing draws over a name.
    layers.forEach((data) => {
      [LAYER_SUFFIXES.clusters, LAYER_SUFFIXES.clusterCount, LAYER_SUFFIXES.points,
        LAYER_SUFFIXES.selected].forEach((suffix) => {
        const id = layerId(data.level, suffix);
        if (map.getLayer(id)) map.moveLayer(id);
      });
    });
    layers.forEach((data) => {
      const id = layerId(data.level, LAYER_SUFFIXES.labels);
      if (map.getLayer(id)) map.moveLayer(id);
    });
  }, [map, styleVersion, layers, metrics, selected]);

  // Pointer events, registered once per map instance.
  useEffect(() => {
    if (!map) return undefined;

    const onClick = (event: MapMouseEvent) => {
      const clusters = clusterLayerIds(map, layersRef.current);
      const cluster = clusters.length
        ? map.queryRenderedFeatures(event.point, { layers: clusters })[0]
        : undefined;
      if (cluster) {
        const clusterId = cluster.properties?.cluster_id as number | undefined;
        const source = map.getSource(cluster.source) as GeoJSONSource | undefined;
        if (clusterId !== undefined && source) {
          void source.getClusterExpansionZoom(clusterId).then((zoom) => {
            const [lng, lat] = (cluster.geometry as { coordinates: [number, number] }).coordinates;
            map.easeTo({ center: [lng, lat], zoom });
          });
        }
        return;
      }
      const points = pointLayerIds(map, layersRef.current);
      const hit = points.length
        ? map.queryRenderedFeatures(event.point, { layers: points })[0]
        : undefined;
      if (!hit) {
        selectRef.current(null);
        return;
      }
      const properties = hit.properties as unknown as MapFeatureProperties;
      selectRef.current({
        level: properties.level, code: properties.code, source: 'map', properties,
      });
    };

    const onMove = (event: MapMouseEvent) => {
      const points = [...pointLayerIds(map, layersRef.current), ...clusterLayerIds(map, layersRef.current)];
      const hit = points.length
        ? map.queryRenderedFeatures(event.point, { layers: points })[0]
        : undefined;
      map.getCanvas().style.cursor = hit ? 'pointer' : '';
      if (!hit || hit.properties?.cluster) {
        hoverRef.current(null);
        return;
      }
      const properties = hit.properties as unknown as MapFeatureProperties;
      hoverRef.current({ level: properties.level, properties, point: { x: event.point.x, y: event.point.y } });
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

  // Fit the data once per data set; a layer toggle does not move the map.
  useEffect(() => {
    if (!map || layers.length === 0 || fitted.current === fitKey) return;
    const bounded = layers.map((layer) => layer.bounds).filter((bounds) => bounds !== null);
    if (bounded.length === 0) return;
    const west = Math.min(...bounded.map((b) => b!.west));
    const east = Math.max(...bounded.map((b) => b!.east));
    const south = Math.min(...bounded.map((b) => b!.south));
    const north = Math.max(...bounded.map((b) => b!.north));
    fitted.current = fitKey;
    map.fitBounds([[west, south], [east, north]], {
      padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM, duration: 600,
    });
  }, [map, layers, fitKey]);

  // A selection made from a table pans to the point, if it has one.
  useEffect(() => {
    if (!map || !selected || selected.source !== 'table') return;
    const layer = layers.find((candidate) => candidate.level === selected.level);
    const feature = layer?.features.features.find((f) => f.properties.code === selected.code);
    if (!feature) return;
    map.easeTo({ center: feature.geometry.coordinates, duration: 500 });
  }, [map, layers, selected]);
}
