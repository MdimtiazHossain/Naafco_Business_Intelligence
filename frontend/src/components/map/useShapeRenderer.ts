/**
 * The demarcation renderer: every level a shape, and not one figure anywhere.
 *
 * One GeoJSON source and up to four MapLibre layers per business layer —
 * clusters, cluster counts, the shaped points, and labels — added once and
 * then updated, exactly as the analysis renderer works. What differs is what
 * gets drawn: a `symbol` layer with a per-level icon rather than a `circle`
 * layer with a metric-driven colour and radius.
 *
 * There is no `if (level === 'zone')` here either. What separates a zone layer
 * from a customer layer is its configuration — shape, colour, the zoom it
 * appears at, the count it clusters above — and every one of those arrives
 * from the server.
 *
 * **Two things a symbol layer needs that a circle layer does not.**
 *
 * `icon-allow-overlap` must be `true`. MapLibre culls colliding symbols by
 * default, which on a demarcation map would silently drop points wherever they
 * are densest — precisely where the reader is looking, and with nothing on
 * screen to say it happened. A map that hides data to stay tidy is the failure
 * this platform refuses everywhere else.
 *
 * And the icon has to *exist*: an image is registered per (shape, colour) pair
 * and is discarded by `setStyle` along with the layers, so both are re-added
 * together whenever the style version changes.
 *
 * A `DERIVED` coordinate draws hollow. It is the centroid of what is placed
 * below it rather than a place anybody surveyed, and on a map whose whole
 * purpose is judging where a boundary falls, that is the distinction a reader
 * must not miss. One `case` expression on the feature's own `source` picks
 * between the two images, so it costs no second layer.
 */

import type {
  GeoJSONSource,
  Map as MapLibreInstance,
} from 'maplibre-gl';
import { useEffect, useRef } from 'react';
import type {
  MapLocationLayer,
  MapLocationProperties,
  MapShape,
} from '../../types/api';
import {
  LABEL_FONT,
  LAYER_SUFFIXES,
  POINT_FILTER,
  layerId,
  selectedFilter,
  sourceId,
} from './mapExpressions';
import {
  SOURCE_OPTIONS,
  addClusterLayers,
  clustersAt,
  useFitBounds,
  usePointerInteraction,
} from './mapLayerCore';
import { ensureShapeImages } from './mapShapes';

/**
 * A selected point on the demarcation map.
 *
 * Deliberately its own type rather than the analysis map's `MapSelection`:
 * that one carries `MapFeatureProperties`, which is a row of *measures*, and
 * there are none here. The two are narrowed to their common identity at the
 * page boundary, which is honest about their being different things rather
 * than widening one of them to `unknown` so they fit.
 */
export interface ShapeSelection {
  level: string;
  code: string;
  source: 'map' | 'table' | 'url';
  properties?: MapLocationProperties;
}

export interface ShapeHover {
  level: string;
  properties: MapLocationProperties;
  point: { x: number; y: number };
}

export interface ShapeRendererOptions {
  map: MapLibreInstance | null;
  styleVersion: number;
  /** Bottom to top. */
  layers: MapLocationLayer[];
  /** The declared catalogue, from `GET /api/map/config`. */
  shapes: MapShape[];
  selected: ShapeSelection | null;
  onSelect: (selection: ShapeSelection | null) => void;
  onHover: (hover: ShapeHover | null) => void;
  /** Changes when the data set changes; the map fits its bounds once per value. */
  fitKey: string;
}

/** Icon width in ems of the raster; the raster is 64px at pixelRatio 2. */
const ICON_SIZE = 0.5;
const SELECTED_ICON_SCALE = 1.45;

function removeLayer(map: MapLibreInstance, level: string): void {
  Object.values(LAYER_SUFFIXES).forEach((suffix) => {
    const id = layerId(level, suffix);
    if (map.getLayer(id)) map.removeLayer(id);
  });
  const id = sourceId(level);
  if (map.getSource(id)) map.removeSource(id);
}

/** Solid for a stated position, hollow for a computed centroid. */
function iconExpression(solid: string, outline: string): unknown {
  return ['case', ['==', ['get', 'source'], 'DERIVED'], outline, solid];
}

