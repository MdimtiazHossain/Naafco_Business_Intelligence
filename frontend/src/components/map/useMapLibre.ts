/**
 * One MapLibre instance for the life of a container.
 *
 * The map is created once, when a style is known, and is never rebuilt for a
 * filter change, a theme change or a data change: filters and data update the
 * sources and layers drawn *on* it (see the renderer), and a theme change
 * swaps the basemap with `setStyle`. Swapping a style discards the business
 * layers, which is why the hook counts style loads — a renderer keyed on
 * `styleVersion` re-adds its layers after every load, so a reader who
 * switches to dark mode keeps their map.
 *
 * The worker is named explicitly. MapLibre resolves it at runtime relative to
 * its own module URL, which a bundler cannot see through; Vite bundles the
 * worker as an asset when it is imported as one, and `setWorkerUrl` tells
 * MapLibre where that asset landed.
 */

import {
  AttributionControl,
  FullscreenControl,
  Map as MapLibreMap,
  NavigationControl,
  setWorkerUrl,
  type StyleSpecification,
} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import { useEffect, useRef, useState, type RefObject } from 'react';
import { styleKey } from './mapStyle';

setWorkerUrl(maplibreWorkerUrl);

export interface MapView {
  latitude: number;
  longitude: number;
  zoom: number;
}

export interface UseMapLibreOptions {
  /** The basemap style; `null` until configuration has arrived. */
  style: string | StyleSpecification | null;
  /** Where the map opens before any layer has answered. */
  view: MapView;
  /** Extra credit for the attribution control; the style's own is kept. */
  attribution?: string | null;
}

export interface MapLibreHandle {
  /** The live map, or `null` before it exists and after it is removed. */
  map: RefObject<MapLibreMap | null>;
  /** True once the first style has loaded and layers may be added. */
  ready: boolean;
  /** Incremented on every style load; a renderer re-adds its layers on change. */
  styleVersion: number;
}

export function useMapLibre(
  container: RefObject<HTMLDivElement | null>,
  { style, view, attribution }: UseMapLibreOptions,
): MapLibreHandle {
  const mapRef = useRef<MapLibreMap | null>(null);
  const appliedStyle = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [styleVersion, setStyleVersion] = useState(0);
  const hasStyle = style !== null;

  // Create once, as soon as there is a style to open with.
  useEffect(() => {
    if (!container.current || !style || mapRef.current) return undefined;

    const map = new MapLibreMap({
      container: container.current,
      style,
      center: [view.longitude, view.latitude],
      zoom: view.zoom,
      // Drawn by hand below so the provider's credit is never compacted away.
      attributionControl: false,
    });
    map.addControl(
      new AttributionControl({
        compact: false,
        ...(attribution ? { customAttribution: attribution } : {}),
      }),
      'bottom-right',
    );
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new FullscreenControl(), 'top-right');
    map.on('load', () => setReady(true));
    map.on('style.load', () => setStyleVersion((version) => version + 1));
    appliedStyle.current = styleKey(style);
    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
      appliedStyle.current = null;
      setReady(false);
    };
    // The map is created for a container and a first style; later style
    // changes are applied below without rebuilding it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [container, hasStyle]);

  // A different style — the theme switched — is swapped in place.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !style) return;
    const key = styleKey(style);
    if (appliedStyle.current === key) return;
    appliedStyle.current = key;
    map.setStyle(style);
  }, [style]);

  return { map: mapRef, ready, styleVersion };
}
