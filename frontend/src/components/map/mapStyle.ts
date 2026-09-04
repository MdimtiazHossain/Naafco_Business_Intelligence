/**
 * Which style MapLibre loads for a basemap, in a theme.
 *
 * The provider is configuration, not code: `GET /api/map/config` names the
 * basemaps the deployment offers and this module only turns one of them into
 * something MapLibre accepts. A style document is handed over as its URL. A
 * raster `{z}/{x}/{y}` template — a plain OpenStreetMap-compatible tile server
 * — is wrapped in the one-source style MapLibre needs, with the provider's
 * attribution on the source so the credit is drawn exactly as a style's would
 * be. Nothing here names a tile server.
 */

import type { StyleSpecification } from 'maplibre-gl';
import type { MapBasemap } from '../../types/api';

export type ThemeName = 'light' | 'dark';

/** The URL a basemap uses in a theme: the dark style when it has one. */
export function basemapUrl(basemap: MapBasemap, theme: ThemeName): string {
  return theme === 'dark' && basemap.style_url_dark ? basemap.style_url_dark : basemap.style_url;
}

/**
 * A minimal style around one raster tile template.
 *
 * `glyphs` is what lets a label be drawn at all: MapLibre renders text from a
 * glyph source the style names, and a tile template names none.
 */
export function rasterStyle(
  template: string,
  attribution: string | null,
  glyphs: string | null = null,
): StyleSpecification {
  return {
    version: 8,
    ...(glyphs ? { glyphs } : {}),
    sources: {
      basemap: {
        type: 'raster',
        tiles: [template],
        tileSize: 256,
        attribution: attribution ?? '',
      },
    },
    layers: [{ id: 'basemap', type: 'raster', source: 'basemap' }],
  };
}

/** What to give MapLibre for this basemap in this theme. */
export function basemapStyle(
  basemap: MapBasemap,
  theme: ThemeName,
): string | StyleSpecification {
  const url = basemapUrl(basemap, theme);
  return basemap.kind === 'raster'
    ? rasterStyle(url, basemap.attribution, basemap.glyphs_url)
    : url;
}

/**
 * A stable identity for a style, so a hook can tell "the same style again"
 * from "a different one" without deep-comparing a style document.
 */
export function styleKey(style: string | StyleSpecification): string {
  return typeof style === 'string' ? style : JSON.stringify(style);
}