export function useShapeRenderer({
  map,
  styleVersion,
  layers,
  shapes,
  selected,
  onSelect,
  onHover,
  fitKey,
}: ShapeRendererOptions): void {
  /** What is drawn, and whether it was drawn clustered — per style version. */
  const drawn = useRef<{ version: number; levels: Map<string, boolean> }>({
    version: -1,
    levels: new Map(),
  });
  const layersRef = useRef(layers);
  layersRef.current = layers;

  useEffect(() => {
    if (!map) return;
    if (drawn.current.version !== styleVersion) {
      // A new style has neither our layers nor our images on it.
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
      const features = data.features.features;
      const clustered = clustersAt(layer.cluster_at, features.length);
      const id = sourceId(level);

      const existing = map.getSource(id) as GeoJSONSource | undefined;
      if (existing && drawn.current.levels.get(level) !== clustered) {
        // Clustering is fixed when a source is created, so a layer that
        // crossed its threshold is rebuilt rather than left un-clustered.
        removeLayer(map, level);
      }
      const source = map.getSource(id) as GeoJSONSource | undefined;
      if (source) {
        source.setData(data.features as never);
      } else {
        map.addSource(id, {
          type: 'geojson',
          data: data.features as never,
          cluster: clustered,
          ...SOURCE_OPTIONS,
        });
      }
      drawn.current.levels.set(level, clustered);

      const images = ensureShapeImages(map, shapes, layer.style.shape,
                                       layer.style.point_color);
      const pointsId = layerId(level, LAYER_SUFFIXES.points);
      const labelsId = layerId(level, LAYER_SUFFIXES.labels);
      const selectedId = layerId(level, LAYER_SUFFIXES.selected);

      if (clustered) {
        addClusterLayers(map, id, level, layer.style.cluster);
      }

      // The layer is added whether or not the images could be made. An icon
      // that does not exist is an invisible symbol and a console warning; a
      // layer that was never added is missing data with nothing to show for
      // it, which is the worse of the two by far.
      {
        const icon = iconExpression(images.solid, images.outline);
        if (!map.getLayer(pointsId)) {
          map.addLayer({
            id: pointsId,
            type: 'symbol',
            source: id,
            filter: POINT_FILTER as never,
            layout: {
              'icon-image': icon as never,
              'icon-size': ICON_SIZE,
              // Never cull a point to avoid an overlap: a demarcation map that
              // hid its densest cluster would hide the answer.
              'icon-allow-overlap': true,
              'icon-ignore-placement': true,
            },
          });
        } else {
          map.setLayoutProperty(pointsId, 'icon-image', icon as never);
        }

        if (!map.getLayer(selectedId)) {
          map.addLayer({
            id: selectedId,
            type: 'symbol',
            source: id,
            filter: selectedFilter(null) as never,
            layout: {
              'icon-image': icon as never,
              'icon-size': ICON_SIZE * SELECTED_ICON_SCALE,
              'icon-allow-overlap': true,
              'icon-ignore-placement': true,
            },
          });
        } else {
          map.setLayoutProperty(selectedId, 'icon-image', icon as never);
        }
      }

      if (!map.getLayer(labelsId)) {
        map.addLayer({
          id: labelsId,
          type: 'symbol',
          source: id,
          filter: POINT_FILTER as never,
          minzoom: layer.label_min_zoom,
          layout: {
            'text-field': ['get', 'name'],
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
        map.setLayoutProperty(labelsId, 'visibility',
                              layer.show_label ? 'visible' : 'none');
        map.setLayerZoomRange(labelsId, layer.label_min_zoom, 24);
      }

      const selectedCode = selected?.level === level ? selected.code : null;
      if (map.getLayer(selectedId)) {
        map.setFilter(selectedId, selectedFilter(selectedCode) as never);
      }

      // A layer that appears only past a zoom keeps its points, its ring and
      // its clusters together.
      [pointsId, selectedId,
       layerId(level, LAYER_SUFFIXES.clusters),
       layerId(level, LAYER_SUFFIXES.clusterCount)].forEach((id_) => {
        if (map.getLayer(id_)) map.setLayerZoomRange(id_, layer.min_zoom, 24);
      });
    });

    // Stacking: bottom to top in the design's order, labels above every point.
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
  }, [map, styleVersion, layers, shapes, selected]);

  usePointerInteraction<MapLocationProperties>({
    map,
    pointLayerIds: () => layersRef.current
      .map((layer) => layerId(layer.level, LAYER_SUFFIXES.points))
      .filter((id) => map?.getLayer(id) !== undefined),
    clusterLayerIds: () => layersRef.current
      .map((layer) => layerId(layer.level, LAYER_SUFFIXES.clusters))
      .filter((id) => map?.getLayer(id) !== undefined),
    onSelect: (properties) => onSelect(
      properties
        ? { level: properties.level, code: properties.code, source: 'map', properties }
        : null,
    ),
    onHover: (hover) => onHover(
      hover
        ? { level: hover.properties.level, properties: hover.properties,
            point: hover.point }
        : null,
    ),
  });

  useFitBounds(map, layers.map((layer) => layer.bounds), fitKey);
}
