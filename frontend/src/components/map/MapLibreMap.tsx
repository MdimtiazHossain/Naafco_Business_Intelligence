/**
 * The map canvas: a MapLibre instance over the configured basemap, following
 * the application theme, with whatever the page lays over it.
 *
 * This component owns nothing about business data. It hands the live map to
 * its parent through `onMap` — once when the first style has loaded, and again
 * after every style swap, with a rising `styleVersion` — and the parent's
 * renderer draws the layers. Keeping the canvas ignorant of what is drawn is
 * what lets the same instance survive every filter, theme and design change.
 */

import type { Map as MapLibreInstance } from 'maplibre-gl';
import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useTheme } from '../../contexts/ThemeContext';
import type { MapBasemap } from '../../types/api';
import { basemapStyle } from './mapStyle';
import { useMapLibre, type MapView } from './useMapLibre';

export interface MapLibreMapProps {
  basemap: MapBasemap;
  view: MapView;
  /** Called with the map once it can take layers, and after every style load. */
  onMap?: (map: MapLibreInstance | null, styleVersion: number) => void;
  /** Overlays: legends, notices, the loading banner. Positioned by the caller. */
  children?: ReactNode;
  className?: string;
}

export function MapLibreMap({ basemap, view, onMap, children, className = '' }: MapLibreMapProps) {
  const { resolved } = useTheme();
  const style = useMemo(() => basemapStyle(basemap, resolved), [basemap, resolved]);
  const container = useRef<HTMLDivElement | null>(null);
  const { map, ready, styleVersion } = useMapLibre(container, {
    style,
    view,
    attribution: basemap.attribution,
  });

  useEffect(() => {
    onMap?.(ready ? map.current : null, styleVersion);
  }, [map, onMap, ready, styleVersion]);

  return (
    <div className={`relative h-full w-full ${className}`}>
      <div
        ref={container}
        className="h-full w-full"
        data-testid="map-canvas"
        role="region"
        aria-label={basemap.label}
      />
      {children}
    </div>
  );
}
